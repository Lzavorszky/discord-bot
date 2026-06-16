# ID Bot — Strategic and Technical Review

*Prepared from a read of the code (`bot_core.py`, `routing.py`, `selection_engine.py`, `retrieval.py`, `protocol_parser.py`, `postprocess.py`, `config.py`, `aliases.py`), the 48 protocol files and their route-claim sidecars, the rule files, the test suite, the routing architecture doc, and the runtime log (`totallog.txt`).*

---

## 1. Executive summary

The instinct behind ID Bot is sound: do the dangerous, exact things (doses, tables, calculators, PCR maps) in deterministic code, and let the LLM only explain and format protocol text it has been shown. That division is the right one and you should keep it.

The problem is not the strategy. It is that **the strategy was implemented as an ever-growing pile of special cases instead of a small set of general mechanisms.** Routing decisions are made in at least three different places that can disagree; knowledge about each protocol is spread across five or six file types and several hardcoded Python tables; two protocol schemas and two routing systems run in parallel; and the main answer function is a single 560-line cascade of ~15 short-circuit gates, each with its own copy of the bookkeeping. The result behaves exactly as you describe: every fix is local, nothing gets globally better, and the configuration gets more complex with each protocol rather than less.

The good news is that the foundations you would want for a clean redesign already exist in the codebase — a structured audit log, 331 tests, a protocol authoring guide, an embeddings cache, and a safety post-processor. The path forward is mostly **consolidation and inversion of control**, not a rewrite.

My headline recommendations:

1. **Collapse routing to one mechanism and one decision point.** Today you have route-claims, the legacy alias/longest-match path, and the orchestrator's own gates all live at once.
2. **Make each protocol a single validated structured file.** Retire the `.txt` + `.route_claims.json` split, the dual old/new schema, and the hand-rolled DSL parser.
3. **Move the routing tables out of Python.** Microbe, marker, steroid and calculator patterns currently live as regex constants in `routing.py`; they are configuration pretending to be code.
4. **Upgrade the chat model — but treat it as a quality lever, not the fix.** `gpt-4o-mini` is a weak model for this task. Upgrading will reduce some comprehension and hallucination complaints, but it will not fix the deterministic bugs that cause most of your logged failures.
5. **Build a regression harness and turn every "debug note" into a test.** You already capture the failures; you are just not yet capturing them *as assertions*.

---

## 2. Diagnosis of the current system

### 2.1 What it is, structurally

The request lifecycle is roughly:

```
message
  -> access check / state load
  -> pending-confirmation handling (out-of-bounds, context, route clarification)
  -> shadow route decision (routing.resolve_route over route_claims)
  -> unsupported-syndrome policy
  -> alias recognition + implicit recognition
  -> ~15 sequential deterministic "shortcut" gates:
       reset / admin-note / fuzzy-confirm / nonclinical /
       organism-disambiguation / dosing-shortcut / tree dispatch /
       steroid-equivalence / periop-info / deterministic selection engine
  -> if nothing fired: RAG (top-3 chunks) + policy header + LLM answer
  -> post-process (strip markdown, kill file paths, add source label + footer)
```

Knowledge about a single protocol is currently spread across:

- the protocol `.txt` (a bespoke ~12-panel DSL: `METADATA`, `INTENTS`, `INPUT_SLOTS`, `SELECTION_RULES`, `SELECTED_OUTPUTS`, `LINKS`, `INFO_BLOCKS`, `RESTRICTED_OUTPUTS`, `SAFETY_RULES`, `OUTPUT_TEMPLATES`, `DEFAULT_FOOTER` …),
- a separate `*.route_claims.json` sidecar,
- entries in `protocols/aliases.json` (435 aliases),
- sometimes hardcoded patterns in `routing.py` (e.g. the microbe/marker/steroid/calculator tables),
- sometimes bespoke logic in `selection_engine.py` (e.g. the TMP/SMX renal table, echo calculators),
- the five global rule `.txt` files.

So the honest answer to "where does the behaviour for protocol X come from?" is "six places, and you have to know which." That is the root cause of the debugging treadmill.

### 2.2 The core architectural problems

**(a) Routing has no single owner.** `routing.resolve_route` is a ~150-line cascade of ~15 ordered `if` branches returning `route / clarify / unsupported / use_active_context / fallthrough`. But that decision does **not** fully determine the answer: `_ask_ai_impl` then runs alias recognition, implicit recognition, and its own gate ladder, any of which can override or contradict the route decision. The architecture doc even says the legacy alias/longest-match fallback is "retained for ordinary non-conflict turns." You therefore have two routers (typed-evidence claims, and longest-alias) plus an orchestrator that can ignore both. When something routes wrong, you cannot point to one function that decided it.

**(b) The orchestrator is a 560-line straight-line function with duplicated bookkeeping.** Each of the ~15 gates in `_ask_ai_impl` repeats the same five-step ritual: build a `TurnContext`, build an `AnswerEnvelope`, `finalize_answer`, `_remember_answer`, `_log_answer_envelope`, `return`. This is why a change in one path silently diverges from the others, and why ordering bugs (a gate firing before the gate that should have caught the case) are common and invisible.

**(c) Two protocol schemas live simultaneously.** `protocol_parser.py` carries a full "old schema" (`ANSWER_POLICY`, `TREATMENT_PATHWAYS`, `DECISION_TREE`, `PROTOCOL_LINKS`…) *and* a "new schema" (`INTENTS`, `SELECTION_RULES`, `SELECTED_OUTPUTS`, `LINKS`…). Every protocol read pays the cost of both, and authors must know which panels are canonical. The parser is 949 lines of hand-rolled indent-sensitive grammar — effectively a bespoke YAML you have to maintain forever.

**(d) Configuration grows superlinearly.** Adding one protocol can mean: a new DSL file, a new sidecar JSON, several alias entries, possibly new regex constants in `routing.py`, possibly new bespoke code in `selection_engine.py`, and new tests. Your stated goal — config should get *simpler* over time — is structurally impossible under this layout.

**(e) Routing logic is encoded as Python constants.** `_MICROBE_PATTERNS`, `_MARKER_PATTERNS`, `_STEROID_DRUG_PATTERNS`, `_CALCULATOR_PROTOCOL_IDS` and the many `_*_INTENT_RE` regexes in `routing.py` are domain data. Every new organism, marker, or calculator is a code change plus a deploy, not a data edit.

---

## 3. Where the brittleness actually comes from (evidence from the log)

The runtime log is the most useful artefact in the repo, because it contains your own real-time debug notes. The failures cluster into five families, and **only one of them is an LLM problem.**

**(1) Protocol-switch friction — the biggest usability failure.**
`"Mi a meropenem dózisa?"` arrived while TMP/SMX was the active protocol. The bot refused to switch and asked a yes/no confirmation; the user answered `"No"`, then `"New patient"`, then re-asked — three turns to change drug. Your own note: *"what prevents changing protocol when a clearly new protocol has been called and the previous protocol has no variables anyway (like kg cm etc)."* This is the `_looks_like_active_protocol_followup` / `pending_context_confirmation` gate being far too sticky. A high-confidence exact alias for a *different* protocol should win immediately, especially when the active protocol holds no patient slots.

**(2) Deterministic selection picking the wrong output.**
`"Tmpsmx high dose"` returned the **PROPHYLAXIS** tier, not high dose. `"dózis kérsz is always there?"` flags that the bot always asks for `indication` even when intent is obvious. The selection rules are authored as text and matched by hand-parsed conditions; the matching is fragile and silently falls through to the wrong tier.

**(3) Alias collisions.**
`"Tazobactam dose"` routed to **ceftolozane/tazobactam** instead of piperacillin/tazobactam. Your note: *"why ceftoltaz not piptaz?"* This is longest/first-match alias resolution with no tie-break and no disambiguation prompt.

**(4) PCR / panel logic — several distinct defects.**
- `"JiPCR Klebsiella"` → *"Klebsiella ignored in answer"* and *"should clarify which Klebsiella"* (K. pneumoniae group vs K. oxytoca not disambiguated).
- `"JiPCR panel list"` → returned a priority/recommendation instead of the panel contents (*"could provide panel list, not priority"*).
- `"Pneumonia PCR influenza"` → influenza apparently not recognised on the panel (*"pretty sure there iflu"*).
- `"Mycoplasma"` → *"spectrum logic: empty, why ceftriaxone?"* (an unexplained recommendation with empty spectrum reasoning).
- A targeted PCR organism already on meropenem still triggered an "upgrade" suggestion (*"doesn't make sense to suggest upgrade when already on mero"*).

**(5) Grounding / output-quality leaks.**
The perioperative answers render the protocol's INFO_BLOCK *header* verbatim, so the ASA answer literally begins `"Aspirin; acetylsalicylic acid; ASA; acetilszalicilsav; Acizalep; Asactal; Astrix; Kardegic:"` — the entire alias chain dumped as a heading. Separately you noted *"no info in protocols re ASA dózis"* yet an answer was still produced, and once flagged *"gross hallucinations."* Encoding is also suspect — Hungarian text appears as mojibake (`d├│zisa`), which means accent-sensitive regexes may be matching inconsistently across environments.

**The pattern:** four of the five families are deterministic-code bugs — routing stickiness, selection mismatch, alias tie-breaks, PCR mapping. They will not improve with a better model, more route claims, or more aliases. They improve only when the mechanisms are made general and centralised.

---

## 4. Answers to your ten questions

**1. Main architectural problems?** No single routing owner; a 560-line orchestrator with duplicated per-gate bookkeeping; two protocol schemas and two routers running at once; knowledge fragmented across six file/code locations; routing data hardcoded in Python. (§2)

**2. Where is the brittleness from?** Special-case accretion. Each fix added a gate, a claim, an alias, or a constant rather than improving a mechanism, so behaviour depends on the *order and interaction* of dozens of narrow rules that no one can hold in their head. (§3)

**3. Is the mix of deterministic routing + parsing + embeddings + LLM right?** The *categories* are right; the *implementation* is not. Keep: deterministic doses/tables/calculators, LLM-for-prose, retrieval grounding. Change: make routing declarative and singular, make protocols one structured artefact, and generalise the selection engine instead of writing per-protocol Python.

**4. Would a different model help, or is it system design?** Mostly system design. A stronger chat model than `gpt-4o-mini` is a cheap, worthwhile upgrade that will reduce the "doesn't understand the question" and "hallucination" complaints and could later absorb intent/slot extraction. But it cannot fix protocol-switch friction, wrong-tier selection, alias collisions, or PCR mapping — those are in your code. **Upgrade the model, but do not expect it to be the fix.**

**5. Other architectures?** See §6 — conservative refactor, LLM-as-router with deterministic tools, structured-RAG-with-verifier, and a recommended hybrid.

**6. How to guarantee protocol-grounded facts?** Today grounding rests on the system prompt ("answer only from excerpts") plus injecting the policy header. That is necessary but weak: blank-line chunking up to 900 chars can split tables and rules; `top_k=3` can miss the right chunk; nothing checks that the answer's claims are actually in the supplied context; and deterministic paths can emit non-answers (ASA). Strengthen with (a) section-level retrieval over structured protocols instead of free-text chunks, (b) a **grounding verifier** pass that removes any dose/drug/number in the answer that does not appear in the supplied context, and (c) keeping every dose behind a deterministic tool the LLM must call rather than generate. (§7)

**7. Would a better debug/test cycle help, and what should it look like?** Yes — this is your highest-leverage near-term investment. See §8.

**8. What to simplify, remove, or replace?** §9.

**9. What to keep?** §10.

**10. Broad paths forward?** §6, with a phased plan in §11.

---

## 5. Biggest risks and failure modes

Ranked by clinical severity × likelihood:

| Risk | Severity | Where it comes from |
|---|---|---|
| **Silently wrong dose/tier** (e.g. high-dose request → prophylaxis) | Critical | Selection-rule text matched by fragile hand-parsing; no assertion that selected tier matches requested tier |
| **Wrong protocol selected** (tazobactam → ceftoltaz; Klebsiella ignored) | High | Longest/first-alias match with no tie-break or disambiguation |
| **Stale active-context bleed** | High | Sticky `active_recognized` + over-eager `use_active_context`; new protocol cannot displace it cleanly |
| **Ungrounded content emitted** (ASA "answer", "gross hallucinations") | High | Deterministic path renders info-blocks that aren't true answers; no grounding verifier on LLM path |
| **Over-blocking / refusal friction** | Medium (usability, erodes trust) | Confirmation gates and unsupported-syndrome policy firing on legitimate turns |
| **Schema drift** between `.txt` and `.route_claims.json` | Medium | Two files, two parsers, no enforced consistency |
| **Locale/encoding bugs** in accent-sensitive regexes | Medium | Hungarian mojibake in logs; matching may differ by environment |
| **Single 4885-line module** | Medium (maintainability) | Any change risks unrelated paths; hard to test in isolation |

The first two are the ones that matter clinically. Everything in the plan below is ordered to protect against *silently wrong but confident* output first.

---

## 6. Candidate future architectures

### Option A — Conservative refactor (keep the design, remove the entropy)
Unify routing into one module returning one decision; data-drive the regex constants; flatten the orchestrator into a list of named handlers sharing one envelope/return path; delete the old schema.
- **Pros:** lowest risk; behaviour-preserving; immediately reduces the debugging treadmill.
- **Cons:** does not lower the fundamental complexity ceiling — you still maintain a bespoke DSL and per-protocol selection code.

### Option B — LLM-as-router with deterministic tools
A strong model does intent classification, slot extraction, and protocol selection via constrained function-calling. The deterministic engines (dose tables, calculators, PCR maps) become **tools the model must call** — the model may *never* emit a dose itself, only call `get_dose(...)`. Route-claims survive only as tool-selection metadata.
- **Pros:** deletes the regex/alias routing cascade and most of `routing.py`; handles paraphrase, code-switching (HU/EN), and protocol switching naturally; new protocol = new tool + data, no router edits.
- **Cons:** routing now depends on the model; mitigate with tight tool schemas, the grounding verifier, and shadow-mode comparison before cutover.

### Option C — Structured RAG with a grounding verifier
Each protocol becomes structured data; retrieval returns whole relevant sections; the LLM answers; a verifier strips any ungrounded claim. Calculators/doses still deterministic tools.
- **Pros:** simplest config story; far fewer code paths; strong grounding guarantee.
- **Cons:** less precise control over multi-step pathway logic than explicit tools; pure-RAG can still mis-rank.

### Option D — Recommended hybrid
**B's tool-calling router + C's structured protocols + your existing deterministic engines exposed as tools + a grounding verifier.** Keep route-claims purely as tool metadata. Keep the deterministic selection/calculator code (it's correct where it's tested) but call it as tools rather than threading it through 15 orchestrator gates.
- **Pros:** preserves clinical safety (doses still deterministic), removes the brittle routing cascade, makes config additive and declarative, and gives the LLM exactly the job it's good at (understanding messy clinical phrasing and formatting bounded content).
- **Cons:** the largest design change; must be rolled out behind a flag with the regression harness proving parity.

---

## 7. Guaranteeing protocol grounding (concretely)

1. **Never let the model produce a number that wasn't given to it.** All doses, tiers, durations, and thresholds come from deterministic tools or verbatim protocol text. The model's job is selection-via-tool-call and prose, never arithmetic or recall.
2. **Add a grounding verifier** after generation: tokenise drug names and numeric+unit spans in the answer; assert each appears in the supplied context (tool outputs + retrieved sections); strip or flag any that don't, and log the event. This directly catches "gross hallucinations" before the user sees them.
3. **Retrieve sections, not blind 900-char chunks.** Because protocols become structured, retrieval can return whole `SELECTED_OUTPUTS`/`INFO_BLOCKS` sections, so tables and rules are never split mid-way.
4. **Keep the existing post-processor** (file-path stripping, single source label) — it's good — but move the source label to come from the structured protocol record, not from re-parsing the header text.

---

## 8. Testing, observability, and the debugging cycle

You are closer than you think — you have 331 tests and a genuinely good structured audit envelope (`deterministic_or_llm`, `blocked_reason`, route trace, retrieved chunks). The missing piece is a **closed loop**.

**Build a golden-question regression harness.** `test_questions.md` already exists; turn it into machine-checked cases. Each case asserts, at minimum: the route decision kind, the selected protocol, and — for deterministic answers — the exact rendered output (tier, dose). Run it in CI on every change. This converts "I fixed meropenem but did I break TMP/SMX?" from a Telegram session into a 10-second test run.

**Turn every debug note into a test.** Your log already contains the failing cases verbatim. `"Tmpsmx high dose" → expect HIGH_DOSE tier`, `"Tazobactam dose" → expect piperacillin/tazobactam OR a disambiguation prompt`, `"Mi a meropenem dózisa?" while TMP/SMX active → expect immediate switch`. Each becomes a red test you make green. This is how the system finally starts improving globally.

**Build a log-replay tool.** Feed logged user turns back through the pipeline offline and diff the new decision/answer against the recorded one. This lets you refactor routing aggressively and *prove* you didn't regress real traffic — essential before the Option D cutover (run new router in shadow mode, diff against old).

**Surface the trace you already compute.** `build_debug_trace` and `_inspect_deterministic_path` produce exactly the "why did it route here" view you need. Make `/debug` (or an admin view) show: evidence extracted → route decision + reason → gate that fired → tool calls → grounding-verifier result. Most of this is already logged; it just needs to be presented as one coherent trace.

**Define one metric:** turn-level pass rate on the golden set, split by deterministic vs LLM. Watch it move. Without a number, "is the system getting better?" stays a feeling — which is precisely the trap you're in now.

---

## 9. What to simplify, remove, or replace

- **Replace** the `.txt` DSL + `.route_claims.json` sidecar with **one structured file per protocol** (YAML or JSON, schema-validated), with the LLM-facing prose as a field. One file, one parser, one source of truth.
- **Remove** the old protocol schema from `protocol_parser.py` once protocols are migrated. Carrying two grammars is pure tax.
- **Collapse** the two routers (route-claims + legacy longest-alias) and the orchestrator's gate ladder into **one decision function** plus a small registry of handlers that share a single envelope/return path.
- **Move** `_MICROBE_PATTERNS`, `_MARKER_PATTERNS`, `_STEROID_DRUG_PATTERNS`, `_CALCULATOR_PROTOCOL_IDS`, and the intent regexes out of `routing.py` into protocol/alias data.
- **Generalise** `selection_engine.py`: you already have the right primitives (priority_rules, table_lookup, decision_tree, pcr_mapping, calculator). Drive them entirely from protocol data so a new protocol of a known type needs **zero** new Python.
- **Fix the periop renderer** so it shows a clean drug name, not the alias chain — but the real fix is that this is a symptom of free-text info-blocks being used as answers; structured records remove it.
- **Add alias tie-breaking + disambiguation** as a first-class mechanism (the tazobactam and Klebsiella cases).
- **Loosen protocol-switch stickiness:** an exact, high-confidence alias for a different protocol should switch immediately when the active protocol holds no patient slots.

## 10. What to keep

- The **deterministic-for-calculations, LLM-for-prose** philosophy.
- The **structured audit envelope** and logging — this is a real asset; build the harness on top of it.
- The **test suite** (331 tests) and the discipline behind it.
- The **post-processing safety layer** (no file paths, single source label, footer).
- The **protocol structure guide** — good authoring discipline; it becomes the schema doc.
- The **unsupported-syndrome / not-claimed safety concept** (HAP/VAP blocking) — keep the *idea*, move it to data.
- The **embeddings cache** and the preferred-file retrieval bias.

---

## 11. Phased plan (less brittle without losing safety or grounding)

**Phase 0 — Instrument and freeze (≈1–2 weeks). No behaviour change.**
Build the golden-question harness from `test_questions.md` + every debug note in the logs. Build the log-replay/shadow tool. Upgrade the chat model and measure the delta on the golden set. Establish turn-level pass rate as the single metric. *Exit criterion: you can change routing and know within seconds whether real traffic regressed.*

**Phase 1 — Conservative consolidation (Option A). Low risk, guarded by Phase 0.**
Unify routing into one decision function; flatten the orchestrator into shared-envelope handlers; data-drive the regex constants; delete the old schema. Fix the two clinically-relevant bugs first (wrong-tier selection, alias collision) and the switch-friction bug. *Exit criterion: pass rate up, no regressions, `routing.py` contains no domain data.*

**Phase 2 — One structured file per protocol.**
Migrate protocols to a single schema-validated format; retire the sidecar; ship a loader+linter that fails CI on malformed or inconsistent protocols. Configuration is now additive and declarative. *Exit criterion: adding a known-type protocol requires no Python.*

**Phase 3 — Tool-calling router behind a flag (move toward Option D).**
Expose the deterministic engines as tools; introduce the LLM router that may select protocols and call tools but never emit doses. Run in **shadow mode** against the current router, diffing on replayed traffic until parity. Flip the flag only when the harness says it's at least as good. *Exit criterion: shadow parity on the golden set + replayed logs.*

**Phase 4 — Grounding verifier + section retrieval; decommission legacy.**
Add the verifier pass and section-level retrieval. Once the new path proves out, delete the legacy alias/longest-match fallback. *Exit criterion: zero ungrounded-number escapes on the golden set; one routing path remains.*

---

## 12. The one-paragraph version

ID Bot's strategy is right and its safety instincts are good; what's broken is that the strategy is expressed as hundreds of interacting special cases spread across six kinds of files, with three things deciding routing and a 560-line function gluing it together. Four of your five logged failure families are deterministic-code bugs, not model bugs — so a better model helps quality but won't fix them. Spend the next two weeks turning your already-excellent logs into a regression harness, then consolidate routing into one declarative mechanism, make each protocol a single validated file, and move doses behind tools the model must call rather than generate. Do that and the configuration finally starts shrinking, the debugging loop closes, and clinical grounding gets *stronger*, not weaker.
