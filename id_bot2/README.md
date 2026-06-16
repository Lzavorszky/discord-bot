# id_bot2 — Plan D rebuild

Structured protocols + a tool-calling LLM router + deterministic engines exposed
as tools + a grounding verifier. The live bot keeps running on `main`; this
package is built on branch `rebuild-d` and only goes live at the Phase 6 cutover.

## Target pipeline

```
message + chat state
  -> LLMProvider.call_with_tools(router_prompt, tools)     # the single decision
       get_dose(drug_id, gfr?, crrt?, ihd?, ...)           # kind: drug_dose
       interpret_pcr(panel, organisms[], markers[])        # kind: pcr_panel
       select_pathway(protocol_id, slots)                  # kind: pathway
       answer_from_section(protocol_id, section, lang)     # kind: prose
       list_panel(panel) / ask_clarification(text)
  -> tool result (verbatim clinical data)
  -> LLMProvider.chat(): phrase the result in the user's language
  -> grounding verifier: strip any dose/drug/number not in the tool output
  -> post-process (source label, footer)   [reuse old code]
  -> audit envelope                         [reuse old code]
```

## Design principles

1. One decision point (the LLM picks one tool — no regex cascade).
2. The model never emits clinical facts; doses/tiers/organisms come from tools.
3. One file per protocol (YAML, schema-validated).
4. Deterministic core is reused, not rewritten.
5. The LLM is swappable behind `LLMProvider`.
6. Nothing ships unmeasured — the regression harness guards every step.
7. Slim memory: `history` + `active_protocol_id` + typed `slots`. No `pending_*`.
8. Thin `Channel` seam so a future channel is an adapter, not a rewrite.

## Layout

| Path | Phase | State |
|------|-------|-------|
| `textnorm.py`        | 0 | UTF-8 normalise + accent-fold (fixes F12). **Built.** |
| `run_harness.py`     | 0 | Regression runner over `regression_cases.yaml`. **Built.** |
| `llm/provider.py`    | 1 | `LLMProvider` interface + `OpenAIProvider`. Stub. |
| `protocols/`         | 2 | YAML protocols + loader + schema. Empty. |
| `tools/`             | 3 | `get_dose`, `interpret_pcr`, … deterministic tools. Empty. |
| `validate_protocols.py` | 2 | Schema + linter entry (CI). Not created yet. |
| `smoke.py`           | 3 | End-to-end canned-query smoke. Not created yet. |
| `tests/`             | 0+ | Unit + contract tests. |
| `docs/`              | — | Roadmap, strategic review, mockup. |

`check.sh` (repo root) runs each block only if its target file exists, so the
same script works from Phase 0 through cutover.
