# body_size_calculators — clinical hand-check (Plan D, the final migration phase)

**Source:** `protocols/body_size_calculators.txt` (v0.1) + `body_size_calculators.route_claims.json`
**Migrated to:** `id_bot2/protocols/body_size_calculators.yaml` (**kind: `calculator`** — a NEW engine mode)
**Tool:** `id_bot2/tools/calculate.py`  ·  **Tests:** `id_bot2/tests/test_calculate.py`
**Status:** owner sign-off **PENDING** (owner L). Nothing clinical ships on the migrator's say-so.

This is the non-delegable clinical check: confirm every migrated **formula** matches the
source character-for-character, confirm the worked examples below, and decide the two
flagged **engine decisions**.

---

## 0. Why this protocol is different — the only COMPUTING kind

Every other tool in the rebuild SELECTS verbatim text. A calculator must do arithmetic.
The safety story (so this stays as trustworthy as a verbatim select):

1. **Formulas live in the YAML, not in code** — copied verbatim from the source
   `## SELECTION_RULES` "Calculation rules". The tool is a generic evaluator; it invents
   no formula.
2. **Arithmetic is evaluated by a restricted AST, never `eval`** — only `+ - * / // % **`,
   unary minus, parentheses, numeric literals, the declared slot/intermediate names, the
   constant `pi`, and a closed function whitelist (`sqrt`, `abs`, `min`, `max`, `round`).
   Anything else (attribute, subscript, call, comprehension, unknown name) raises.
3. **Every formula has a unit test with a hand-computed expected value** and the phrased
   answer is checked by the grounding verifier in `calculator` (**hard**) mode.

---

## 1. Slots (source `## SLOT_SCHEMA`) — cm + kg only, verbatim ranges

| Slot | Unit | clinical_min | clinical_max | out-of-range policy |
|------|------|-------------:|-------------:|---------------------|
| `height_cm` | cm | 80 | 250 | ask_confirmation |
| `actual_weight_kg` | kg | 1 | 350 | ask_confirmation |

Source unit rule (carried): accept height only in cm, weight only in kg; if supplied in
another unit, **ask** the user to resend in cm and kg (no lb/in conversion). Confirm.

---

## 2. Formulas — source line ⟶ migrated `expr` (VERBATIM)

| # | Source "Calculation rule" | Migrated `expr` |
|---|---------------------------|-----------------|
| 1 | `height_m = height_cm / 100` | `height_cm / 100` |
| 2 | `BMI = actual_weight_kg / (height_m * height_m)` | `actual_weight_kg / (height_m ** 2)` |
| 3 | `BSA_Mosteller_m2 = square_root((height_cm * actual_weight_kg) / 3600)` | `sqrt((height_cm * actual_weight_kg) / 3600)` |
| 4 | `IBW_male_kg = 50 + 0.91 * (height_cm - 152.4)` | `50 + 0.91 * (height_cm - 152.4)` |
| 5 | `IBW_female_kg = 45.5 + 0.91 * (height_cm - 152.4)` | `45.5 + 0.91 * (height_cm - 152.4)` |
| 6 | `Adjusted_body_weight_male_kg = IBW_male_kg + 0.4 * (actual_weight_kg - IBW_male_kg)` | `ibw_male_kg + 0.4 * (actual_weight_kg - ibw_male_kg)` |
| 7 | `Adjusted_body_weight_female_kg = IBW_female_kg + 0.4 * (actual_weight_kg - IBW_female_kg)` | `ibw_female_kg + 0.4 * (actual_weight_kg - ibw_female_kg)` |

`height_m ** 2` is mathematically identical to `height_m * height_m`. ✅ Please confirm rows 1–7.

---

## 3. Worked example (re-derive by hand) — height 170 cm, weight 70 kg

| Quantity | Hand calculation | Result | Displayed |
|----------|------------------|-------:|-----------|
| height_m | 170 / 100 | 1.70 | — |
| BMI | 70 / 1.70² = 70 / 2.89 | 24.2215… | **24.2 kg/m2** |
| BSA (Mosteller) | √(170·70/3600) = √3.30556 | 1.81812… | **1.82 m2** |
| IBW (male) | 50 + 0.91·(170−152.4) = 50 + 16.016 | 66.016 | **66.0 kg** |
| IBW (female) | 45.5 + 0.91·17.6 = 45.5 + 16.016 | 61.516 | **61.5 kg** |
| AdjBW (male IBW) | 66.016 + 0.4·(70−66.016) | 67.6096 | **67.6 kg** |
| AdjBW (female IBW) | 61.516 + 0.4·(70−61.516) | 64.9096 | **64.9 kg** |

These exact values are pinned by `test_calculate.py::test_body_size_hand_values` and the
harness case `bmi_bsa_170_70`.

---

## 4. Engine decisions (please confirm)

> ⚠ **ENGINE DECISION 1 — display rounding (NOT in the source).**
> The source defines the formulas but specifies **no rounding**. The migration displays
> **BMI to 1 dp, BSA to 2 dp, weights to 1 dp**. The full-precision values are retained
> internally (in `result.values`); only the rendered text is rounded. → Confirm these
> display precisions are acceptable, or specify others.

> ⚠ **ENGINE DECISION 2 — both-sex reporting (matches source SAFETY_RULE).**
> The source says: "Where sex changes the equation, calculate and report **both** male and
> female formula outputs. Do not ask for sex." The migration reports both IBW and both
> AdjBW values and never asks for sex. → Confirm this is what you want (vs. asking for sex).

---

## 5. State machine (required-slot gating)

| Situation | Behaviour | Source basis |
|-----------|-----------|--------------|
| No input at all | verbatim `DEFAULT_ANSWER` ("Provide height in cm and actual body weight in kg…") | `## DEFAULT_ANSWER` |
| Height OR weight only | verbatim `MISSING_INPUT` ("Please provide height in cm and actual body weight in kg.") | `### MISSING_INPUT` |
| height/weight outside clinical range | **ask to confirm**, run no formula | `out_of_clinical_policy: ask_confirmation` |
| both present, in range | compute + render all six values + footer | `### CALCULATED_BODY_SIZE` |

Footer (verbatim): "Calculator output only; use the relevant clinical protocol to decide
which weight scalar applies."

---

## 6. Boundaries carried (source `## RESTRICTED_OUTPUTS` / `## INFO_BLOCKS`, kept in `notes:`)

Estimates only; do not determine which weight scalar a drug/nutrition/ventilator/renal
decision should use unless another active protocol says so; no lb/in conversion; no
pediatric or pregnancy interpretation; if actual weight ≤ IBW, AdjBW may not be the
appropriate dosing scalar. The tool computes **only** these formulas — it offers no
clinical interpretation.

---

## Sign-off checklist (owner L)

- [ ] Formulas 1–7 match the source verbatim (§2).
- [ ] Worked example 170 cm / 70 kg re-derived and correct (§3).
- [ ] Display rounding accepted (ENGINE DECISION 1).
- [ ] Both-sex reporting accepted (ENGINE DECISION 2).
- [ ] State machine + footer + boundaries correct (§5–6).
