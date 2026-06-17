# steroid_equivalence — clinical hand-check (Plan D, the final migration phase)

**Source:** `protocols/steroid_equivalence.txt` (v0.1) + `steroid_equivalence.route_claims.json`
**Migrated to:** `id_bot2/protocols/steroid_equivalence.yaml` (**kind: `calculator`**)
**Tool:** `id_bot2/tools/calculate.py`  ·  **Tests:** `id_bot2/tests/test_calculate.py`
**Status:** owner sign-off **PENDING** (owner L). Nothing clinical ships on the migrator's say-so.

The non-delegable clinical check: confirm the reference equivalent doses and the
conversion rule match the source verbatim, confirm the worked examples, and decide the
flagged **engine decision**.

---

## 0. Computing kind — safety story

Same as body size: the conversion (a table lookup feeding one division, then a multiply
per steroid) is **declared in the YAML** (verbatim from source) and evaluated by the
restricted arithmetic AST (no `eval`); every output is unit-tested with hand-computed
values and verified in `calculator` (**hard**) mode.

---

## 1. Reference equivalent doses (source `## SELECTION_RULES` table) — VERBATIM

| Steroid (enum value) | Reference equivalent dose (lookup table value) |
|----------------------|-----------------------------------------------:|
| methylprednisone | 8 mg |
| dexamethasone | 1.5 mg |
| hydrocortisone | 40 mg |
| prednisolone | 10 mg |
| fludrocortisone | 4 mg |

These five numbers appear **twice** in the YAML and must agree: once as the `lookup`
`table` (input steroid → its reference mg) and once as the `* <ref>` multiplier in each
`eq_*` formula. ✅ Please confirm all five against the source.

---

## 2. Conversion rule (source) ⟶ migrated formulas (VERBATIM)

Source:
- `conversion_factor = input_dose_mg / reference_equivalent_dose_for_input_steroid`
- `equivalent_dose_for_each_steroid = conversion_factor * reference_equivalent_dose_for_that_steroid`

Migrated `compute` steps:

| # | Step | Migrated |
|---|------|----------|
| 1 | reference dose for the **input** steroid | `lookup: steroid_agent` → table above → `ref_in` |
| 2 | conversion factor | `factor = steroid_dose_mg / ref_in` |
| 3 | methylprednisone equivalent | `eq_methylprednisone = factor * 8` |
| 4 | dexamethasone equivalent | `eq_dexamethasone = factor * 1.5` |
| 5 | hydrocortisone equivalent | `eq_hydrocortisone = factor * 40` |
| 6 | prednisolone equivalent | `eq_prednisolone = factor * 10` |
| 7 | fludrocortisone equivalent | `eq_fludrocortisone = factor * 4` |

---

## 3. Worked examples (re-derive by hand)

**Example A — dexamethasone 6 mg.** factor = 6 / 1.5 = **4**.

| Output | factor × ref | Displayed |
|--------|-------------:|-----------|
| methylprednisone | 4 × 8 = 32 | **32.00 mg** |
| dexamethasone | 4 × 1.5 = 6 | **6.00 mg** |
| hydrocortisone | 4 × 40 = 160 | **160.00 mg** |
| prednisolone | 4 × 10 = 40 | **40.00 mg** |
| fludrocortisone | 4 × 4 = 16 | **16.00 mg** |

**Example B — hydrocortisone 100 mg.** factor = 100 / 40 = **2.5**. → dexamethasone
2.5 × 1.5 = **3.75 mg**; prednisolone 2.5 × 10 = **25 mg**; methylprednisone 2.5 × 8 = **20 mg**.

**Example C — methylprednisone 8 mg (identity).** factor = 8 / 8 = **1** → every output
equals its reference dose (sanity check the table).

Pinned by `test_calculate.py` (`test_steroid_*`) and harness cases
`steroid_dexamethasone_6mg`, `steroid_hydrocortisone_100mg`.

---

## 4. Engine decision (please confirm)

> ⚠ **ENGINE DECISION — display rounding (NOT in the source).**
> Outputs are displayed to **2 dp** (e.g. `3.75 mg`). The source specifies no rounding.
> Full precision is retained internally. → Confirm 2 dp is acceptable.

---

## 5. State machine + supported-set gate

| Situation | Behaviour | Source basis |
|-----------|-----------|--------------|
| No input | verbatim `DEFAULT_ANSWER` ("…provide a supported steroid and dose in mg.") | `## DEFAULT_ANSWER` |
| Steroid OR dose missing | verbatim `MISSING_INPUT` ("Please provide the steroid and dose in mg, for example: methylprednisone 8 mg.") | `### MISSING_INPUT` |
| Steroid **not** in the supported five | verbatim `UNSUPPORTED_STEROID` ("I can calculate equivalence only for methylprednisone, dexamethasone, hydrocortisone, prednisolone, and fludrocortisone.") | `### UNSUPPORTED_STEROID` |
| dose outside 0–10000 mg | ask to confirm, compute nothing | `out_of_clinical_policy` |
| supported steroid + dose | compute all five + footer table | `### CALCULATED_EQUIVALENCE` |

The unsupported-steroid path is a **lookup miss** (the steroid is not a key in the
reference table) → the verbatim message, never a guessed conversion.

---

## 6. Footer (source `## DEFAULT_FOOTER`) — VERBATIM generic table

The generic equivalence table (equivalent dose · glucocorticoid:mineralocorticoid
activity · duration) is reproduced character-for-character in `footer:`. Confirm the five
activity ratios and durations (e.g. dexamethasone `30:0`, `36-54 h`; fludrocortisone
`10:250`, `24 h`).

Boundaries carried (`notes:`): mathematical equivalence only — not treatment, tapering,
adrenal-insufficiency, pediatric, or pregnancy advice; **perioperative** hydrocortisone
supplementation belongs to the separate perioperative steroid guide (not yet migrated).

---

## Sign-off checklist (owner L)

- [ ] Reference doses (the five numbers) match the source, in both the lookup table and the multipliers (§1–2).
- [ ] Worked examples A/B/C re-derived and correct (§3).
- [ ] Display rounding (2 dp) accepted (§4).
- [ ] State machine incl. unsupported-steroid + missing-input behaviour correct (§5).
- [ ] Footer generic table verbatim (§6).
