# echo_ava — clinical hand-check (Plan D, final calculator migration)

**Source:** `protocols/echo_ava.txt` (v0.1) + `echo_ava.route_claims.json`
**Migrated to:** `id_bot2/protocols/echo_ava.yaml` (**kind: `calculator`**, 7 methods)
**Tool:** `id_bot2/tools/calculate.py`  ·  **Tests:** `id_bot2/tests/test_calculate.py`, `id_bot2/tests/test_router.py`
**Status:** owner sign-off **PENDING** (owner L).

Confirm every migrated **formula** vs source, confirm the worked examples per method, and
decide the flagged **engine decisions**.

---

## 0. Safety story + unit ambiguity
Same as the other calculators (declared formulas, restricted AST, hand-computed tests, hard
verifier). Ambiguous measurements (LVOT diameter/VTIs in mm or cm; Vmax in m/s or cm/s) carry
a required `*_unit` enum driving a `lookup -> multiply` normalize step; missing/unknown unit
-> verbatim `unsupported_value` ("resend with units"), never a silent /10 or *100.

---

## 1. Slots (source `## SLOT_SCHEMA`)

| Slot | Source unit | Migrated handling |
|------|-------------|-------------------|
| `lvot_diameter` | mm_or_cm | + `lvot_diameter_unit` [mm, cm]; no range gate |
| `lvot_csa` | cm2 | range **0.2–10, ask_confirmation** (verbatim) |
| `lvot_vti` | cm (rules: mm_or_cm) | + `lvot_vti_unit` [mm, cm]; no range gate |
| `av_vti` | cm (rules: mm_or_cm) | + `av_vti_unit` [mm, cm]; no range gate |
| `bsa_m2` | m2 | range **0.3–3.5, ask_confirmation** (verbatim) |
| `lvot_vmax` | m/s or cm/s | + `lvot_vmax_unit` [m_per_s, cm_per_s]; no range gate |
| `av_vmax` | m/s or cm/s | + `av_vmax_unit` [m_per_s, cm_per_s]; no range gate |

---

## 2. Formulas — source `## SELECTION_RULES` ⟶ migrated `expr` (VERBATIM)

| Source rule | Migrated `expr` |
|-------------|-----------------|
| `LVOT_CSA_cm2 = pi * (LVOT_diameter_cm / 2)^2` | `pi * (lvot_diameter_cm / 2) ** 2` |
| `AVA_cm2 = LVOT_CSA_cm2 * LVOT_VTI_cm / AV_VTI_cm` | `lvot_csa_cm2 * lvot_vti_cm / av_vti_cm` |
| `Dimensionless_index = LVOT_VTI_cm / AV_VTI_cm` | `lvot_vti_cm / av_vti_cm` |
| `Indexed_AVA_cm2_m2 = AVA_cm2 / BSA_m2` | `ava_cm2 / bsa_m2` |
| `velocity_ratio = LVOT_Vmax / AV_Vmax` | `lvot_vmax_cms / av_vmax_cms` |
| `simplified_AVA_cm2 = LVOT_CSA_cm2 * LVOT_Vmax / AV_Vmax` | `lvot_csa_cm2 * lvot_vmax_cms / av_vmax_cms` |
| (CSA direct) "use it directly" | `lvot_csa_cm2 = lvot_csa` |

✅ Please confirm each row.

## 2b. Method order (first-satisfiable-wins) — realises "prefer VTI continuity"

| # | Method | requires |
|---|--------|----------|
| 1 | `ava_indexed_csa` | lvot_vti, av_vti, lvot_csa, bsa_m2 |
| 2 | `ava_indexed_diameter` | lvot_vti, av_vti, lvot_diameter, bsa_m2 |
| 3 | `ava_csa` | lvot_vti, av_vti, lvot_csa |
| 4 | `ava_diameter` | lvot_vti, av_vti, lvot_diameter |
| 5 | `velocity_ratio_csa` | lvot_vmax, av_vmax, lvot_csa |
| 6 | `velocity_ratio_diameter` | lvot_vmax, av_vmax, lvot_diameter |
| 7 | `velocity_ratio` | lvot_vmax, av_vmax |

VTI methods (1–4) precede velocity methods (5–7), so when VTIs are present the continuity
equation is used (source: *"Prefer the VTI continuity-equation AVA when VTI measurements are
available."*). ✅ Confirm ordering.

---

## 3. Worked examples (re-derive by hand)

**Continuity (diameter), diameter 2.0 cm, LVOT VTI 20 cm, AV VTI 100 cm:**
- CSA = π·1² = 3.14 cm²; AVA = 3.14·20/100 = **0.63 cm²**; DI = 20/100 = **0.20**.

**Indexed (CSA direct), CSA 3.0, LVOT VTI 20, AV VTI 100, BSA 2.0:**
- AVA = 3.0·20/100 = **0.60 cm²**; Indexed = 0.60/2.0 = **0.30 cm²/m²**.

**Velocity ratio, LVOT Vmax 1.0 m/s, AV Vmax 4.0 m/s, CSA 3.0:**
- ratio = 1/4 = **0.25**; simplified AVA = 3.0·0.25 = **0.75 cm²**.

(Tests: `test_echo_ava_*`; harness: `echo_ava_continuity`, `echo_ava_indexed_csa_direct`,
`echo_ava_velocity_ratio`.) ✅ Confirm.

---

## 4. Gating
no input -> verbatim `default_answer`; measurements but missing a needed unit ->
`unsupported_value` ("resend … mm or cm, velocities m/s or cm/s, BSA m2"); CSA=50 (out of
0.2–10) -> `needs_confirmation`; partial -> `missing_inputs`. All tested.

---

## 5. Flagged engine decisions (owner to confirm)

- **A. Display rounding** not in source (AVA/DI/indexed 2 dp, VTI 1 dp).
- **B. Ranges only on unit-unambiguous slots** (lvot_csa, bsa); see cardiac-output sheet B.
- **C. Source SLOT_SCHEMA lists VTI unit as `cm`, but the `## DEFAULT_ANSWER` and
  `## SELECTION_RULES` treat VTIs as mm_or_cm and convert.** We follow the rules: VTIs require
  a unit and are normalized. If the team prefers VTIs be cm-only (no unit asked), say so.
- **D. SAFETY_RULE "if diameter-derived and directly-supplied LVOT CSA disagree, ask which to
  use" is NOT implemented.** When LVOT CSA is supplied it is used directly (CSA-direct methods
  ordered ahead of diameter methods); a supplied diameter is then ignored for CSA. Confirm
  acceptable, or specify the compare-and-ask behaviour for a follow-up.
- **E. Velocity-ratio templates state "Prefer the VTI continuity-equation AVA when available"**
  (verbatim source intent), and simplified AVA is shown only when CSA/diameter is available.

## 6. Sign-off checklist
- [ ] Formulas verbatim (§2)
- [ ] Method ordering prefers VTI continuity (§2b)
- [ ] Worked examples 0.63 / 0.30 / 0.75 (§3)
- [ ] Unit-ambiguity asks (§4)
- [ ] Decisions A–E (esp. D: CSA-disagreement not implemented)
