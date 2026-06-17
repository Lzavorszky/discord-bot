# ID Bot rebuild — PROGRESS (the bookmark)

**Read this first in every new session.** It says where we are, what's next, and which model to use. The full plan is in `ID_Bot_Plan_D_Roadmap.md`; decisions are recorded there too.

---

## ✅ Clinical sign-offs — COMPLETE (2026-06-17)

**All 29 drug_dose hand-checks are signed off by owner L** (`id_bot2/docs/*_handcheck.md`):
meropenem (2026-06-16) + the other 28 on 2026-06-17. No longer a go-live blocker.
imipenem/cilastatin/relebactam was **removed** (not on formulary), so the set is 29 protocols.

Remaining go-live (Phase 6) tidy-ups — NOT sign-off, do before cutover:
- **meropenem** `footer` is still the placeholder `"Think TDM! replace later"` — replace with real guidance.
- (vancomycin footer source typo "Ae you"→"Are you" already corrected 2026-06-17.)
---

## How we run this build

- **One coherent chunk per session.** Each ends green (`./check.sh`) and committed. A session running out mid-chunk is fine — this file is the bookmark.
- **Branch, never main.** All work on `rebuild-d`. The live bot on `main` is untouched until cutover (Phase 6).
- **Per-session ritual:**
  1. Read this file (the sign-off section is now COMPLETE; surface any remaining go-live tidy-ups if relevant).
  2. `git checkout rebuild-d && ./check.sh`  → confirm prior phases still green.
  3. Do the session's chunk.
  4. `./check.sh` → must be green.
  5. Update the "Status" + "Pass-rate log" + "Next action" below; `git commit`.
- **Keep checks offline/free.** `./check.sh` uses mocked LLM by default; `./check.sh --live` (a few cents) only per-phase or pre-cutover.
- **Model for building:** Opus for design/safety steps (provider seam, router, verifier, anything touching routing or clinical correctness); a cheaper model for mechanical batches (protocol conversions once the converter exists, boilerplate, test scaffolding). The "Next action" line names which.

---

## Status

- **Current phase:** Phase 5 (roadmap Phase 4) — **grounding verifier + phrasing loop COMPLETE**; the safe pipeline is router → get_dose → phrase → verify. Phase 4 router (roadmap 3.5) before it built the message→`get_dose` resolver. A deterministic alias+keyword resolver over all 30 drug_dose protocols (accent/separator-folded alias match with compound-name containment so *imipenem-relebactam* beats *imipenem*; keyword slot extraction gated to each protocol's declared slots) **plus** an LLM `call_with_tools` path via the existing provider seam — both turn a user message into a `get_dose` call. No-drug → `unsupported` (no silent answer); 2+ drugs → `clarify`. `--target new` **125/125 PASS** with a router cross-check (input must route to the same drug/tool as the pre-baked `call:`); id_bot2 unit **88 → 119**. (Was 131/30-protocols before imipenem/cilastatin/relebactam was removed 2026-06-17 → now 29 protocols / 125 cases.) Remaining: **grounding verifier** (roadmap Phase 4), phrasing-loop finish, then **tmpsmx** + calculators (final phase).
- **Branch:** `rebuild-d` (created off `main`; live bot on `main` untouched).
- **Last session (2026-06-17, Opus):** Phase 5 (roadmap Phase 4) — **grounding verifier + phrasing loop** on `rebuild-d`:
  - `id_bot2/verifier.py` — `verify_grounding(candidate, grounded, kind, *, known_drugs=())` → `VerifierResult`. **One function, per-kind mode** (roadmap 4.1): `drug_dose`/`pcr_panel` = **hard-block**, `pathway`/`prose`/unknown = **soft-flag**. The tool output (`render_dose`) is ground truth; the verifier proves the phrasing introduced no ungrounded clinical fact. Hard-block on any violation **rejects the whole phrasing and returns the verbatim tool text** (safer than excising a number mid-sentence); soft-flag logs but keeps the candidate. Checks (roadmap 4.1b): (1) **hallucinated number** — any number in the candidate absent from source; (2) **right number, wrong unit** — same number, different *base* unit (units folded on the part before `/`, synonyms mapped: microgram/µg→mcg, millilitre→ml, gram→g, hour→h…), only flagged when the source pins a unit to that number; (3) **wrong drug** — a known drug alias present in the candidate but not the source. Formatting (case/accents/separators/trailing `.0`) is normalised away on both sides so faithful paraphrase is not falsely stripped. `id_bot2/tests/test_verifier.py` — **18 tests** incl. the two mandated cases (`test_catches_hallucinated_dose`, `test_faithful_paraphrase_survives`).
  - **Phrasing wired behind the verifier** in `Router` (`router.py`): the loop is now **router → get_dose → phrase → verify**. `Router(..., phrasing_provider=None)`; `route(..., phrasing_provider=None)`. When a phrasing model is supplied AND there is a real dose (never an out-of-range "needs confirmation" prompt), `provider.chat(PHRASING_SYSTEM, grounded)` rewrites the verbatim text, then `verify_grounding` (hard, drug_dose) passes it or **falls back to verbatim** on block. Any phrasing failure/empty output also falls back to verbatim — never raises. The verifier's drug-name vocabulary is **every OTHER drug's aliases** (the answered drug's own aliases are excluded) so a faithful "meropenem dosing" mention isn't falsely blocked while a switch to a different antibiotic still is. `RouterResult` gained `phrased`/`phrasing_blocked`/`grounded_answer`. **Default stays offline/free:** no phrasing provider → answer is verbatim `render_dose` (what `check.sh` runs). `test_router.py` +9 (`TestPhrasingVerifierLoop`): no-phraser-verbatim, faithful-survives, hallucinated-blocked, wrong-drug-blocked, exploding/empty→verbatim, out-of-range-never-phrased, phrasing-via-LLM-route, init-level phraser.
  - **Harness exercises the loop offline** (roadmap 4.1b): cases may carry a `phrase: {candidate, verdict: survives|blocked}` block; `run_harness.py` routes the `input` through the router with a **scripted phraser** (no real model) and asserts the verdict (survives → candidate kept; blocked → verbatim fallback). 2 new meropenem cases. `--target new` **125 → 127 PASS / 0 FAIL**. id_bot2 unit **119 → 146**.
- **Last session (2026-06-17, Opus):** Phase 4 — **the router** (`id_bot2/router.py`) on `rebuild-d`:
  - `Router(protocols_dir, provider=None)` builds a registry from the 30 drug_dose YAMLs (id + folded aliases + declared slots). `Router.route(message) -> RouterResult` (route / tool / protocol / answer / needs_clarification / via / slots / dose).
  - **Deterministic stage (offline, free — the check.sh path & a production fast-path/fallback):** `_norm` folds accents AND separators (`/ - _` → space) so `imipenem-relebactam` == `imipenem relebactam`; longest-alias match per drug + **span-containment dedup** so a compound name beats its component (`imipenem`⊂`imipenem relebactam`, `ceftazidime`⊂`ceftazidime avibactam`). Slots keyword-extracted but **only** for slots the matched protocol declares (so the word "shock" can't set `septic_shock` on meropenem). Out-of-range numerics propagate `get_dose`'s `needs_confirmation`.
  - **LLM stage (production primary):** when nothing resolves deterministically and a provider is supplied, `provider.call_with_tools` picks the tool+args over a closed-`drug_id`-enum `get_dose` Tool schema; the returned `ToolCall` is arg-validated, unknown/invalid → falls through to `unsupported` (never a silent/ungrounded dose). Model prose (no tool) is **not** treated as an answer. Any provider passing `test_provider_contract` works unchanged.
  - **Safety invariants:** no silent answers (F10/F11) → explicit `unsupported`; ambiguity asks (F2) → `clarify`; the tool stays verbatim (router only *selects* a `get_dose` call).
  - `id_bot2/tests/test_router.py` — **31 tests**: registry/tool-schema, alias resolution (folding, separators, compound-vs-component, brands, unknown→None, substring safety), slot extraction (gfr variants/decimals, booleans, declared-only gating, vanco numerics, weight forms, no-invention), end-to-end tiers (NORMAL/CRRT/STEP_UP/default/out-of-range), `clarify`, `unsupported`, and the LLM path via a scripted provider (resolves, invalid-args→unsupported, unknown drug→unsupported, prose→unsupported, fast-path skips provider, drops undeclared slots). id_bot2 unit **88 → 119**.
  - **Harness wired:** `run_harness.py --target new` now routes each case's `input` through the deterministic router and asserts it lands on the same `drug_dose`/`get_dose`/`<drug_id>` as the explicit `call:` (slots NOT compared — inputs may under-specify). All **131/131 PASS**; verified offline that all 131 inputs resolve to the correct drug (0 mismatch / 0 ambiguous / 0 invented slots) before making the cross-check fatal.
  - **Phrasing deferred:** the answer is still `render_dose` (faithful plain text); the PHRASING_MODEL pass + grounding verifier are the next chunk.
- **Last session (2026-06-16, Opus):** Phase 3.4 — **vancomycin migrated** (the TDM/weight drug) on `rebuild-d`:
  - `id_bot2/protocols/vancomycin.yaml` — 24 verbatim tiers (6 TDM level bands; 6 renal × 2 weight first-dose tiers; weight-only & renal-only fallbacks; GFR<10 gap), 25-rung `select` ladder encoding source priority (TDM level 300 > IHD+TBW 220 > CRRT+TBW 210 > GFR+TBW 200–180 > IHD/CRRT/GFR-only 120–80 > TBW-only 60 > GFR<10 50 > default). **No computation** — fixed gram doses selected by bands, so it fits the existing engine.
  - **`get_dose` out-of-range check generalized** from gfr-only to *every declared numeric slot* (implausible `vancomycin_level`/`body_weight`/`mic` → `needs_confirmation`, not a silent band). Verified no regression: 28 `test_get_dose.py` + the prior 118 `--target new` cases still green.
  - `id_bot2/docs/vancomycin_handcheck.md` — full row-by-row sheet (loading/maintenance/TDM tables, selection priority, slots, guardrails, 5 modelling deviations incl. a verbatim source footer typo). **Sign-off pending.**
  - 13 `--target new` vanco cases (TDM bands, renal×weight, fallbacks, IHD-beats-GFR, out-of-range, default) → `--target new` **131 PASS / 0 FAIL / 19 SKIP**.
  - **tmpsmx still deferred** (`table_lookup` 2-D indication×renal + required-slot gating = genuinely new engine work; carries the F3/F4 bug fixes). Agreed next: build the Phase 4 router; tmpsmx is its own phase.
- **Last session (2026-06-16, Sonnet):** Phase 3.2 — **migrated 28 antibiotic drug_dose YAMLs** on `rebuild-d`:
  - All migratable antibiotic source files converted: 5 trivial single-tier, 6 simple GFR-ladder, 6 CI-ladder (GFR≥20 cutoff), 11 extended (multi-tier, ANURIA_IHD, STEP_UP-in-select, or complex combos). Commit: `b04b4d4`.
  - All 29 protocol YAMLs (incl. meropenem) parse clean (29 OK / 0 FAIL). STEP_UP tiers without a selection rule kept in `tiers:` only. Distinct ANURIA_IHD tiers preserved. Source GFR gaps faithfully preserved (cefiderocol >90, colomycin 10–30, imipenem_cilastatin <15) → fall-through to DEFAULT_ANSWER.
  - **Deferred:** vancomycin (weight×GFR matrix + TDM too complex), tmpsmx (`table_lookup` mode unsupported), general_rules (not a drug_dose protocol).
  - **Pending:** clinical hand-check sheets for all 28 new protocols; `--target new` harness cases; human sign-off.
- **Last session (2026-06-16, Opus):** Phase 3.1 — **`get_dose`, the first dose-emitting tool** — on `rebuild-d`:
  - `id_bot2/tools/get_dose.py` — `get_dose(drug_id, *, gfr, crrt, ihd, cns_infection, tdm_low_level, record=None, protocols_dir=None) -> DoseResult`. Loads the drug_dose record (via the validating loader), walks the `select:` ladder **in list order** (first firing guard wins), returns the matched tier **verbatim** + `source_label`, always prepending any `always_show` tier (LOADING). No input → terminal `default` rung → full table. GFR outside the slot range (0–250) → `needs_confirmation=True`, runs no ladder. **Never computes a novel dose.**
  - Guards (`"gfr >= 20"`, `"cns_infection or tdm_low_level"`, …) are evaluated by a **restricted AST walker, not `eval`**: only boolean ops, comparisons, the declared slot names, and literals are allowed; an unknown slot name (protocol typo) or a function call raises `GuardError`. A `None` operand (unprovided gfr) makes a comparison False, so an unspecified slot simply doesn’t fire its rung. `render_dose()` gives a faithful plain-text dump for the harness/debug (NOT final UX phrasing — that’s the phrasing model later).
  - `id_bot2/tests/test_get_dose.py` — **28 tests**: every ladder branch (cns/tdm→STEP_UP, ihd→SEVERE_AKI, crrt→CRRT, gfr≥20→NORMAL, gfr<20→SEVERE_AKI, no-input→full table, gfr=300→needs_confirmation), selection priority, GFR boundaries (0/250 in-range, −5/300 out), verbatim fidelity (NORMAL = 4 g/day), LOADING always shown, footer/prep/never propagation, guard-safety (unknown slot / malformed / function-call all raise), load + wrong-kind errors, renderer. id_bot2 unit tests **60 → 88**.
  - **Harness `--target new` now exercises the slice.** Added an explicit `call:` field to cases (the structured tool invocation the not-yet-built router will later derive from `input`); the `new` target runs `get_dose` for `call`-bearing cases and checks route/tool/protocol + output_has/output_not, SKIPping the rest. Added **6 `new`-status meropenem slice cases**: `python id_bot2/run_harness.py regression_cases.yaml --target new` → **6 PASS / 0 FAIL / 20 SKIP**.
  - **Flagged for the human (clinical — not mine to decide):** the old `meropenem_normal_table` baseline still asserts NORMAL = **3 g/day**, but the owner deliberately revised NORMAL to **4 g/day** (recorded 2026-06-16); the new slice cases assert 4 g/day. I left the stale baseline untouched (it SKIPs under `--target new`, no `call`) — at meropenem sign-off it should be retired/repointed to 4 g/day. (The full-table substring "3 g/day" still appears via the CRRT tier, so the old assertion passed for the wrong reason.)
  - **Env note (mount truncation, NOT a regression):** the sandbox OneDrive Files-On-Demand mount served **truncated/dehydrated** copies of some pre-existing files (`meropenem.yaml` lost its `footer:` line; file-tool edits to `run_harness.py`/`regression_cases.yaml` didn’t fully materialise in the mount). Fixed by rebuilding each file in the working tree from `git show HEAD:<path>` (+ re-applying edits via bash) so the tree `git commit` captures is whole. Committed blobs were always intact.
- **Last session (2026-06-16, Opus):** Phase 2.4 rev 2 — schema extension + owner edits on `rebuild-d`:
  - **Schema extended:** added optional free-text `prep` and `notes` fields to the **drug_dose** kind (`schema.py` JSON-Schema + `KIND_FIELDS`; `loader.py` string-validates both). Available to every antibiotic/drug_dose protocol going forward. +2 loader tests (now 60 id_bot2 unit tests).
  - **meropenem.yaml owner edits (deliberate, ID-team-directed — diverge from source on purpose):** NORMAL tier `3 g/day` → `4 g/day`, admin `6.3 mL/h` → `8.3 mL/h` (internally consistent: 4 g/day at 1 g/50 mL ≈ 8.3 mL/h). `footer` replaced wholesale with placeholder `"Think TDM! replace later"` (original GFR-cutoff guidance dropped — replace before go-live). Reduced-dose preparation note moved from `footer` into the new `prep:` field (deviation RESOLVED).
  - `meropenem_handcheck.md` updated to rev 2: NORMAL flagged ⚠ owner-edited, new §7 (owner edits) + §8 (prep resolved), sign-off checklist now asks the human to confirm the 4 g/day change specifically.
  - `./check.sh` green except the one known env legacy failure; validator green over the corpus.
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
- **`./check.sh`:** id_bot2 unit tests ✓ (**146 passed**, 1 live-skipped — incl. 18 verifier + 9 phrasing-loop tests), protocol schema + linter ✓ (**29** valid records, no alias collisions), LLMProvider contract ✓ (11 passed, 1 live-skipped), regression harness offline ✓ (146 cases valid). `--target new` ✓ **127 PASS / 0 FAIL / 19 SKIP** (router input→call cross-check + phrasing/verifier cross-check). Legacy suite 330/331 (one pre-existing env failure, noted below).
  - **Deliberate noted exception:** the one legacy failure, `test_missing_allowlist_allowed_with_local_debug_warning`, is **pre-existing and environmental** — it fails identically on the original `HEAD:config.py` and in isolation, because this sandbox has a `runtime_options.json` that defines access, so the "ALLOWED USERS NOT DEFINED" warning the test asserts never fires. Not caused by the rebuild. Re-confirm it passes in the real deploy env; otherwise it's a stale test to fix separately.
- **Old-bot harness baseline: DEFERRED — do not chase.** The before-picture is already encoded in `regression_cases.yaml` via the `status:` labels (8 `baseline` = works on the old bot, 8 `known_fail` = broken, 4 `new` = not yet specified). A `--live` run would only *confirm* those labels — it adds no new information. The user's OpenAI key lives only on Railway (they test online; no local key or local deps), so a live run isn't worth the friction now. Revisit only if an empirically-measured number is wanted before Phase 3; the easiest route then is to run the 20 cases in a Cowork sandbox with a key pasted in once.

## Next action (do this first, next session)

> **Phase 5 (PROGRESS) / roadmap Phase 4 is DONE.** The grounding verifier + phrasing loop landed this session (router → get_dose → phrase → verify; hard-block for drug_dose, verbatim fallback on any ungrounded number/unit/drug). **Next chunk — pick one with the owner:**
> **(A) Roadmap 4.2/4.3 — safety-rule parity (Opus, small).** Port the answerer/router safety rules into prompts and add tests: no identifiers, no outside knowledge, escalate on conflict; confirm "not covered by uploaded protocols" is an explicit tested outcome (already true via `route="unsupported"` — add named tests). Closes Phase 4 entirely.
> **(B) Phase 2.5 — migrate `pcr_panel`/`pathway` protocols (mixed model).** Unlocks `interpret_pcr`/`select_pathway` as router tools (fixes F5–F9). The verifier's hard-block already covers `pcr_panel`; the soft-flag covers `pathway`/`prose`.
> **(C) Final phase — `tmpsmx` (`table_lookup`) + calculators (Opus for tmpsmx).** Genuinely new engine mode; carries the F3/F4 fixes. Do LAST.
> Per-session ritual unchanged: `git checkout rebuild-d && ./check.sh` (green bar = legacy 330/331 + id_bot2 146 + 29 protocols + 11 contract + harness `--target new` 127/127). If the mount served stale blobs, rebuild the working tree from `git show HEAD:<path>` before editing (env note recipe).
> *Done when (this chunk, MET):* zero ungrounded-number escapes on the harness; phrasing runs behind the verifier; check.sh green.

> **Env note — mount is create/overwrite-only, no deletes (this session, 2026-06-17).** The Cowork sandbox mounts the repo via virtiofs where `unlink`/`rmdir` raise "Operation not permitted" (hence the stuck `.git/index.lock`, `HEAD.lock`, junk `desktop.ini` ref, and `rebuild-d.lock.stale.N` branches that can't be cleaned from inside the sandbox). **Recipe that works:** clone/copy the repo to local sandbox disk (`/tmp`), clean the stale locks there (deletes ARE allowed on local disk), `git reset --hard HEAD`, build+test+commit on the fast local copy, then persist back to the mount **without deleting anything** — copy new loose objects into `.git/objects/` (write-once, content-addressed) and **overwrite** `.git/refs/heads/rebuild-d` in place with the new commit sha. Working-tree source files: overwrite/create on the mount (deletes still won't work, but Phase 4 only adds files). The committed blobs in the mount `.git` are always intact even when the working tree reads dehydrated.

> **Clinical sign-off — meropenem DONE (2026-06-16, owner L):** hand-check rev 2 **signed** — NORMAL 4 g/day @ 8.3 mL/h confirmed, footer placeholder retained (replace before go-live), all other tiers/selection/slots confirmed. The stale `meropenem_normal_table` case was repointed 3 g/day → **4 g/day** (status `new`, `call:` added). Remaining footer replacement is a go-live (Phase 6) task, not a blocker.

After this: the rest of the drug_dose kind (~20 antibiotic files under `protocols/antibiotics/`) migrates almost mechanically (a cheaper model is fine for the batch), each still getting its own clinical hand-check sheet before sign-off.

> **Old-bot baseline — DEFERRED, no action needed.** The baseline is already captured by the
> `status:` labels in `regression_cases.yaml` (8 work / 8 broken / 4 unspecified on the current bot);
> a live run only confirms them. The user runs the bot on Railway (key not available locally), so we
> are NOT recording a live number now. If wanted before Phase 3: paste a key into a Cowork session and
> run `python id_bot2/run_harness.py regression_cases.yaml --live` there (≈ a few cents).

---

## Final phase — do LAST (after the router and everything else)

These are intentionally deferred to the very end of the rebuild; come back to them only once the router and the rest of Phase 4+ are done:

- **tmpsmx** — `selection_mode: table_lookup` (a 2-D `{indication_tier}_{renal_category}` key + required-slot gating that asks when indication/weight/renal are missing + weight-band dosing). This is a genuinely new engine mode, not a config migration, and it carries the original **F3/F4** misrouting bug fixes (returned PROPHYLAXIS instead of the high-dose path). Source: `protocols/antibiotics/tmpsmx.txt`. Until done, the router routes tmpsmx to a fallback / the old bot.
- **Calculator protocols** (`body_size_calculators`, the echo_* / steroid_equivalence helpers, etc.) — migrate last; lower priority than the router.

---

## Pass-rate log (update every session)

| Date | Phase | Harness pass-rate | Notes |
|------|-------|-------------------|-------|
| 2026-06-16 | planning | — | seeds created; no code yet |
| 2026-06-16 | Phase 0 | offline 20/20 cases valid; live baseline **deferred** (encoded in case labels: 8 ok / 8 fail / 4 new) | id_bot2 scaffolding green: 17/17 unit tests, F12 normaliser, harness machinery. Legacy 330/331 (1 pre-existing env failure, noted). |
| 2026-06-16 | Phase 1 | offline 20/20 cases valid; old-bot baseline **pending (needs key)** | LLM provider seam: 28 id_bot2 unit tests + 11 contract tests green (2 live-gated, skipped). Legacy 330/331 (same noted env failure). |
| 2026-06-16 | Phase 2 (2.1–2.3) | offline 20/20 cases valid; old-bot baseline **pending (needs key)** | Protocol schema + loader/validator + linter stub: 58 id_bot2 unit tests (30 new) + schema/linter block green over empty corpus; 11 contract tests green. Legacy 330/331 (same noted env failure). |
| 2026-06-16 | Phase 2.4 | offline 20/20 cases valid; old-bot baseline **pending (needs key)** | First real protocol migrated: `meropenem.yaml` (drug_dose) schema-valid + linter-green over **non-empty** corpus (1 record, no alias collisions). 58 id_bot2 unit + 11 contract tests green. Clinical hand-check sheet produced; **human sign-off pending**. Legacy 330/331 (same noted env failure). |
| 2026-06-16 | Phase 2.4 rev 2 | offline 20/20 cases valid; old-bot baseline **pending (needs key)** | Schema extended (`prep`/`notes` on drug_dose, +2 tests → 60 id_bot2 unit). Owner edits to meropenem.yaml: NORMAL 4 g/day @ 8.3 mL/h, footer placeholder, prep field carries reduced-dose note. Hand-check rev 2; **sign-off (incl. NORMAL change) pending**. Legacy 330/331 (same noted env failure). |
| 2026-06-16 | Phase 3.3 | **`--target new` 118/118 PASS** (100%); 28 test_get_dose.py still pass | `get_dose.py` `**extra_slots` patch; 111 new call: cases (28 protocols) + 7 existing meropenem = 118 total. Commit `590af33`. |
| 2026-06-17 | Sign-off COMPLETE | **`--target new` 125/125 PASS**; 119 id_bot2 unit; 29/29 protocols valid | All 29 hand-checks signed by owner L. vancomycin footer typo corrected ("Ae you"→"Are you"). Standing sign-off reminder retired. Legacy 330/331 (same noted env failure). |
| 2026-06-17 | Sign-off + formulary | **`--target new` 125/125 PASS**; 119 id_bot2 unit; 29/29 protocols valid | Clinical sign-off batches 1–3c: 26/29 hand-checks signed by owner L (pending: fosfomycin, fluconazole, vancomycin). **imipenem/cilastatin/relebactam removed** (not on formulary): protocol+sheet+6 harness cases dropped, router tests re-pointed to ceftazidime/avibactam. Legacy 330/331 (same noted env failure). |
| 2026-06-17 | Phase 4 (router) | **`--target new` 131/131 PASS** (with router input→call cross-check); 119 id_bot2 unit (31 new router tests); 30/30 protocols valid | `router.py`: deterministic alias+slot resolver (compound-name containment, declared-slot gating) + LLM `call_with_tools` dispatch; no-drug→unsupported, 2+→clarify. Harness routes `input` and asserts same drug/tool as `call:`. Legacy 330/331 (same noted env failure). |
| 2026-06-17 | Phase 5 (verifier+phrasing) | **`--target new` 127/127 PASS** (+ phrasing/verifier cross-check); **146 id_bot2 unit** (18 verifier + 9 phrasing-loop); 29/29 protocols valid | `verifier.py`: one fn, per-kind mode (drug_dose/pcr_panel hard-block, prose/pathway soft-flag); catches hallucinated number, wrong unit (base-unit fold), wrong drug; verbatim fallback on block. Phrasing wired behind it in `Router` (offline default = verbatim `render_dose`). 2 harness `phrase:` cases (faithful survives / hallucination blocked). Legacy 330/331 (same noted env failure). |
| 2026-06-16 | Phase 3.4 | **`--target new` 131/131 PASS**; 88 id_bot2 unit; 30/30 protocols valid | vancomycin migrated (24 tiers, TDM + weight×renal, no computation) + 13 harness cases; `get_dose` out-of-range generalized to all numeric slots (no regression). Hand-check written, **sign-off pending**. Legacy 330/331 (same noted env failure). |
| 2026-06-16 | Phase 3.2 | 29/29 YAMLs parse valid; harness cases TBD | 28 new drug_dose YAMLs committed (`b04b4d4`). Hand-check sheets + `--target new` cases pending. |
| 2026-06-16 | Phase 3.1 | offline 26/26 cases valid; `--target new` 6/6 meropenem slice PASS; old-bot baseline **deferred** | `get_dose` vertical slice: 88 id_bot2 unit tests (28 new) + schema/linter + 11 contract green. Harness `--target new` exercises the slice via explicit `call:` (6 PASS / 20 SKIP — router not built). Meropenem clinical **sign-off still pending**. Legacy 330/331 (same noted env failure). |

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
