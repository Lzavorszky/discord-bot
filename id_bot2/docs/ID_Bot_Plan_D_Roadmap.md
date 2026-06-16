# ID Bot — Plan D Roadmap (clean rebuild, provider-swappable, small steps)

This is the build plan for the Option D revamp: structured protocols + a tool-calling LLM router + deterministic engines exposed as tools + a grounding verifier. It is paced for limited time per session — every step is small, self-contained, and **leaves a working artefact** (the live bot keeps running untouched until the final cutover).

---

## Locked decisions

| Decision | Choice | Consequence for the plan |
|---|---|---|
| LLM (now) | **GPT-5.5** for the router; mini/nano for pure phrasing | Keep `openai` client; trial 5.5 (~2–3¢/turn, ~$60–90/mo worst case at 100 turns/day, less with caching) |
| LLM (later) | Keep DeepSeek/Claude/GPT-5.4 as options | **Provider-agnostic LLM boundary**; dropping router to 5.4 if parity holds is a config flip |
| Messaging | Stay on Telegram | Add a thin **`Channel` adapter** seam so WhatsApp is a later adapter, not a rewrite |
| Migration | Clean rebuild, then cut over | New package `id_bot2/`; old bot stays live; one switch at the end |
| Protocol format | YAML + JSON-Schema validation | One file per protocol; loader rejects invalid files in CI |
| Cadence | Many small steps; time-limited | Phases are decomposed into ≤1-session tasks with explicit "done when" |

---

## Design principles (the rules the rebuild obeys)

1. **One decision point.** All routing is the LLM choosing one tool. No regex cascade, no 15 gates, no longest-alias fallback.
2. **The model never emits clinical facts.** Doses, tiers, organism therapies, thresholds come only from tool results. The model selects, asks, and phrases.
3. **One file per protocol.** YAML, schema-validated, carrying both data and routing metadata. No sidecars, no dual schema.
4. **Deterministic core is reused, not rewritten.** The tested `selection_engine` logic becomes the body of the tools.
5. **The LLM is swappable.** Everything model-specific sits behind one interface (`LLMProvider`) with `chat()` and `call_with_tools()`. OpenAI is the first implementation; DeepSeek/Claude are drop-ins.
6. **Nothing ships unmeasured.** The regression harness exists before the rebuild and guards every step.
7. **Slim conversation memory.** State carries only `history`, `active_protocol_id`, and a typed `slots` dict. The LLM handles clarification/confirmation conversationally — no `pending_*` flags. Slots are server-side typed values (doses are computed from validated numbers, not from the model re-reading history).
8. **Thin messaging seam.** A `Channel` adapter isolates Telegram so a future channel (WhatsApp) is an adapter swap.

---

## Target architecture (recap, one diagram)

```
message + chat state
   -> LLMProvider.call_with_tools(router_prompt, tools)        # the single decision
        tools the model may call:
          get_dose(drug_id, gfr?, crrt?, ihd?, ...)            # kind: drug_dose
          interpret_pcr(panel, organisms[], markers[])         # kind: pcr_panel
          select_pathway(protocol_id, slots)                   # kind: pathway
          answer_from_section(protocol_id, section, lang)      # kind: prose (the C path)
          list_panel(panel) / ask_clarification(text)
   -> tool result (verbatim clinical data)
   -> LLMProvider.chat(): phrase the result in user's language
   -> grounding verifier: strip any dose/drug/number not in tool output
   -> post-process (source label, footer)  [reuse existing]
   -> audit envelope  [reuse existing]
```

---

## Proposed fixes catalogue (every logged failure → where D resolves it)

These are the concrete defects from the logs/code, mapped to the step that fixes them so nothing gets lost in the redesign.

| # | Symptom (from logs) | Root cause | Resolved by |
|---|---|---|---|
| F1 | `Tazobactam dose` → wrong drug (ceftoltaz not piptaz) | longest/first alias match, no tie-break | `get_dose` closed enum + `ask_clarification` on ambiguity (Phase 3) |
| F2 | `Mi a meropenem dózisa?` refused to switch off TMP/SMX | over-sticky `active_recognized` + confirmation gate | LLM router owns switching; rule "confirm only if active protocol holds unsaved slots" (Phase 3) |
| F3 | `Tmpsmx high dose` → returned PROPHYLAXIS | fragile text-matched selection rules | declarative `select:` ladder + regression test asserting tier (Phase 2/5) |
| F4 | `dózis kérsz` always asks indication | required-slot logic too eager | slot schema with true vs optional requirements per protocol (Phase 2) |
| F5 | `JiPCR Klebsiella` → organism ignored / which species? | free-text PCR parse drops args | `interpret_pcr` required validated args + `disambiguate_genus` (Phase 3) |
| F6 | `JiPCR panel list` → gave priority not the list | no panel-list path | dedicated `list_panel` tool (Phase 3) |
| F7 | `Pneumonia PCR influenza` → influenza missed | panel membership not modelled cleanly | organisms/markers as structured panel data (Phase 2) |
| F8 | `Mycoplasma` → "spectrum logic empty, why ceftriaxone?" | opaque mapping fallthrough | mapping returns explicit reason or "not on panel" (Phase 3) |
| F9 | ASA periop → alias chain dumped as heading | info-block header rendered as answer | `kind: prose` + `answer_from_section`; aliases are metadata (Phase 2/3) |
| F10 | "no info in protocols re ASA" yet answered | deterministic path emitted a non-answer | grounding verifier + explicit "not covered" path (Phase 4) |
| F11 | "gross hallucinations" | LLM free-generating from weak context | model never emits facts; verifier backstop (Phase 3/4) |
| F12 | Hungarian mojibake (`d├│zisa`) | encoding/locale in regex matching | UTF-8 normalisation at input boundary; accent-folding util (Phase 1) |
| F13 | "what stops protocol change when new one called" | config/code split across 6 places | one file per protocol, one router (Phases 2–3) |

---

## Roadmap

Each phase is a sequence of small tasks. **"Bot status"** notes confirm the live bot is unaffected until Phase 6.

### Phase 0 — Safety net & scaffolding *(do first; unblocks everything)*
Bot status: live bot untouched.

- 0.1 Create `id_bot2/` package skeleton (empty modules, README of the target architecture).
- 0.2 Build the **regression harness**: load `test_questions.md` + every debug-note case from the logs into a table of `{input, expected_route_or_tool, expected_output_substring}`; a runner that reports pass rate.
- 0.3 Build the **log-replay tool**: feed recorded user turns through any pipeline and diff outputs. (Used later for shadow comparison.)
- 0.4 Add UTF-8 normalisation + an accent-folding helper at the input boundary (fixes F12 everywhere downstream).
- 0.5 Pick & wire the stronger OpenAI model in config; record baseline pass rate of the **current** bot on the harness.
- *Done when:* harness runs in CI, prints a number, and the current bot has a recorded baseline.

### Phase 1 — The LLM boundary *(the swappability seam)*
Bot status: live bot untouched (new code unused).

- 1.1 Define `LLMProvider` interface: `chat(messages) -> str` and `call_with_tools(messages, tools) -> ToolCall|str`.
- 1.2 Implement `OpenAIProvider`.
- 1.3 Add a `tools` abstraction (name, JSON-schema params, handler) that maps cleanly to OpenAI function-calling *and* to DeepSeek/Anthropic shapes.
- 1.4 Write a contract test the provider must pass (given a prompt + 2 fake tools, it calls the right one with valid args). This test is what any future provider (DeepSeek/Claude) must also pass.
- *Done when:* a throwaway script routes a toy query through `OpenAIProvider.call_with_tools` and picks the right fake tool.

### Phase 2 — Structured protocols *(this is the "C foundation" — config gets simpler here)*
Bot status: live bot untouched.

- 2.1 Write the **protocol JSON Schema** for the four kinds (`drug_dose`, `pcr_panel`, `pathway`, `prose`).
- 2.2 Write the **loader + validator** (YAML → validated record; fail loudly on bad files).
- 2.3 Write a **converter** that reads an existing `.txt` + `.route_claims.json` and emits a draft `.yaml` (semi-automated migration; you hand-check each).
- 2.4 Migrate the **drug_dose** protocols first (~20 files; mechanical). Start with `meropenem.yaml`, validate against the source table.
- 2.5 Migrate `pcr_panel` (pneumonia, joint infection), then `pathway` (CAP, UTI, …), then `prose` (periop meds, info-only).
- 2.6 Add a **protocol linter** to CI: schema valid, no duplicate aliases across files, every referenced drug_id exists.
- *Done when:* all protocols load as validated records; the cross-file alias-collision check passes (pre-empts F1-type issues at author time).

### Phase 3 — Tools & router *(the heart of D)*
Bot status: live bot untouched; new pipeline testable offline.

- 3.1 Implement `get_dose` over `drug_dose` records, reusing `selection_engine` priority/table logic. Closed drug enum from loaded files. (Fixes F1, F4.)
- 3.2 Implement `interpret_pcr` + `list_panel` over `pcr_panel` records, with `disambiguate_genus`. (Fixes F5, F6, F7, F8.)
- 3.3 Implement `select_pathway` over `pathway` records. Implement `answer_from_section` (+ section retrieval, reusing the embeddings cache) over `prose`. (Fixes F9.)
- 3.4 Implement `ask_clarification`.
- 3.5 Write the **router prompt** (short, stable, with the slimmed chat-state placeholder: `history`, `active_protocol_id`, typed `slots`) and the orchestration loop: call_with_tools → run tool → chat to phrase. Clarification/confirmation handled conversationally, not via stored `pending_*` flags. (Fixes F2, F13.)
- 3.6 Carry over the post-processor and audit envelope from the old code.
- 3.7 **Calculators last:** migrate echo (cardiac output, AVA, ERO/Rvol) and body-size (BMI/BSA/IBW/ABW) to deterministic tools (`get_body_size`, `calc_ava`, …), deleting the brittle implicit-recognition heuristics (`_looks_like_body_size_input`, `_extract_echo_calculator_slots`). Lower urgency (not in logged failures) — safe to defer to post-cutover if a session runs short.
- *Done when:* the new pipeline answers the harness offline; pass rate meets or beats the Phase 0 baseline.

### Phase 4 — Grounding verifier & safety parity
Bot status: live bot untouched.

- 4.1 Implement the verifier as **one function with a per-kind mode** (not branching logic): `drug_dose`/`pcr_panel` → **hard-block** (strip+log any drug name or numeric/unit span not in the tool output); `prose` → **soft-flag** (log/escalate, don't strip, to avoid breaking faithful paraphrase). Mode is picked by the protocol `kind`, so the mix adds no complexity. (Fixes F10, F11.)
- 4.1b Tune empirically: what counts as a "number/unit span," how to avoid false strips on legitimate rephrasing. Add harness cases for both a caught hallucination and a faithful paraphrase that must survive.
- 4.2 Port the safety rules (no identifiers, no outside knowledge, escalation on conflict) into the router/answerer prompts and add tests for each.
- 4.3 Add "not covered by uploaded protocols" as an explicit, tested outcome (no silent answers).
- *Done when:* zero ungrounded-number escapes on the harness; all safety tests green.

### Phase 5 — Replay-diff & hardening
Bot status: old bot idle but intact (rollback); validation is offline.

- 5.1 Run the new pipeline on **replayed real turns** (Phase 0 tool); diff every decision/answer vs the old bot's recorded output. (No live parallel-shadow infra needed, since keeping the bot live during the rebuild is not required.)
- 5.2 Triage diffs into "new is better / equal / regression"; fix regressions; add each as a harness case.
- 5.3 Expand the harness until it covers all protocol kinds and the full debug-note backlog.
- *Done when:* shadow parity — new pipeline ≥ old on the harness and no unexplained regressions on replayed traffic.

### Phase 6 — Cutover & decommission
Bot status: **the switch.**

- 6.1 Flip the entry point (`telegram_app`) to `id_bot2`. Keep the old module importable for one release as rollback.
- 6.2 Monitor live for an agreed window; keep the `/debug` trace view on.
- 6.3 Delete `routing.py` cascade, old schema in `protocol_parser.py`, sidecars, and the old `_ask_ai_impl` gate ladder.
- *Done when:* one routing path remains; config is one-file-per-protocol; old code removed.

### Phase 7 — Optional: provider bake-off
- 7.1 Implement `DeepSeekProvider` / `ClaudeProvider` against the Phase 1 contract test.
- 7.2 Run the harness across providers; compare pass rate + cost. Decide per the governance note (hosting/data residency). Swap via config if warranted.

---

## Time & effort estimate

Rough, and the variance is real (router-prompt tuning and the 48-file migration are the unpredictable parts). "Focused hrs" = uninterrupted skilled work; in practice it spreads over more calendar time in short sessions. Because the bot does **not** need to stay live during the rebuild, Phase 5 is trimmed to offline replay-diffs (no parallel-shadow infra).

| Phase | Focused hrs | ~Sessions | Long pole |
|---|---|---|---|
| 0 — Safety net & scaffolding | 4–8 | 1–2 | harness + log-replay tool |
| 1 — LLM boundary | 3–6 | 1 | tool abstraction + contract test |
| 2 — Structured protocols | 10–20 | 3–5 | **migrating + clinically verifying 48 files** |
| 3 — Tools & router | 10–16 | 3–4 | router prompt iteration |
| 4 — Grounding verifier & safety | 4–6 | 1–2 | verifier tuning |
| 5 — Replay-diff & hardening | 3–6 | 1–2 | triaging diffs |
| 6 — Cutover & decommission | 2–4 | 1 | deleting old code safely |
| 7 — Provider bake-off (optional) | 2–4 | 1 | — |
| **Total** | **~35–65** | **~12–18** | Phase 2 dominates |

Two things shrink your personal time:
- **The vertical slice** (Phase 0 + 1 + just `meropenem.yaml` + `get_dose`) is only **~8–14 hrs / 2–4 sessions** and proves the whole pattern before the bulk migration.
- **Most of this is buildable in Cowork sessions** — your own time is concentrated on decisions and **clinically verifying migrated protocol data** (Phase 2), which must not be rushed. If I do the coding, your hands-on hours are a fraction of the totals above; the calendar length is driven by session/plan limits, not difficulty.

A realistic shape for limited sessions: slice first (2–4 sessions), then migrate protocols in small batches by kind (drug_dose → pcr → pathway → prose), each batch a self-contained session.

## Testing & observability (runs through every phase)

- Regression harness is the single source of truth for "better." One number, watched.
- Every fixed bug and every future debug note becomes a harness case before it's closed.
- `/debug` trace shows: evidence → tool chosen + args → tool result → verifier outcome. (Most data already in your audit envelope.)
- CI gates: protocol linter + schema validation + harness pass-rate threshold.

---

## Decisions — resolved (2026-06-16)

1. **LLM:** **GPT-5.5** for the router; mini/nano permitted for pure phrasing. Trial 5.5 from step 0.5. Router can drop to GPT-5.4 later via the provider seam if harness parity holds. *(Resolved.)*
2. **Verifier strictness:** **mix** — hard-block for `drug_dose`/`pcr_panel`, soft-flag for `prose`. Implemented as one function whose mode is set by protocol `kind` (no added complexity); empirically tuned in Phase 4 (4.1b). *(Resolved.)*
3. **Conversation memory:** **slim** to `history` + `active_protocol_id` + typed `slots`; drop `pending_*` flags; LLM handles clarification/confirmation conversationally; slots stay server-side typed. *(Resolved — design principle 7.)*
4. **HU/EN answering:** keep per-message auto-detect. *(Resolved.)*
5. **Hosting/channel:** stay on **Railway + Telegram**; add a thin `Channel` adapter seam so WhatsApp is a possible later adapter (not part of this rebuild; verify WhatsApp Business pricing/policy + hospital data-governance before any clinical pilot). *(Resolved — design principle 8.)*
6. **Calculators:** **migrate** echo + body-size to deterministic tools, as the last Phase 3 batch (step 3.7); deferrable to post-cutover if time-limited. *(Resolved.)*

### Still open (low-priority, decide in-flight)
- Exact cheap model for the phrasing-only call (mini vs nano) — pick on cost after the Phase 7 bake-off; not blocking.
- Verifier numeric-span definition specifics — settled empirically in step 4.1b.

---

## Risks & mitigations

- **Router mis-selects a tool** → tight tool schemas + `ask_clarification` default + verifier backstop + shadow-mode parity gate before cutover.
- **Migration introduces a transcription error in protocol data** → semi-automated converter + hand-check + a test asserting each migrated dose matches the source file.
- **Clean-rebuild drags on** → strict "every step leaves a working artefact"; drug_dose kind (largest, simplest) first delivers visible wins early.
- **Provider lock-in creeps back** → the Phase 1 contract test forbids model-specific code outside `LLMProvider`.
- **Scope creep on protocols** → schema + linter make malformed/over-complex protocols fail loudly rather than silently adding special cases.

---

## The shortest path to feeling the difference

Phases 0 → 1 → 2.4 (just `meropenem.yaml`) → 3.1 (`get_dose`) gives you a *vertical slice*: one protocol, end to end, on the new architecture, with the meropenem switch-friction and tier bugs fixed and proven by the harness. That slice validates the whole D pattern before you migrate the other 47 files.
