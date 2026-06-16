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

- **Current phase:** Phase 2 — **2.4 landed** (meropenem.yaml migrated, schema-valid + linter-green; clinical hand-check sheet produced, human sign-off pending). Next: **3.1 get_dose** (vertical slice).
- **Branch:** `rebuild-d` (created off `main`; live bot on `main` untouched).
- **Last session (2026-06-16, Opus):** Phase 2.4 — migrated the first real protocol on `rebuild-d`:
  - `id_bot2/protocols/meropenem.yaml` (kind: drug_dose) — tiers/select/never/slots/footer transcribed **verbatim** from source `protocols/antibiotics/meropenem.txt` (v0.3) + `meropenem.route_claims.json`. 5 tiers (LOADING `always_show`), 6-rung `select` ladder preserving source priority order (STEP_UP 110 > IHD→SEVERE_AKI 100 > CRRT 95 > GFR≥20 NORMAL 70 > GFR<20 SEVERE_AKI 60 > default full table).
  - **One deviation flagged for sign-off:** the source's *reduced-dose preparation* line has no field in the drug_dose schema; preserved **verbatim in `footer`** (no clinical value lost). Decide later: keep in footer vs add a `prep:`/`notes:` schema field.
  - `id_bot2/docs/meropenem_handcheck.md` — full row-by-row side-by-side (every dose/when/admin/cutoff, selection priority, slots, routing, guardrails, aliases) for the human's non-delegable clinical check. **Sign-off still pending.**
  - Validator + linter green over the now non-empty corpus (1 valid record, no alias collisions).
  - **Env note (not a regression):** on session start the `rebuild-d` tip git object read as “corrupt” — it was transient OneDrive Files-On-Demand dehydration in the sandbox mount; objects read fine once hydrated. `git log rebuild-d` shows all four phase commits intact.
- **Last session (2026-06-16, Opus):** Built Phase 2 foundation (2.1–2.3) on `rebuild-d`:
  - `id_bot2/protocols/schema.py` — single declarative source of truth for the four `kind`s. Exposes shared enums (`KINDS`, `INTENTS`, `SLOT_TYPES`, `OUT_OF_RANGE_ACTIONS`, `STATUSES`), a real **JSON-Schema (draft 2020-12)** doc (`PROTOCOL_JSON_SCHEMA`, for export/audit) **and** the compact `KIND_REQUIRED`/`KIND_FIELDS` rule tables the dependency-light validator walks — kept together so the two can't drift.
  - `id_bot2/protocols/loader.py` — `validate_record()` (returns a problems list, no hard `jsonschema` dep, mirroring `run_harness.py`), `load_protocol()` (raises `ProtocolError` naming the file + **all** problems — fails loudly), `load_protocol_dir()`. Per-kind structural checks: drug_dose tiers+`select` ladder (ghost-tier + missing-`default` caught), pcr_panel organisms, pathway outputs+select, prose sections; slot/intent/status enums; wrong-kind-field smell check.
  - `id_bot2/validate_protocols.py` — CI entrypoint (check.sh block 3). Schema-validates every `protocols/*.yaml`, then the **linter stub**: cross-file **alias-collision** check (accent/space/case-folded → pre-empts F1) as a hard error; unresolved drug-id references as warnings (promoted to errors in 2.6). Green when `protocols/` is still empty.
  - `id_bot2/tests/test_protocol_loader.py` (30 tests) + fixtures under `id_bot2/tests/fixtures/{good,bad}/`. **Done-when met:** `validate_protocols.py` green over the good fixtures; a broken-schema fixture and an alias-collision pair are each rejected with specific, useful errors.
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
- **`./check.sh`:** id_bot2 unit tests ✓ (58 passed, 1 live-skipped), protocol schema + linter ✓ (now green over a **non-empty** corpus — `meropenem.yaml`: 1 valid record, no alias collisions), LLMProvider contract ✓ (11 passed, 1 live-skipped), regression harness offline ✓ (20/20 cases valid). Legacy suite 330/331.
  - **Deliberate noted exception:** the one legacy failure, `test_missing_allowlist_allowed_with_local_debug_warning`, is **pre-existing and environmental** — it fails identically on the original `HEAD:config.py` and in isolation, because this sandbox has a `runtime_options.json` that defines access, so the "ALLOWED USERS NOT DEFINED" warning the test asserts never fires. Not caused by the rebuild. Re-confirm it passes in the real deploy env; otherwise it's a stale test to fix separately.
- **Old-bot harness baseline: DEFERRED — do not chase.** The before-picture is already encoded in `regression_cases.yaml` via the `status:` labels (8 `baseline` = works on the old bot, 8 `known_fail` = broken, 4 `new` = not yet specified). A `--live` run would only *confirm* those labels — it adds no new information. The user's OpenAI key lives only on Railway (they test online; no local key or local deps), so a live run isn't worth the friction now. Revisit only if an empirically-measured number is wanted before Phase 3; the easiest route then is to run the 20 cases in a Cowork sandbox with a key pasted in once.

## Next action (do this first, next session)

> **Phase 3.1 — implement `get_dose` over `meropenem.yaml` (the vertical slice). Model: Opus (clinical correctness — first tool that emits a dose).**
> 1. `git checkout rebuild-d && ./check.sh` — confirm green (the one legacy failure is the noted, pre-existing env exception; all id_bot2 / schema+linter / contract / harness blocks must be green). Sandbox deps: `pip install --break-system-packages pytest "httpx[socks]" socksio pyyaml openai python-telegram-bot rapidfuzz`.
> 2. **Pre-req: clinical sign-off on meropenem.** `id_bot2/docs/meropenem_handcheck.md` must be signed — including the reduced-dose-prep deviation (keep in `footer` vs add a `prep:`/`notes:` schema field). If a field is wanted, add it to schema/loader first.
> 3. Implement `get_dose(drug_id, gfr=None, crrt=False, ihd=False, cns_infection=False, tdm_low_level=False) -> DoseResult` in `id_bot2/tools/`: load the drug_dose record, run the `select:` ladder **in list order**, return the matched tier **verbatim** + `source_label`, plus any `always_show` tier (LOADING). GFR out of declared range → `{needs_confirmation: true}`. Never computes a novel dose.
> 4. Unit-test every ladder branch against meropenem: cns/tdm→STEP_UP, ihd→SEVERE_AKI, crrt→CRRT, gfr≥20→NORMAL, gfr<20→SEVERE_AKI, no input→DEFAULT_ANSWER+full table, gfr=300→needs_confirmation.
> 5. Wire the meropenem `--target new` harness rows so `python id_bot2/run_harness.py regression_cases.yaml --target new` exercises the slice; `./check.sh` green.
> *Done when:* `get_dose` returns the correct verbatim tier for each meropenem case, the vertical slice runs end to end offline, and the harness runs `--target new` on the meropenem rows.

After this: the rest of the drug_dose kind (~20 antibiotic files under `protocols/antibiotics/`) migrates almost mechanically (a cheaper model is fine for the batch), each still getting its own clinical hand-check sheet before sign-off.

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
| 2026-06-16 | Phase 2 (2.1–2.3) | offline 20/20 cases valid; old-bot baseline **pending (needs key)** | Protocol schema + loader/validator + linter stub: 58 id_bot2 unit tests (30 new) + schema/linter block green over empty corpus; 11 contract tests green. Legacy 330/331 (same noted env failure). |
| 2026-06-16 | Phase 2.4 | offline 20/20 cases valid; old-bot baseline **pending (needs key)** | First real protocol migrated: `meropenem.yaml` (drug_dose) schema-valid + linter-green over **non-empty** corpus (1 record, no alias collisions). 58 id_bot2 unit + 11 contract tests green. Clinical hand-check sheet produced; **human sign-off pending**. Legacy 330/331 (same noted env failure). |

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
