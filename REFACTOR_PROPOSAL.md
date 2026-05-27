# Hospital Protocol Bot — Refactor Proposal

Reviewer notes are organized to match the requested output structure (A–L). No code is written; this document is the design contract that the implementation will follow.

---

## A. Diagnosis of the current failure

### A.1 Why "Strep pneumo" routes to CAP instead of BioFire organism interpretation

Two compounding causes, both in `aliases.json`/`normalize_question`:

1. **CAP owns terms that are not syndrome terms.** `conditions.cap.aliases` contains broad strings: `"pneumonia"`, `"pneumoniara"`, `"pneumoniás"`, `"pneumoniat"`, `"pneumoniában"`, plus all the misspells. These are syndrome terms only by convention; in a microbiology query like "Strep pneumo", they substring-collide with the organism's name fragment. The longest-first sort in `normalize_question` does not save us — once `"pneumonia"` lives in CAP and is short enough to substring-collide with organism queries, CAP wins.
2. **No BioFire short-form for "Strep pneumo".** `conditions.biofire.aliases` has `"strep pn"`, `"strep pneumoniae"`, `"streptococcus pneumoniae"`, `"pneumococcus"`, `"pneumococcal"` — but no `"strep pneumo"`. Even the existing organism aliases live under the BioFire **protocol** rather than as first-class organism entities, so they only fire when the user mentions BioFire-flavored wording.
3. **Active-state carryover masks failed recognition.** When fresh recognition is weak or fails, the bot reuses the previous turn's `active_recognized`. In the log, turn 3 ("Strep pneumo") follows turn 2 (CAP confirmed), so even if recognition had failed cleanly the bot would stay in CAP.
4. **Fuzzy matching is disabled on Railway** (`rapidfuzz not installed`), so the production matcher is exact-substring only. That hides one class of routing bugs in dev but also means any token-level overlap with a long CAP alias dominates.

### A.2 Why active state lets CAP persist into later vague turns

`CONVERSATION_STATE[chat_id]` holds `active_recognized` and is cleared only on `/reset`, "új beteg" style phrases, or when fresh recognition returns a different protocol with sufficient confidence. There is **no rule that says "vague follow-up (`mit adjak?`, `what dose?`, `gfr 60 50kg`) must be evaluated against the active protocol's authorization, not just its identity"**. So:

- The bot keeps `active_recognized = CAP` after turn 2.
- Turn 3 ("Strep pneumo") nominally re-recognizes CAP via the broken alias, but even if it hadn't, state would have stuck.
- Turns 4–5 ("what dose?", "gfr 60, 50kg") never had a recognition strong enough to displace CAP, so they inherit CAP — and CAP is allowed to answer dosing because the protocol text contains the string "Ceftriaxone 2 g".

### A.3 Why semantic retrieval lets CAP, BioFire, TMP/SMX fragments cross-contaminate

The retriever is purely embedding-similarity (`text-embedding-3-small`, cosine, top-k) across **all** chunks in `PROTOCOL_CHUNKS` without any protocol filter. The log shows the smoking guns:

- Turn 2 "Pneumococcal pneumonia, what ab?" → top-3 = `CAP:0.63 CAP:0.42 BioFire:0.47` (CAP-BioFire mixed)
- Turn 4 "What's the dose?" → `CAP:0.6 CAP:0.47 TMP/SMX:0.55` (CAP + TMP/SMX mixed; this is where the "kell az indikáció, vesefunkció, testsúly" wording came from — that is TMP/SMX prose)
- Turn 5 "Gfr 60, 50kg" → `CAP:0.48 CAP:0.41 TMP/SMX:0.49`

The retriever cannot tell that "TMP/SMX wants indication/weight/GFR" is irrelevant guidance for a CAP context. Once those chunks are in the prompt, the LLM faithfully fuses the active-protocol identity (CAP, ceftriaxone) with the dosing-philosophy fragments from TMP/SMX and the literal "Ceftriaxone 2 g" found in `### INTUBATED_CAP`.

### A.4 Why the LLM can invent ceftriaxone dosing despite safety rules

Three failures stacking:

1. **`cap.txt` actually contains dose-looking text.** `### INTUBATED_CAP` includes `"Step 2 — Until BioFire results available: Ceftriaxone 2 g"`. `### ASPIRATION_PNEUMONIA`, `### COPD_ACUTE_EXACERBATION`, `### HOSPITALIZED_CAP_NON_INTUBATED` each name ceftriaxone (mostly without dose), but the intubated branch puts a dose right next to the antibiotic. To the LLM, this is gold-plated source material — there is **no marker** distinguishing "treatment-choice text the protocol authorizes" from "dose authorized for free-text use".
2. **No post-LLM authorization gate.** `clean_response` strips markdown, file paths, and stray "not specified" lines. It does **not** check whether a dose pattern (`\d+\s*(mg|g|amp|...)`) in the output corresponds to an authorized dose for an authorized antibiotic within the active protocol. The dose pattern check it does have (`_HAS_DOSING_RE`) is only used to decide whether to delete a "not specified" sentence — it actively suppresses the safety phrase when the model emits a number.
3. **`safety_rules.txt` and `system_rules.txt` are prompt-level constraints.** Prompt-level safety is necessary but not sufficient; once the protocol text contains a number adjacent to the recommended antibiotic, no amount of "do not invent" in the system prompt reliably prevents the model from echoing it.

### A.5 Is the decision-tree / state / footer code wired correctly?

Partially. The pieces exist (`parse_decision_tree`, `init_tree_state`, `advance_tree_state`, `dispatch_tree`, `_classify_branch`, `_maybe_attach_links`, `apply_footer`, `finalize_answer`), and unit tests 6–10 in `test_bot.py` confirm the parser and dispatcher work in isolation. But:

- `cap.txt` declares `## DECISION_TREE\n\n(none)` — CAP has no tree, so the deterministic dispatcher is never invoked for the CAP flow. CAP relies entirely on the LLM + injected policy header + RAG, which is exactly the path where the bug lives.
- `pneumonia_pcr.txt` **has** a tree (`ROOT: ask_result`) but the tree is only activated when BioFire is the selected protocol; in turn 1 of the log the bot answered with a tree-leaf string (`"Streptococcus pneumoniae — Tier 1 — Ceftriaxone. Dosing: not in this protocol."`) which is correct. So when the tree fires it works.
- The footer system works (turn 1 appended BioFire's footer, turn 5 appended a Source line); the bug is upstream of the footer.

So the dispatcher/tree machinery is *correct but underused* — most syndromes including CAP fall back to free-form LLM+RAG with no deterministic gate.

### A.6 Is the current schema parser sufficient?

It parses the canonical panels but is too permissive:

- `protocol_type` is **not** a parsed field — the parser cannot tell CAP from BioFire from TMP/SMX from a general-rules file. The bot infers "type" only via `source_label` heuristics in tests.
- There is no `DOSING_ALLOWED` panel. Any string in the file that contains `\d+\s*(mg|g)` is fair game for the LLM.
- There is no `ANTIBIOTIC_CHOICES_ALLOWED` panel. The set of drugs the protocol authorizes the bot to mention is implicit in the prose.
- `PROTOCOL_LINKS` exists but only as a soft "Need dosing? → ceftriaxone" offer; it does not constrain what the LLM may say in the meantime.
- Startup validation (lines ~142–214) checks file existence, JSON validity, ALLOWED_USER_IDS — but never inspects whether a syndrome protocol contains dose-looking text.

The parser is the right shape; it needs more required fields and stricter validation, not a rewrite.

---

## B. Protocol taxonomy

Four explicit `protocol_type` values, declared in METADATA and validated at startup.

| protocol_type | Example file | May name antibiotic? | May output dose? | Notes |
|---|---|---|---|---|
| `SYNDROME_PROTOCOL` | `cap.txt` | Yes (from `ANTIBIOTIC_CHOICES_ALLOWED`) | Only from `DOSING_ALLOWED`; default `(none)` | Owns patient-status gating and pathway selection. Cannot answer organism-only queries unless linked. |
| `MICROBIOLOGY_INTERPRETATION_PROTOCOL` | `pneumonia_pcr.txt` | Yes (per tier mapping) | Only from `DOSING_ALLOWED`; usually `(none)` | Owns organism/resistance → tier mapping. Does not own patient-status gating. |
| `DRUG_DOSING_PROTOCOL` | `meropenem.txt`, `tmpsmx.txt`, `ampsul.txt` | Only the drug it covers | Yes — this is its purpose | Owns dose, route, renal adjustment, infusion details. |
| `GENERAL_RULES_PROTOCOL` | `general_rules_antibiotic_dosing.txt` | No | No | Behavioural constraints, monitoring philosophy, escalation triggers only. Never selected as the active answer source. |

Every protocol file gains:

```
protocol_type: SYNDROME_PROTOCOL | MICROBIOLOGY_INTERPRETATION_PROTOCOL | DRUG_DOSING_PROTOCOL | GENERAL_RULES_PROTOCOL
allows_antibiotic_choice: yes | no
allows_dosing: yes | no
```

`allows_dosing: yes` requires a non-empty `DOSING_ALLOWED` panel; `allows_antibiotic_choice: yes` requires a non-empty `ANTIBIOTIC_CHOICES_ALLOWED` panel. Startup validation refuses to load otherwise.

---

## C. Hard authorization gates

Five deterministic gates, applied *after* the LLM returns and *before* the response is sent to the user.

### C.1 Antibiotic choice gate

After the LLM response is produced, scan for antibiotic name tokens (a curated lexicon of drug names + aliases). Each named drug must be:

- present in `active_protocol.antibiotic_choices_allowed`, **or**
- present in `linked_protocol.antibiotic_choices_allowed` where `linked_protocol` is explicitly authorized for this turn by `LINKED_PROTOCOL_RULES`.

Unauthorized drug names are stripped or replaced with a neutral fallback ("This antibiotic is not listed in the active protocol").

### C.2 Dosing gate (the headline fix)

A "dose-bearing token" is any of:

- numeric + unit: `\d+\s*(mg|g|amp|mcg|ml|mmol|tablet|unit|IU)`
- frequency: `q\d+h`, `\d+\s*x\s*\d+`, `napi\s*\d+`, `naponta`, `BID`, `TID`, `QID`, `OD`
- renal-adjustment phrases: `GFR\s*[<>]`, `csökkentett dózis`, `50%`, `CRRT`, `IHD`, `dialysis`
- infusion descriptors: `infusion`, `infúzió`, `bolus`, `over\s*\d+`, `pump`, `prolonged infusion`

The output may contain dose-bearing tokens only if **both** are true:
1. `active_protocol.allows_dosing == yes` **or** an explicitly linked drug-dosing protocol is the dose source on this turn.
2. The exact dose text appears in `active_protocol.DOSING_ALLOWED` (or the linked protocol's DOSING_ALLOWED), matched by a normalized-string check.

Otherwise the response is rewritten:

- If the antibiotic *was* authorized as a choice but no dosing protocol is active:
  `"Ceftriaxone is recommended by the [CAP / BioFire] pathway, but ceftriaxone dosing is not specified in the uploaded protocol."`
- If no antibiotic was authorized either:
  `"Dose is not specified in the uploaded protocol."`

This is the gate that would have caught the turn-5 bug.

### C.3 Source-isolation gate

The active protocol's retrieved chunks are the only chunks used unless explicitly authorized:

- `CAP active` may consult BioFire only if the user message contains a BioFire/PCR/panel/result token, **or** `cap.txt`'s `LINKED_PROTOCOL_RULES` says "consult BioFire when patient is intubated".
- `BioFire active` may ask CAP-style status questions only if BioFire's `LINKED_PROTOCOL_RULES` says so.
- `DRUG_DOSING active` never auto-consults syndrome/microbiology protocols for treatment-choice; it answers dosing for the drug it covers.

Implementation: after retrieval, filter chunks by `active_protocol_file ∪ authorized_linked_files`. No cross-protocol synthesis unless declared.

### C.4 General-rules gate

`general_rules_antibiotic_dosing.txt` is loaded but is **never** the active protocol. Its chunks are appended to context as "constraints only", and the authorization gates ignore it as a source for antibiotic choices or doses. A safety check rejects it if `select_active_protocol` ever returns it.

### C.5 Retrieved-source gate

If post-retrieval, the top-N chunks include >1 protocol type and the bot has not yet selected an active protocol, the bot must:

- pick one deterministically by priority (drug > test-result > organism+context > syndrome), **or**
- emit a disambiguation question ("Drug dosing question, BioFire result, or syndrome treatment?"). No silent synthesis.

---

## D. Active protocol state model

Replace `CONVERSATION_STATE[chat_id] = {"history": [...], "active_recognized": {...}}` with a richer structure:

```
CONVERSATION_STATE[chat_id] = {
    "history": [...],                       # last N turns, unchanged

    "active_protocol_file": str | None,     # normalized path
    "active_protocol_type": str | None,     # SYNDROME / MICRO / DOSING / GENERAL
    "active_task_type":    str | None,     # syndrome_pathway / microbiology_interpretation
                                            # / drug_dosing / general_question
    "active_entity": {                      # what is the user actually asking about?
        "kind":  "syndrome" | "organism" | "resistance_marker" | "drug" | None,
        "label": str | None,                # e.g. "CAP", "Streptococcus pneumoniae", "ceftriaxone"
    },

    "collected_parameters": {               # accumulated across turns
        "patient_status":   "intubated" | "hospitalized" | "dischargeable" | None,
        "weight_kg":        float | None,
        "renal_gfr":        float | None,
        "indication":       str | None,
        "biofire_result":   str | None,
        # ...
    },
    "pending_required_parameter": str | None,   # what the bot is currently asking for

    "current_tree_node":  str | None,       # only if a DECISION_TREE protocol is active
    "tree_path":          [str],            # breadcrumb of visited nodes (for /debug)

    "last_user_intent":              str | None,    # from classify_user_intent
    "last_authorized_antibiotic":    str | None,    # last antibiotic NAMED with authorization
    "last_authorized_dose_source":   str | None,    # protocol file that authorized the last dose, if any

    "protocol_stack":   [str],              # only populated when LINKED_PROTOCOL_RULES says so
    "linked_protocols": [str],              # files temporarily consulted for this turn

    "pending_links":         [...],         # existing offer-and-confirm flow, unchanged
    "pending_topic_switch":  {...} | None,  # existing topic-switch flow, unchanged
}
```

### Reset rules

| Trigger | Effect |
|---|---|
| `/reset`, `/clear` | Wipe everything except `history` if explicit reset of history is desired too. |
| User message matches `"új beteg"`, `"másik beteg"`, `"new case"`, `"new patient"` (regex on normalized text) | Wipe everything. |
| High-confidence (`exact` or `score ≥ 88`) drug alias detected | Switch to `DRUG_DOSING` for that drug. Keep `collected_parameters` if compatible. |
| High-confidence BioFire/PCR/panel/result alias detected | Switch to `MICROBIOLOGY_INTERPRETATION`. Keep `collected_parameters`. |
| High-confidence syndrome alias detected | Switch to `SYNDROME`. Keep `collected_parameters`. |
| Vague follow-up (`mit adjak`, `what dose`, `adag`, `dose?`, bare numbers/units) | **Do NOT change protocol.** Resolve against `last_authorized_antibiotic` + active protocol's authorization rules. If no dosing source authorizes a dose, answer "dosing not specified". |
| Off-topic | Stay in current state but emit "I can answer protocol-based questions about X. Right now we're discussing Y." |

The vague-follow-up rule is the second of the two changes that would have fixed the log: turn 4 ("What's the dose?") would resolve to "ceftriaxone (last authorized) → no DRUG_DOSING protocol for ceftriaxone exists → answer 'ceftriaxone dosing is not specified in the uploaded protocol'", and turn 5 would do the same instead of synthesizing 2 g IV.

---

## E. Alias and entity recognition fixes

### E.1 New aliases.json structure

Split into five categories rather than two:

```
{
  "drugs":             { ... maps to DRUG_DOSING_PROTOCOL files ... },
  "conditions":        { ... maps to SYNDROME_PROTOCOL files ... },
  "organisms":         { ... maps to MICROBIOLOGY_INTERPRETATION_PROTOCOL files,
                            but ONLY when a microbiology context exists ... },
  "resistance_markers":{ ... maps to MICROBIOLOGY_INTERPRETATION_PROTOCOL files ... },
  "tests_platforms":   { ... e.g. BioFire, FilmArray, GeneXpert, PCR, panel, result ... }
}
```

Each entry keeps `display`, `canonical`, `source_label`, `protocol_file`, `aliases` — and gains:

```
"entity_type": "drug" | "condition" | "organism" | "resistance_marker" | "test_platform",
"requires_context": ["test_result_present"]   # optional; only fires when context flag set
```

### E.2 Concrete changes

- Move `"streptococcus pneumoniae"`, `"strep pneumoniae"`, `"strep pn"`, `"s. pneumoniae"`, `"s.pneumoniae"`, `"pneumococcus"`, `"pneumococcal"`, `"strep pneumo"` (and the obvious typo `"pneumococus"`) into `organisms.streptococcus_pneumoniae` with `requires_context = ["test_result_present"]`.
- Add `"strep pneumo"` to the organism aliases (currently missing — it does not appear in any list).
- Remove `"pneumonia"` and its inflections from CAP unless they coexist with a syndrome marker. Concretely: keep `"community acquired pneumonia"`, `"otthon szerzett pneumonia"`, `"CAP"`, `"COPD exacerbation"`, `"aspiration pneumonia"`, `"HAP"`, `"VAP"`, etc. Drop bare `"pneumonia"`, `"pneumoniara"`, `"pneumoniás"`, `"pneumoniát"`, `"pneumoniában"`, `"tüdőgyulladás"` (alone) **unless** the recognition cascade has determined no organism/drug/test-platform match first.
- Add a `tests_platforms` category for `"biofire"`, `"filmarray"`, `"pcr"`, `"panel"`, `"result"`, `"eredmény"`, `"pozitív"`. Detecting a token here flips a `test_result_present` context flag that gates the organism aliases.
- Keep CAP aliases narrow but allow a "no other match wins" fallback: if recognition produces no match in (1)-(4) below and the message contains a bare `"pneumonia"`/`"tüdőgyulladás"`, ask "Is this a syndrome question or a BioFire result?" instead of silently picking CAP.

### E.3 Recognition hierarchy (replaces the current cascade in `normalize_question`)

Evaluate in this strict order; first match wins:

1. **Explicit drug alias** (`drugs.*`) → `DRUG_DOSING`.
2. **Explicit test/platform alias** (`tests_platforms.*`) → enable `test_result_present`; continue to organism check.
3. **Resistance marker** (`resistance_markers.*`) → `MICROBIOLOGY_INTERPRETATION`, force `test_result_present`.
4. **Organism alias** AND `test_result_present` is set (either from this message or a recent turn) → `MICROBIOLOGY_INTERPRETATION`.
5. **Syndrome alias** (`conditions.*`) → `SYNDROME`.
6. **Vague follow-up** detected (`mit adjak`, `adag`, `dose`, `how much`, bare number+unit) → use active state; do not re-route.
7. **Unknown / off-topic** → emit a clarifying question or off-topic response.

The hierarchy is the *only* place protocol routing happens. Semantic retrieval never alters it.

---

## F. Canonical protocol schema

Every protocol file MUST have the following panels in this exact order. Missing-but-required panels are written as `(none)`; startup validation rejects files that omit the panel headers entirely.

```
# TITLE

## METADATA
protocol_name:        <human-readable name>
source_label:         <label shown in "Source: ..." line>
protocol_type:        SYNDROME_PROTOCOL | MICROBIOLOGY_INTERPRETATION_PROTOCOL
                      | DRUG_DOSING_PROTOCOL | GENERAL_RULES_PROTOCOL
canonical_entities:   <list of canonical entity labels this protocol owns>
protocol_file:        protocols/<file>.txt
allows_antibiotic_choice: yes | no
allows_dosing:        yes | no
linked_protocols:     <list of (entity -> protocol_file) entries; can be (none)>
forbidden_outputs:    <free-text list of things this protocol must not produce>

## ALIASES
drug_aliases:         <list or (none)>
condition_aliases:    <list or (none)>
organism_aliases:     <list or (none)>
resistance_aliases:   <list or (none)>
test_aliases:         <list or (none)>

## ANSWER_POLICY
<prose explaining when and how to answer>

## REQUIRED_INFORMATION
<bulleted list of fields that must be collected before any pathway selection>

## PREFERRED_INFORMATION
<bulleted list of nice-to-have fields, asked AFTER initial guidance>

## MODIFIER_INFORMATION
<bulleted list of fields that may modify therapy>

## DEFAULT_QUESTION
<exact text the bot must emit when REQUIRED_INFORMATION is missing>
<may be (none) for protocols with no default question>

## PATHWAY_PRIORITY
<ordered list of pathways; first-match-wins semantics>

## DECISION_TREE
<deterministic state machine as currently parsed by parse_decision_tree, or (none)>

## TREATMENT_PATHWAYS
<free-text or sub-sectioned pathway prose — LLM-visible context>

## DOSING_ALLOWED
<the ONLY section whose dose statements may be output as dose>
<each line: drug, dose, route, frequency — one regimen per line>
<may be (none); required to be (none) unless allows_dosing: yes>

## ANTIBIOTIC_CHOICES_ALLOWED
<the ONLY section whose drug names may be output as treatment choices>
<each line: one antibiotic or regimen>
<required to be (none) unless allows_antibiotic_choice: yes>

## LINKED_PROTOCOL_RULES
<conditions under which another protocol may be consulted; e.g.>
<"consult BioFire when patient is intubated">
<"consult ceftriaxone protocol when user explicitly asks for dose">

## SAFETY_NOTES
<warnings, monitoring, escalation triggers — LLM-visible context only>

## DEFAULT_FOOTER
<text appended by Python after the LLM response; may be (none)>

## FORBIDDEN_BEHAVIOUR
<explicit "do not" list; consumed both by the LLM prompt and the post-LLM gate>
<example: "do not give ceftriaxone dose">
<example: "do not apply renal adjustment">
<example: "do not use BioFire organism table unless BioFire result is explicitly mentioned">
```

### What is parsed by Python vs LLM-visible

**Parsed strictly by Python (used by gates / state machine, not in the LLM prompt verbatim):**

- `METADATA` (every field)
- `ALIASES` (all five lists)
- `DECISION_TREE`
- `DOSING_ALLOWED` (parsed into a normalized list of `{drug, dose_text}` for the dosing-gate matcher)
- `ANTIBIOTIC_CHOICES_ALLOWED` (parsed into a set of canonical drug names)
- `LINKED_PROTOCOL_RULES` (parsed into a list of `{condition, target_file, forwarded_params}`)
- `FORBIDDEN_BEHAVIOUR` (parsed into a list of regex/string rules the post-LLM gate enforces)
- `DEFAULT_FOOTER` (appended after the LLM response)

**Visible to the LLM (injected as part of the prompt):**

- `ANSWER_POLICY`, `REQUIRED_INFORMATION`, `PREFERRED_INFORMATION`, `MODIFIER_INFORMATION`, `DEFAULT_QUESTION`, `PATHWAY_PRIORITY`, `TREATMENT_PATHWAYS`, `SAFETY_NOTES`, `FORBIDDEN_BEHAVIOUR` (as guidance — but the gate, not the LLM, enforces it).

**Both:** `METADATA.source_label` (Python uses it for the Source line; LLM sees it so it does not invent a label).

---

## G. Refactor plan for `telegram_bot.py`

### G.1 `parse_protocol_file(path) -> ParsedProtocol`

Replaces and extends the current `_parse_protocol_text`. Required behaviour:

- Parse every canonical panel; missing panels = `warnings` entries, not silent zeroes.
- Parse `METADATA` into a typed dict — `protocol_type` and the two `allows_*` booleans are required.
- Parse `DOSING_ALLOWED` into `List[{drug, route, dose_text, normalized_dose}]`. Build a regex from each `normalized_dose` for the gate.
- Parse `ANTIBIOTIC_CHOICES_ALLOWED` into `Set[canonical_drug_name]`.
- Parse `LINKED_PROTOCOL_RULES` into `List[{condition, target_file, forwarded_params}]`.
- Parse `FORBIDDEN_BEHAVIOUR` into a list of patterns (string or regex).
- Validate at parse time and emit warnings; let `run_startup_checks` decide whether to error out.

### G.2 `classify_user_intent(text, state) -> IntentResult`

```
IntentResult {
  task_type:        "drug_dosing" | "microbiology_interpretation" | "syndrome_pathway"
                    | "general_question" | "vague_followup" | "unknown",
  protocol_type:    SYNDROME | MICRO | DOSING | GENERAL | None,
  entity_type:      "drug" | "condition" | "organism" | "resistance_marker" | "test_platform" | None,
  entity:           str | None,
  confidence:       "exact" | "high" | "medium" | "low" | "none",
  context_flags:    Set[str],   # "biofire_present", "pcr_present", "cap_present",
                                #  "drug_present", "dose_word_present", ...
}
```

Implementation follows the E.3 recognition hierarchy strictly. Returns `vague_followup` for `"what dose"`, `"mit adjak"`, `"adag"`, bare number+unit, etc. — these do **not** carry their own protocol identity.

### G.3 `select_active_protocol(intent, state) -> ActiveProtocolResult`

Deterministic, runs **before** retrieval.

- Drug intent → load drug protocol from `intent.entity`.
- Microbiology intent (organism + `test_result_present`, or resistance marker) → load microbiology protocol; record `entity.label`.
- Syndrome intent → load syndrome protocol; record `entity.label`.
- Vague followup → reuse `state.active_protocol_file` if compatible; otherwise emit clarification.
- Multiple matches with comparable confidence → emit a disambiguation question instead of guessing.

### G.4 `retrieve_within_protocol(query, active_protocol, linked_protocols) -> List[Chunk]`

Replaces the current global retriever:

- Default: cosine top-k restricted to `chunk.source_file in {active_protocol_file}`.
- If `LINKED_PROTOCOL_RULES` authorizes a linked protocol for this turn (based on `state.collected_parameters` or current intent), include its chunks too — but tag them as "linked" so the gate knows.
- Never include `GENERAL_RULES_PROTOCOL` chunks here. General rules ride along as a small fixed preamble in the prompt instead.

### G.5 `authorize_answer(raw_answer, active_protocol, linked_protocols, state) -> AuthorizedAnswer`

The post-LLM gate. Applies (in order):

1. **Antibiotic-name scan** (gate C.1). For every recognized drug name in `raw_answer`, check membership in `active_protocol.antibiotic_choices_allowed ∪ linked_protocols.*.antibiotic_choices_allowed`. Unauthorized names → strip or replace.
2. **Dose-bearing token scan** (gate C.2). For every dose pattern, attempt to match against `active_protocol.dosing_allowed ∪ linked_protocols.*.dosing_allowed` using normalized-string match. Unmatched dose lines → strip and replace with the "dosing not specified" sentence.
3. **Renal-adjustment / infusion-detail scan**. Same logic — only allowed if the active or linked DRUG_DOSING protocol owns it.
4. **Forbidden-behaviour scan**. Apply each rule from the active protocol's `FORBIDDEN_BEHAVIOUR`.
5. Record `last_authorized_antibiotic` and `last_authorized_dose_source` into `state`.
6. Return the rewritten answer plus a `blocked: [reason, ...]` audit list for `/debug`.

### G.6 `handle_dose_followup(message, state) -> Reply | None`

Specifically for vague-followup intents asking for dose:

- If `state.last_authorized_antibiotic` is set:
  - Look for a `DRUG_DOSING_PROTOCOL` whose `canonical_entities` include that drug.
  - If found → switch active protocol, run the normal pipeline (which will collect required params).
  - If not found → emit: `"<Drug> dosing is not specified in the uploaded protocol."`
- If `state.last_authorized_antibiotic` is unset:
  - Emit: `"I don't have an active dosing context. Which antibiotic?"`
- **Never** invent dose from CAP/BioFire chunks. The gate from G.5 will catch any model leak, but `handle_dose_followup` is the first line of defence.

### G.7 `/debug` output

Augment the existing `/debug` to render, in this order:

```
recognized_aliases:        [(token, category, entity, confidence, score), ...]
intent:                    {task_type, protocol_type, entity, confidence, context_flags}
selected_protocol_file:    protocols/<file>.txt
selected_protocol_type:    SYNDROME | MICRO | DOSING | GENERAL
active_state_before:       {protocol_file, task_type, last_authorized_antibiotic, ...}
retrieved_chunks:          [(source_label, similarity, file), ...]
linked_protocols_used:     [...]
allowed_antibiotics:       [...]
allowed_doses:             [(drug, dose_text), ...]
raw_llm_output:            <verbatim>
gate_blocks:               [(rule, matched_substring, replacement), ...]
active_state_after:        {...}
final_response:            <verbatim>
```

This makes any future regression trivially explainable from a single `/debug` line.

---

## H. Harness / test plan

Three layers — names match `test_bot.py` conventions for easy migration.

### H.1 Pure unit tests (no API)

Build on the existing `test_bot.py` sections 1–10. New cases:

- `test_protocol_schema_validation` — every protocol file declares all required METADATA fields and every required panel header.
- `test_protocol_type_invariants` — `allows_dosing=no` implies `DOSING_ALLOWED == (none)`; `allows_antibiotic_choice=no` implies `ANTIBIOTIC_CHOICES_ALLOWED == (none)`.
- `test_dose_token_detection` — `_HAS_DOSING_RE` and its extended cousins correctly flag `"2 g IV"`, `"napi 1x"`, `"q24h"`, `"3 x 4 amp"`, etc.
- `test_authorize_answer_strips_unauthorized_dose` — given a fake CAP parsed protocol (allows_dosing=no, DOSING_ALLOWED=(none)) and a raw LLM answer containing "Ceftriaxone 2 g IV daily", the gate replaces the dose with the canned "not specified" sentence.
- `test_authorize_answer_passes_authorized_dose` — given the meropenem parsed protocol and a raw answer `"meropenem 4 g/day, prolonged infusion"`, the gate passes it through.
- `test_recognition_hierarchy_organism_requires_context` — `"Strep pneumo"` alone returns intent `vague_followup` or `unknown`; `"Strep pneumo on BioFire"` returns microbiology intent.
- `test_alias_separation` — `"pneumonia"` alone does not auto-select CAP.
- `test_linked_protocol_rules` — when CAP is active and patient is intubated, BioFire chunks are authorized; when CAP is active and patient is dischargeable, BioFire chunks are not.
- `test_general_rules_never_active` — `select_active_protocol` cannot return the general-rules file even if it has the highest retrieval score.
- `test_state_followup_dose_no_drug_protocol` — state has `last_authorized_antibiotic="ceftriaxone"` and no `protocols/ceftriaxone.txt`; `handle_dose_followup` returns the canned "not specified" sentence.

### H.2 Golden conversation tests (deterministic, no API)

Use a fake LLM that returns scripted responses; assert the gate rewrites them correctly. One scenario per row from the bug log plus the additional cases requested:

| # | Conversation step | Fake LLM emits | Gate expected output |
|---|---|---|---|
| 1 | "What to give for pneumococus by Biofire" | "Streptococcus pneumoniae — Tier 1 — Ceftriaxone. Dose: 2 g IV q24h." | BioFire selected; "Ceftriaxone" passes (in `ANTIBIOTIC_CHOICES_ALLOWED`); dose stripped; "Dosing not specified in this protocol." appended. |
| 2 | "Strep pneumo" | <anything> | Intent = ambiguous; bot asks "Is this a BioFire/PCR result or a syndrome question?" — no CAP, no organism mapping. |
| 3 | "Pneumococcal pneumonia, what ab?" | <anything> | CAP selected; bot asks patient status (`DEFAULT_QUESTION`); no antibiotic listed yet. |
| 4 | After 3, user says "intubated" | "Intubated → BioFire pneumonia panel. Until results: ceftriaxone 2 g IV daily." | CAP intubated pathway; "ceftriaxone" passes; "2 g IV daily" — only passes if `cap.txt` DOSING_ALLOWED authorizes it; otherwise rewritten to "ceftriaxone, dose not specified". |
| 5 | "what dose?" with `last_authorized_antibiotic=ceftriaxone` and no ceftriaxone DRUG_DOSING protocol | <anything> | "Ceftriaxone dosing is not specified in the uploaded protocol." |
| 6 | "GFR 60, 50 kg" with CAP active and last antibiotic ceftriaxone | "Ceftriaxone 2 g IV daily for GFR 60." | All numeric dose tokens stripped; replaced with the canned "not specified" sentence. |
| 7 | "sumetrolim dose" | <anything> | TMP/SMX selected; bot asks indication + weight + GFR. |
| 8 | "Steno BSI 60 kg GFR 60" with TMP/SMX active | "3 x 4 amp per high-dose table" | Passes — matches TMP/SMX `DOSING_ALLOWED` (HIGH_DOSE_TABLE row for 60 kg). |
| 9 | "meropenem dose" | "Meropenem 4 g/day prolonged infusion" | Passes — meropenem DRUG_DOSING protocol authorizes it. |
| 10 | "BioFire: Strep pneumo + CTX-M" | "Tier 3 — Ertapenem (CTX-M positive)" | Conflict-detection branch in the BioFire tree fires, OR an explanation that CTX-M applies to Enterobacterales, not Strep pneumoniae; no invented coverage. |

The fake LLM is wired in via dependency injection in `ask_ai` so the integration test in H.3 reuses the same scenarios but with the real model.

### H.3 Integration tests (real OpenAI)

Same 10 scenarios from H.2, run against the live model. Additional invariants asserted on every response:

- No `protocols/` path in the output.
- Exactly one `Source: ` line at the end.
- No markdown bold (`**`).
- `source_label` of the Source line matches the active protocol.
- No dose-bearing token unless the gate audit log shows an authorized match.
- No antibiotic name unless the gate audit log shows an authorized match.
- For each "ask status / ask indication" expected case, the response contains the protocol's `DEFAULT_QUESTION` exact text.

Run integration tests on CI nightly, fast tests on every commit.

---

## I. Per-file migration plan

### I.1 `cap.txt` (currently SYNDROME-shaped but leaks dose)

- Add to METADATA:
  ```
  protocol_type: SYNDROME_PROTOCOL
  allows_antibiotic_choice: yes
  allows_dosing: no
  forbidden_outputs: ceftriaxone dose, levofloxacin dose, amoxicillin dose, clarithromycin dose, methylprednisolone dose, renal adjustment, infusion details
  ```
- Add `## ANTIBIOTIC_CHOICES_ALLOWED`:
  ```
  ceftriaxone
  clarithromycin
  levofloxacin
  amoxicillin
  azithromycin
  oseltamivir
  methylprednisolone
  ```
- Add `## DOSING_ALLOWED`:
  - **Option A (recommended):** `(none)`. Remove `"Ceftriaxone 2 g"` from `### INTUBATED_CAP` step 2 and replace with `"Ceftriaxone — see ceftriaxone dosing protocol"`. Remove the example "ceftriaxon 2 g" line. Remove `"methylprednisolone 0.5 mg/kg"` and replace with `"Methylprednisolone — see corticosteroid dosing protocol"` (or accept it as authorized; see option B).
  - **Option B:** Keep `Ceftriaxone 2 g IV daily` and `methylprednisolone 0.5 mg/kg` in `DOSING_ALLOWED`. Then those exact strings (and only those) survive the gate. This is the right call **only** if CAP truly intends to be the dose source for these drugs.
- Add `## LINKED_PROTOCOL_RULES`:
  ```
  ceftriaxone -> protocols/ceftriaxone.txt via: patient_status, renal_gfr, weight_kg
  clarithromycin -> protocols/clarithromycin.txt
  levofloxacin -> protocols/levofloxacin.txt
  pneumonia_pcr -> protocols/pneumonia_pcr.txt when: patient_status == "intubated" OR user mentions biofire/pcr/panel
  ```
- Add `## FORBIDDEN_BEHAVIOUR`:
  ```
  - Do not answer organism-only BioFire queries.
  - Do not provide ceftriaxone, levofloxacin, amoxicillin, clarithromycin, or oseltamivir dosing unless DOSING_ALLOWED contains it.
  - Do not apply renal adjustment.
  - Do not mix CAP pathway selection with BioFire tier mapping unless the user explicitly provided a BioFire result.
  ```

### I.2 `pneumonia_pcr.txt` (BioFire — needs schema upgrade)

- Add to METADATA:
  ```
  protocol_type: MICROBIOLOGY_INTERPRETATION_PROTOCOL
  allows_antibiotic_choice: yes
  allows_dosing: no
  ```
- Convert ALIASES into the new five-category form. Move organisms (`streptococcus pneumoniae` etc.) into the `organisms` block in `aliases.json`, **not** as protocol aliases.
- Add `## ANTIBIOTIC_CHOICES_ALLOWED`:
  ```
  ceftriaxone
  cefepime
  ertapenem
  meropenem
  colistin
  vancomycin
  cefazolin
  clarithromycin
  oseltamivir
  penicillin
  clindamycin
  ```
- Keep `## DOSING_ALLOWED: (none)`. The existing "Dózis: e protokollban nem szerepel" markers stay as belt-and-braces.
- Add `## LINKED_PROTOCOL_RULES`:
  ```
  patient_status -> protocols/cap.txt   when: user asks for CAP/VAP/intubation context
  meropenem      -> protocols/meropenem.txt
  ```
- Add `## FORBIDDEN_BEHAVIOUR`:
  ```
  - Do not produce dose amounts, frequencies, infusion durations, weight-based calculations, or renal-adjustment text.
  - Do not import CAP treatment pathways unless the user explicitly provided patient status.
  - Do not invent CTX-M coverage for non-Enterobacterales organisms.
  ```

### I.3 `tmpsmx.txt` (DRUG_DOSING — mostly correct)

- Add to METADATA:
  ```
  protocol_type: DRUG_DOSING_PROTOCOL
  allows_antibiotic_choice: yes
  allows_dosing: yes
  canonical_entities: trimethoprim/sulfamethoxazole
  ```
- Add `## ANTIBIOTIC_CHOICES_ALLOWED: trimethoprim/sulfamethoxazole`.
- Add `## DOSING_ALLOWED` populated from the existing `### STANDARD_DOSE`, `### MODERATE_DOSE`, `### HIGH_DOSE`, `### MODERATE_DOSE_TABLE`, `### HIGH_DOSE_TABLE`, `### RENAL_DOSING`, `### PROPHYLAXIS` sections. Each row of each weight table becomes a `DOSING_ALLOWED` line.
- Tree not required; current flat structure is fine.

### I.4 `meropenem.txt` (DRUG_DOSING)

- Add to METADATA:
  ```
  protocol_type: DRUG_DOSING_PROTOCOL
  allows_antibiotic_choice: yes
  allows_dosing: yes
  canonical_entities: meropenem
  ```
- Add `## ANTIBIOTIC_CHOICES_ALLOWED: meropenem`.
- Add `## DOSING_ALLOWED` with every dosing tier currently in the file (MAGAS, STANDARD, prolonged infusion variants, renal-adjusted rows).
- `## DEFAULT_FOOTER` stays Python-appended.

### I.5 `ampsul.txt` (DRUG_DOSING + decision tree)

- Add to METADATA:
  ```
  protocol_type: DRUG_DOSING_PROTOCOL
  allows_antibiotic_choice: yes
  allows_dosing: yes
  canonical_entities: ampicillin/sulbactam
  ```
- Keep existing `DECISION_TREE`.
- Move MACI confirmation and renal-function classification to deterministic tree nodes (currently leaning on `_classify_branch` LLM call — make these explicit branches based on collected parameters where possible).
- Add `## ANTIBIOTIC_CHOICES_ALLOWED: ampicillin/sulbactam, sulbactam`.
- Add `## DOSING_ALLOWED` with every tier from the existing file.

### I.6 `general_rules_antibiotic_dosing.txt`

- Add to METADATA:
  ```
  protocol_type: GENERAL_RULES_PROTOCOL
  allows_antibiotic_choice: no
  allows_dosing: no
  ```
- `## ANTIBIOTIC_CHOICES_ALLOWED: (none)`.
- `## DOSING_ALLOWED: (none)`.
- `select_active_protocol` is hard-wired never to return this file. It contributes only a small fixed preamble of constraints to the prompt.

---

## J. Startup validation

`run_startup_checks` extends to fail loudly (exit 1) when:

- Any protocol file lacks a required panel header (one error per missing panel).
- `protocol_type` missing from METADATA.
- `allows_dosing: yes` but `DOSING_ALLOWED` is empty or `(none)`.
- `allows_dosing: no` but `DOSING_ALLOWED` is non-empty.
- `allows_antibiotic_choice: yes` but `ANTIBIOTIC_CHOICES_ALLOWED` is empty or `(none)`.
- `allows_antibiotic_choice: no` but `ANTIBIOTIC_CHOICES_ALLOWED` is non-empty.
- A `SYNDROME_PROTOCOL` or `MICROBIOLOGY_INTERPRETATION_PROTOCOL` contains dose-looking text (`_HAS_DOSING_RE` matches) **outside** `DOSING_ALLOWED`. This is the check that would have flagged `cap.txt`'s "Ceftriaxone 2 g" today.
- `aliases.json` points to a missing protocol file.
- Two aliases in different categories collide without an explicit disambiguation rule (e.g., `"strep pn"` in both `organisms` and `tests_platforms`).
- `source_label` missing from METADATA.
- `DEFAULT_FOOTER` missing — must explicitly be `(none)`.
- Any `LINKED_PROTOCOL_RULES` entry points to a missing file.
- A protocol declares itself `MICROBIOLOGY_INTERPRETATION_PROTOCOL` but has no `DECISION_TREE` or `ORGANISM_TIER_MAPPING`-style content.

Print the full error list and exit with code 1. This is consistent with the existing pattern in `run_startup_checks` (lines ~200–210).

---

## K. Security note

The Railway log you supplied contains the live Telegram bot token in the visible URL (`/bot8862105492:AAGPEIsyR-GfC7C2TrKo3Kd7wXepfCC5A2c/...`). Anyone with this token can fully control the bot — read all messages, send messages as the bot, change the webhook, drain message history.

Immediate actions:

1. **Rotate the token in BotFather right now.** `/revoke` then `/token` on @BotFather to issue a new one.
2. **Update `TELEGRAM_TOKEN` in Railway → Variables.** The bot will pick up the new value on next deploy / restart.
3. **Avoid logging full Telegram API URLs.** Replace the default `httpx`/`python-telegram-bot` request logger with one that redacts the bot token from any URL it logs. Concretely: add a logging filter that runs `re.sub(r'bot\d+:[A-Za-z0-9_-]+', 'bot<redacted>', record.msg)` on every emitted record before it reaches the handler. Apply this to the root logger before `setup_logging()` returns.
4. **Treat any shared log file as compromised until the token is rotated.** Search your chat history, ticketing systems, and any file sharing where this log might have been posted, and verify nothing else (OPENAI_API_KEY, ALLOWED_USER_IDS) was leaked alongside.
5. **Make this a routine.** Add a CI lint step that grep-blocks any pattern like `bot\d{6,}:[A-Za-z0-9_-]{30,}` from being committed.

This is independent of the architectural refactor and should happen today.

---

## L. Deliverables summary and implementation order

### L.1 Architecture diagnosis
See section A.

### L.2 Proposed protocol schema
See section F.

### L.3 Revised state model
See section D.

### L.4 Routing and authorization algorithm
Recognition hierarchy (section E.3) → `select_active_protocol` (G.3) → `retrieve_within_protocol` (G.4) → LLM call → `authorize_answer` (G.5) → footer append → response. Vague follow-ups skip recognition and go through `handle_dose_followup` (G.6).

### L.5 Per-file migration plan
See section I.

### L.6 Test / harness plan
See section H.

### L.7 Implementation order

The order is chosen so each step is independently testable and reversible. Do not skip the validation step before refactoring code — it forces clean inputs.

1. **Rotate the Telegram token** (section K). Five-minute task, blocks nothing.
2. **Define the schema** (section F) on paper; lock the panel list and METADATA fields before touching any file.
3. **Migrate the protocol files** (section I) one at a time, starting with `general_rules_antibiotic_dosing.txt` (lowest risk) and ending with `cap.txt` (highest risk). Each migration is a separate PR. Run the existing fast tests after each.
4. **Extend `parse_protocol_file`** (G.1) and **strengthen `run_startup_checks`** (section J). The bot still works the same after this step; only validation is stricter.
5. **Add `DOSING_ALLOWED` and `ANTIBIOTIC_CHOICES_ALLOWED` parsing**. No gate yet — just parsing into `PROTOCOL_PARSED_BY_FILE`. Unit-test the parser exhaustively before the gate uses it (H.1).
6. **Reorganize `aliases.json`** into five categories (E.1) and **rewrite `normalize_question` into the recognition hierarchy** (E.3 + G.2). At this point, "Strep pneumo" returns `vague_followup`/`unknown` instead of CAP, and "What dose?" no longer routes anywhere.
7. **Implement the revised state model** (section D). Wire `select_active_protocol` (G.3) to set it.
8. **Implement `retrieve_within_protocol`** (G.4) with protocol-scoped filtering. Cross-protocol contamination is gone from this point on.
9. **Implement `authorize_answer`** (G.5). This is the gate that fixes turn 5 of the log. Land it with extensive unit tests (H.1) and the deterministic golden tests (H.2). Keep a feature flag for one week so you can toggle it back if a false-positive blocks a clinical use case.
10. **Implement `handle_dose_followup`** (G.6).
11. **Augment `/debug`** (G.7). Critical for triaging any future regression.
12. **Run the full golden conversation suite** (H.2) against deterministic fake LLM responses.
13. **Run the integration suite** (H.3) against the real model. Hold the release until all 10 scenarios pass.
14. **Re-enable rapidfuzz on Railway** (`pip install rapidfuzz` in the deploy step — the production log shows it missing). The recognition hierarchy in step 6 is more sensitive without fuzzy; this is the moment to enable it.
15. **Add the token-redaction logging filter** (section K item 3) before re-enabling verbose logs.

Sections A–L are the contract; the implementation will not deviate without an updated proposal.
