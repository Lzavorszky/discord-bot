# echo_ero_rvol — clinical hand-check (Plan D, final calculator migration)

**Source:** `protocols/echo_ero_rvol.txt` (v0.1) + `echo_ero_rvol.route_claims.json`
**Migrated to:** `id_bot2/protocols/echo_ero_rvol.yaml` (**kind: `calculator`**, 10 methods)
**Tool:** `id_bot2/tools/calculate.py`  ·  **Tests:** `id_bot2/tests/test_calculate.py`, `id_bot2/tests/test_router.py`
**Status:** owner sign-off **PENDING** (owner L).

Confirm formulas vs source, confirm the worked examples per method, and decide the flagged
**engine decisions** — note decision C (slots the source names in formulas but omits from the
SLOT_SCHEMA) needs a clinical OK.

---

## 0. Safety + unit ambiguity
Declared formulas, restricted AST, hand-computed tests, hard verifier. Radius/VTI (mm or cm)
and velocities (m/s or cm/s) carry a required `*_unit` enum driving a `lookup -> multiply`
normalize step; missing/unknown unit -> verbatim `unsupported_value`, never a silent
conversion. **"RV" in this protocol means regurgitant volume, never right ventricle** (carried
in `notes`, per source RESTRICTED_OUTPUTS / SAFETY_RULE).

---

## 1. Formulas — source `## SELECTION_RULES` ⟶ migrated `expr` (VERBATIM)

| Source rule | Migrated `expr` |
|-------------|-----------------|
| `PISA_area_cm2 = 2 * pi * pisa_radius_cm^2` | `2 * pi * pisa_radius_cm ** 2` |
| `corrected_PISA_area = PISA_area * (angle / 180)` | `pisa_area_cm2 * (flow_convergence_angle_degrees / 180)` |
| `Regurgitant_flow_ml_s = PISA_area_cm2 * aliasing_velocity_cm_s` | `<area> * aliasing_velocity_cms` |
| `EROA_cm2 = Regurgitant_flow_ml_s / peak_regurgitant_velocity_cm_s` | `regurgitant_flow_ml_s / peak_regurgitant_velocity_cms` |
| `RVol_ml = EROA_cm2 * regurgitant_VTI_cm` | `eroa_cm2_calc * regurgitant_vti_cm` (PISA) / `eroa_cm2 * regurgitant_vti_cm` (direct) |
| `EROA_cm2 = RVol_ml / regurgitant_VTI_cm` | `regurgitant_volume_ml / regurgitant_vti_cm` |
| `RVol_ml = SV_regurgitant_valve - SV_competent_valve` | `regurgitant_valve_stroke_volume_ml - competent_valve_stroke_volume_ml` |
| `Regurgitant_fraction_percent = 100 * RVol_ml / SV_regurgitant_valve` | `100 * rvol_ml / regurgitant_valve_stroke_volume_ml` |
| `LV_stroke_volume = LV_EDV - LV_ESV` | `lv_edv - lv_esv` |
| `RVol_ml = LV_stroke_volume - forward_stroke_volume` | `lv_stroke_volume_ml - forward_stroke_volume_ml` |

✅ Please confirm each row, especially that the angle correction divides by **180** and PISA
uses the hemispheric `2·π·r²`.

## 1b. Methods (first-satisfiable-wins), most-specific first

1 `pisa_angle_vti` · 2 `pisa_vti` · 3 `pisa_angle` · 4 `pisa` · 5 `direct_rvol` ·
6 `direct_eroa` · 7 `volumetric_eroa` · 8 `volumetric` · 9 `lv_eroa` · 10 `lv`.
EROA-only PISA variants (3,4) drop RVol when no regurgitant VTI is supplied; angle variants
(1,3) apply the `(angle/180)` correction, hemispheric variants (2,4) do not. ✅ Confirm.

---

## 2. Worked examples (re-derive by hand)

**PISA (hemispheric), r 1.0 cm, aliasing 40 cm/s, peak 500 cm/s, reg VTI 100 cm:**
- PISA area = 2·π·1² = 6.28 cm²; flow = 6.28·40 = 251.3 mL/s;
  EROA = 251.3/500 = **0.50 cm²**; RVol = 0.50·100 = **50.3 mL**.
- 90° angle correction halves the area (×0.5) → EROA halves (`test_echo_ero_angle_correction_halves_at_90deg`).

**Direct:** EROA 0.5 + VTI 100 → RVol = 50 mL. RVol 60 + VTI 120 → EROA = 0.50 cm².

**Volumetric:** regurg SV 100, competent SV 60 → RVol = 40 mL; RF = 100·40/100 = 40 %;
EROA = 40/100 = 0.40 cm².

**LV-volume:** EDV 120, ESV 50 → LV SV 70; forward 40 → RVol = 30 mL.

(Tests `test_echo_ero_*`; harness `echo_pisa_eroa_rvol`, `echo_direct_rvol_from_eroa`,
`echo_volumetric_rvol`, `echo_lv_volume_rvol`.) ✅ Confirm.

---

## 3. Gating
no input -> verbatim `default_answer` (PISA-vs-volumetric proposal); incomplete single method ->
verbatim `missing_inputs`; a measurement without its unit (e.g. aliasing velocity) ->
`unsupported_value` ("resend … velocities in m/s or cm/s …"); out-of-range unambiguous slot ->
`needs_confirmation`. All tested.

---

## 4. Flagged engine decisions (owner to confirm)

- **A. Display rounding** not in source (areas/EROA 2 dp, volumes 1 dp, RF 0 dp).
- **B. Ranges only on unit-unambiguous slots** (eroa_cm2, regurgitant_volume_ml, angle, the mL
  stroke-volume slots, lv_edv/esv).
- **C. Added input slots the source formulas NAME but the SLOT_SCHEMA omits:**
  `regurgitant_valve_stroke_volume_ml`, `competent_valve_stroke_volume_ml`,
  `forward_stroke_volume_ml` (all mL). The source `## SELECTION_RULES` reference these by name
  (the volumetric subtraction and the LV-volume method), but `## SLOT_SCHEMA` lists only a
  single generic `stroke_volume`, plus `annulus_diameter`/`annulus_vti`. **A single annulus
  pair cannot yield the two distinct stroke volumes the subtraction needs**, so those three
  source slots are omitted and the two named stroke volumes (+ forward SV) are taken directly
  in mL. No formula was invented — only the inputs the source formulas already name were
  declared. **Please confirm this is acceptable** (or specify how the two stroke volumes
  should be supplied/derived).
- **D. `unsupported_units` instruction → direct ask message** (as in the other echo sheets).
- **E. "RV = regurgitant volume" caveat** carried in `notes` (source SAFETY_RULE).

## 5. Sign-off checklist
- [ ] Formulas verbatim, angle /180, hemispheric 2·π·r² (§1)
- [ ] Method ordering (§1b)
- [ ] Worked examples 0.50/50.3, 40/40%/0.40, 30 (§2)
- [ ] Unit-ambiguity asks (§3)
- [ ] **Decision C — added stroke-volume slots** (clinical OK needed)
- [ ] Decisions A, B, D, E
