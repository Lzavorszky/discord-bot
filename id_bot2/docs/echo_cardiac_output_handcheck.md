# echo_cardiac_output — clinical hand-check (Plan D, final calculator migration)

**Source:** `protocols/echo_cardiac_output.txt` (v0.1) + `echo_cardiac_output.route_claims.json`
**Migrated to:** `id_bot2/protocols/echo_cardiac_output.yaml` (**kind: `calculator`**)
**Tool:** `id_bot2/tools/calculate.py`  ·  **Tests:** `id_bot2/tests/test_calculate.py`, `id_bot2/tests/test_router.py`
**Status:** owner sign-off **PENDING** (owner L). Nothing clinical ships on the migrator's say-so.

This is the non-delegable clinical check: confirm every migrated **formula** matches the
source character-for-character, confirm the worked examples, and decide the flagged
**engine decisions** (esp. the unit-ambiguity handling, new to this chunk).

---

## 0. The computing-kind safety story (unchanged from body_size/steroid)

1. **Formulas live in the YAML, not in code** — copied verbatim from `## SELECTION_RULES`.
2. **Arithmetic is a restricted AST, never `eval`** — only `+ - * / // % **`, unary minus,
   parens, numeric literals, declared names, `pi`, and `sqrt/abs/min/max/round`.
3. **Every formula has a unit test with a hand-computed expected value**; the phrased answer
   is checked by the grounding verifier in `calculator` (**hard**) mode.

## 0b. NEW this chunk — unit ambiguity handled without engine change

LVOT diameter and LVOT VTI may be mm **or** cm. Source SAFETY_RULE: *"If the unit is
ambiguous, ask for clarification rather than guessing."* Modelled as: each ambiguous
measurement has a required `*_unit` enum slot (`mm`/`cm`) that drives a declared
`lookup -> multiply` normalize step (`mm -> *0.1`, `cm -> *1`). **If the unit is absent or
unknown, the lookup misses and the tool returns the verbatim `unsupported_value` ("resend
with units") — never a silent /10.** Please confirm this is the desired behaviour.

---

## 1. Slots (source `## SLOT_SCHEMA`)

| Slot | Source unit | Migrated handling |
|------|-------------|-------------------|
| `lvot_diameter` | mm_or_cm | value + required `lvot_diameter_unit` enum [mm, cm]; **no range gate** (see decision B) |
| `lvot_vti` | cm (rules say mm_or_cm) | value + required `lvot_vti_unit` enum [mm, cm]; no range gate |
| `heart_rate_bpm` | bpm | clinical range **1–250, ask_confirmation** (verbatim) |

---

## 2. Formulas — source line ⟶ migrated `expr` (VERBATIM)

| # | Source "Calculation rule" | Migrated `expr` |
|---|---------------------------|-----------------|
| 1 | `LVOT_diameter_cm = LVOT_diameter_mm / 10 if supplied in mm` | normalize lookup `{mm: 0.1, cm: 1}` then `lvot_diameter * lvot_diam_factor` |
| 2 | `LVOT_CSA_cm2 = pi * (LVOT_diameter_cm / 2)^2` | `pi * (lvot_diameter_cm / 2) ** 2` |
| 3 | `Stroke_volume_ml = LVOT_CSA_cm2 * LVOT_VTI_cm` | `lvot_csa_cm2 * lvot_vti_cm` |
| 4 | `Cardiac_output_L_min = Stroke_volume_ml * heart_rate_bpm / 1000` | `stroke_volume_ml * heart_rate_bpm / 1000` |

VTI normalized the same way as the diameter (mm -> *0.1). ✅ Please confirm rows 1–4.

## 2b. Method selection (source: "return stroke volume only" when no HR)

| Method | requires | returns |
|--------|----------|---------|
| `calculated_co` (first) | lvot_diameter, lvot_vti, heart_rate_bpm | SV + CO |
| `calculated_sv` | lvot_diameter, lvot_vti | SV only |

First-satisfiable-wins: HR present -> CO; HR absent -> SV only. ✅ Confirm.

---

## 3. Worked example (re-derive by hand) — diameter 2.0 cm, LVOT VTI 20 cm, HR 70

- LVOT CSA = π·(2.0/2)² = π·1 = **3.14 cm²**
- Stroke volume = 3.14 · 20 = **62.8 mL**
- Cardiac output = 62.8 · 70 / 1000 = **4.40 L/min**

Engine output (`test_echo_co_hand_values`, harness `echo_co_compute`):
```
Echo LVOT cardiac output:
- LVOT diameter: 2.00 cm
- LVOT CSA: 3.14 cm2
- LVOT VTI: 20.0 cm
- Stroke volume: 62.8 mL
- Heart rate: 70 bpm
- Cardiac output: 4.40 L/min
```
mm variant (20 mm / 200 mm) gives the identical CO (`test_echo_co_mm_equals_cm`). ✅ Confirm.

---

## 4. Gating (state machine) — all tested

| Input | Outcome |
|-------|---------|
| no input | verbatim `default_answer` ("Provide LVOT VTI and LVOT diameter…") |
| only diameter | verbatim `missing_inputs` ask |
| diameter+VTI present, **no unit** | verbatim `unsupported_value` ("resend … in mm or cm") |
| HR = 400 (out of 1–250) | `needs_confirmation`, no formula run |

---

## 5. Flagged engine decisions (owner to confirm)

- **A. Display rounding** (CSA/CO 2 dp, VTI/SV 1 dp) is an engine presentation choice, NOT in
  the source. Full precision retained in `result.values`.
- **B. Clinical ranges enforced only on the unit-unambiguous slot (heart_rate).** A range
  cannot be soundly applied to a mm_or_cm value before its unit is known (10 may be 10 mm =
  1 cm, in range, or 10 cm, out of range). The unit requirement is the guard for those slots.
- **C. `unsupported_units` instruction → message.** Source `## SELECTED_OUTPUTS` phrases it as
  "Ask the user to resend…"; migrated as the direct ask "Please resend LVOT diameter and LVOT
  VTI in mm or cm…". No clinical content changed.
- **D. Empty `## DEFAULT_FOOTER`** in source → no footer emitted.

## 6. Sign-off checklist
- [ ] Formulas 1–4 verbatim
- [ ] Method selection (CO needs HR; SV otherwise)
- [ ] Worked example 4.40 L/min
- [ ] Unit-ambiguity asks (decision A–B–C)
- [ ] Display rounding (decision A)
