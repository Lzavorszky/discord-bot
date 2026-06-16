# ID Bot rebuild — PROGRESS (the bookmark)

**Read this first in every new session.** It says where we are, what's next, and which model to use. The full plan is in `ID_Bot_Plan_D_Roadmap.md`; decisions are recorded there too.

---

## How we run this build

- **One coherent chunk per session.** Each ends green (`./check.sh`) and committed. A session running out mid-chunk is fine — this file is the bookmark.
- **Branch, never main.** All work on `rebuild-d`. The live bot on `main` is untouched until cutover (Phase 6).
- **Per-session ritual:**
  1. Read this file.
  2. `git checkout rebuild-d && ./check.sh`  → confirm prior phases still green.
  3. Do the session's chunk.
  4. `./check.sh` → must be green.
  5. Update the "Status" + "Pass-rate log" + "Next action" below; `git commit`.
- **Keep checks offline/free.** `./check.sh` uses mocked LLM by default; `./check.sh --live` (a few cents) only per-phase or pre-cutover.
- **Model for building:** Opus for design/safety steps (provider seam, router, verifier, anything touching routing or clinical correctness); a cheaper model for mechanical batches (protocol conversions once the converter exists, boilerplate, test scaffolding). The "Next action" line names which.

---

## Status

- **Current phase:** Phase 0 — **complete** (scaffolding + harness + F12 normaliser landed).
- **Branch:** `rebuild-d` (created off `main`; live bot on `main` untouched).
- **Last session (2026-06-16, Opus):** Built Phase 0 end to end on `rebuild-d`:
  - `id_bot2/` package skeleton (README of target architecture; `llm/`, `protocols/`, `tools/`, `tests/`, `docs/`).
  - `id_bot2/textnorm.py` — UTF-8 mojibake repair + accent-folding (**fixes F12**; recovers `d├│zisa`/`dÃ³zisa` → `dózisa`). 9 unit tests.
  - `id_bot2/run_harness.py` — loads + schema-validates `regression_cases.yaml`; offline by default (free, green), `--live` runs the old bot for the baseline, `--target new` for Phase 3+. 8 unit tests.
  - Seeds moved into the repo: `PROGRESS.md`, `check.sh` (root, +x), `regression_cases.yaml`; planning docs → `id_bot2/docs/`.
  - `config.py` — added `ROUTER_MODEL=gpt-5.5`, `PHRASING_MODEL`, `VERIFIER_MODEL`, `ROUTER_PROVIDER` (env-overridable, read only by id_bot2). Live `CHAT_MODEL` untouched.
- **`./check.sh`:** id_bot2 unit tests ✓ (17/17), regression harness offline ✓ (20/20 cases valid). Legacy suite 330/331.
  - **Deliberate noted exception:** the one legacy failure, `test_missing_allowlist_allowed_with_local_debug_warning`, is **pre-existing and environmental** — it fails identically on the original `HEAD:config.py` and in isolation, because this sandbox has a `runtime_options.json` that defines access, so the "ALLOWED USERS NOT DEFINED" warning the test asserts never fires. Not caused by the rebuild. Re-confirm it passes in the real deploy env; otherwise it's a stale test to fix separately.
- **Old-bot harness baseline: DEFERRED — do not chase.** The before-picture is already encoded in `regression_cases.yaml` via the `status:` labels (8 `baseline` = works on the old bot, 8 `known_fail` = broken, 4 `new` = not yet specified). A `--live` run would only *confirm* those labels — it adds no new information. The user's OpenAI key lives only on Railway (they test online; no local key or local deps), so a live run isn't worth the friction now. Revisit only if an empirically-measured number is wanted before Phase 3; the easiest route then is to run the 20 cases in a Cowork sandbox with a key pasted in once.

## Next action (do this first, next session)

> **Phase 1 — the LLM boundary (swappability seam). Model: Opus.**
> 1. `git checkout rebuild-d && ./check.sh` — confirm green (the one legacy failure is the noted, pre-existing exception above; the id_bot2 + harness blocks must be green).
> 2. Define `LLMProvider` in `id_bot2/llm/provider.py`: `chat(messages) -> str` and `call_with_tools(messages, tools) -> ToolCall|str` (the stub interface is already there).
> 3. Implement `OpenAIProvider` (uses `config.ROUTER_MODEL` / `PHRASING_MODEL`).
> 4. Add a `tools` abstraction (name, JSON-schema params, handler) that maps cleanly to OpenAI function-calling *and* DeepSeek/Anthropic shapes.
> 5. Write `id_bot2/tests/test_provider_contract.py` — given a prompt + 2 fake tools, the provider calls the right one with valid args. Offline via a fake/mock provider by default; the live OpenAI variant behind `--live`. (check.sh already has a block for this file.)
> *Done when:* a throwaway script routes a toy query through `OpenAIProvider.call_with_tools` and picks the right fake tool.

After Phase 1: the vertical slice — `meropenem.yaml` (Phase 2.4) + `get_dose` (Phase 3.1) — proves the whole pattern before the 48-file migration.

> **Old-bot baseline — DEFERRED, no action needed.** The baseline is already captured by the
> `status:` labels in `regression_cases.yaml` (8 work / 8 broken / 4 unspecified on the current bot);
> a live run only confirms them. The user runs the bot on Railway (key not available locally), so we
> are NOT recording a live number now. If wanted before Phase 3: paste a key into a Cowork session and
> run `python id_bot2/run_harness.py regression_cases.yaml --live` there (≈ a few cents).

---

## Pass-rate log (update every session)

| Date | Phase | Harness pass-rate | Notes |
|------|-------|-------------------|-------|
| 2026-06-16 | planning | — | seeds created; no code yet |
| 2026-06-16 | Phase 0 | offline 20/20 cases valid; live baseline **deferred** (encoded in case labels: 8 ok / 8 fail / 4 new) | id_bot2 scaffolding green: 17/17 unit tests, F12 normaliser, harness machinery. Legacy 330/331 (1 pre-existing env failure, noted). |

---

## What I (the human) need to do — step by step

You don't write code. Your job is decisions, clinical verification, and kicking off sessions. In order:

1. **Kick off Phase 0** — start a fresh session and say *"Continue the ID Bot rebuild — read PROGRESS.md."* Pick **Opus**.
2. **Confirm the branch/skeleton** looks right when I report back (quick glance, not a code review).
3. **Each later session:** open fresh, same kickoff line, pick the model named in "Next action."
4. **Clinical verification — your one non-delegable job (Phase 2).** When I migrate each protocol to `.yaml`, check that the migrated doses / tiers / organisms / therapies match the source `.txt`. I'll give you a side-by-side diff per file. **Nothing clinical ships on my say-so alone.**
5. **Triage harness diffs (Phase 5).** I'll show cases where the new bot differs from the old; you tell me "new is correct" vs "that's a regression." Each becomes a permanent test.
6. **Approve cutover (Phase 6).** When the harness is green and shadow/replay parity holds, you give the go to flip Telegram to `id_bot2` and keep `main` as rollback.
7. **Watch the pass-rate** in the table above — it should climb. If it ever drops, the last session regressed something; tell me and we fix before continuing.

Things only you can decide as they come up (all low-stakes, flagged in the roadmap): exact phrasing-model (mini vs nano), verifier numeric-span tuning calls, and — much later, separately — whether to pilot WhatsApp.

---

## Decisions (locked — see roadmap for detail)

LLM: **GPT-5.5** router + mini/nano phrasing · Migration: **clean rebuild then cut over** · Format: **YAML + JSON-Schema** · Verifier: **hard for dose/PCR, soft for prose** · Memory: **slim (history + active_protocol + typed slots)** · Channel: **Telegram + thin adapter seam** · Calculators: **migrate (last Phase 3 batch)** · HU/EN: **auto-detect**.

## Open items (decide in-flight, non-blocking)

- Phrasing-call model: mini vs nano (after Phase 7 bake-off).
- Verifier "numeric span" definition (tune in step 4.1b).
- WhatsApp pilot: separate, post-cutover, needs data-governance sign-off.
