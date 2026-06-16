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

- **Current phase:** Phase 1 — **complete** (LLM provider seam landed).
- **Branch:** `rebuild-d` (created off `main`; live bot on `main` untouched).
- **Last session (2026-06-16, Opus):** Built Phase 1 — the LLM boundary — on `rebuild-d`:
  - `id_bot2/llm/tools.py` — `Tool` (name, description, JSON-Schema params, handler) with `to_openai()`/`to_anthropic()` wire shapes (DeepSeek shares the OpenAI shape) and a dependency-free `validate_arguments()`; `ToolCall` dataclass (name, arguments, id, raw).
  - `id_bot2/llm/provider.py` — `LLMProvider` Protocol (`chat`, `call_with_tools`); `OpenAIProvider` (reads `config.ROUTER_MODEL`/`PHRASING_MODEL`, **injectable client** so parsing is unit-tested offline, lazy `openai`/`config` import); `get_provider()` factory keyed on `ROUTER_PROVIDER`.
  - `id_bot2/llm/__init__.py` — exports `Tool, ToolCall, LLMProvider, OpenAIProvider, get_provider`.
  - `id_bot2/tests/test_provider_contract.py` — 11 offline tests + 1 live (skipped unless `OPENAI_API_KEY` & `ID_BOT2_LIVE=1`): contract (right tool, valid args) via a scripted provider AND `OpenAIProvider` driven by a fake OpenAI client; wire-shape, arg-validation, malformed-JSON, and factory tests.
  - **Done-when met:** a throwaway script routed two toy queries through `OpenAIProvider.call_with_tools` and picked the right fake tool with valid args.
- **Last session (2026-06-16, Opus):** Built Phase 0 end to end on `rebuild-d`:
  - `id_bot2/` package skeleton (README of target architecture; `llm/`, `protocols/`, `tools/`, `tests/`, `docs/`).
  - `id_bot2/textnorm.py` — UTF-8 mojibake repair + accent-folding (**fixes F12**; recovers `d├│zisa`/`dÃ³zisa` → `dózisa`). 9 unit tests.
  - `id_bot2/run_harness.py` — loads + schema-validates `regression_cases.yaml`; offline by default (free, green), `--live` runs the old bot for the baseline, `--target new` for Phase 3+. 8 unit tests.
  - Seeds moved into the repo: `PROGRESS.md`, `check.sh` (root, +x), `regression_cases.yaml`; planning docs → `id_bot2/docs/`.
  - `config.py` — added `ROUTER_MODEL=gpt-5.5`, `PHRASING_MODEL`, `VERIFIER_MODEL`, `ROUTER_PROVIDER` (env-overridable, read only by id_bot2). Live `CHAT_MODEL` untouched.
- **`./check.sh`:** id_bot2 unit tests ✓ (28 passed, 1 live-skipped), LLMProvider contract ✓ (11 passed, 1 live-skipped), regression harness offline ✓ (20/20 cases valid). Legacy suite 330/331.
  - **Deliberate noted exception:** the one legacy failure, `test_missing_allowlist_allowed_with_local_debug_warning`, is **pre-existing and environmental** — it fails identically on the original `HEAD:config.py` and in isolation, because this sandbox has a `runtime_options.json` that defines access, so the "ALLOWED USERS NOT DEFINED" warning the test asserts never fires. Not caused by the rebuild. Re-confirm it passes in the real deploy env; otherwise it's a stale test to fix separately.
- **Old-bot harness baseline: DEFERRED — do not chase.** The before-picture is already encoded in `regression_cases.yaml` via the `status:` labels (8 `baseline` = works on the old bot, 8 `known_fail` = broken, 4 `new` = not yet specified). A `--live` run would only *confirm* those labels — it adds no new information. The user's OpenAI key lives only on Railway (they test online; no local key or local deps), so a live run isn't worth the friction now. Revisit only if an empirically-measured number is wanted before Phase 3; the easiest route then is to run the 20 cases in a Cowork sandbox with a key pasted in once.

## Next action (do this first, next session)

> **Phase 2 (foundation for the vertical slice) — protocol schema + loader/validator. Model: Opus.**
> 1. `git checkout rebuild-d && ./check.sh` — confirm green (the one legacy failure is the noted, pre-existing exception above; all id_bot2 / contract / harness blocks must be green). Note: the sandbox needs `pip install --break-system-packages pytest "httpx[socks]" socksio pyyaml openai python-telegram-bot rapidfuzz` for the legacy suite to collect.
> 2. **2.1** Write the **protocol JSON Schema** for the four kinds (`drug_dose`, `pcr_panel`, `pathway`, `prose`) in `id_bot2/protocols/` (see roadmap §Phase 2 and the mockup for field shapes).
> 3. **2.2** Write the **loader + validator** (`id_bot2/protocols/loader.py`): YAML → validated record; fail loudly on bad files. Reuse the dependency-light validation style already used by the harness (no hard `jsonschema` dep).
> 4. Add `id_bot2/validate_protocols.py` (check.sh already has a block for it): loads every `.yaml`, schema-validates, and runs the **linter** stub (no duplicate aliases across files; referenced `drug_id`s exist — full linter is 2.6).
> 5. Unit tests in `id_bot2/tests/` for the loader/validator (valid file loads; each bad-file class is rejected with a clear message). Offline.
> *Done when:* `python id_bot2/validate_protocols.py` runs green over a tiny fixture protocol, and a deliberately broken fixture is rejected with a useful error.

After this: **2.4** migrate `meropenem.yaml` (clinical hand-check vs source `.txt` — the human's non-delegable job) then **3.1** `get_dose` → the vertical slice is complete and the harness can run `--target new` on it.

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
| 2026-06-16 | Phase 1 | offline 20/20 cases valid; old-bot baseline **pending (needs key)** | LLM provider seam: 28 id_bot2 unit tests + 11 contract tests green (2 live-gated, skipped). Legacy 330/331 (same noted env failure). |

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
