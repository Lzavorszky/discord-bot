# Imipenem/cilastatin/relebactam — Clinical Hand-Check Sheet

**Protocol ID:** `imipenem_cilastatin_relebactam`
**Source:** uploaded antibiotic renal dosing PDF
**YAML version:** 0.1
**Date migrated:** 2026-06-16
**Sign-off status:** ☐ PENDING — owner L must verify every row before go-live

---

## 1. Aliases (check completeness)

`imipenem/cilastatin/relebactam`, `imipenem cilastatin relebactam`, `recarbrio`, `imipenem relebactam`

**Verify:** Do these aliases capture all names clinicians use? Any missing (trade names, abbreviations, language variants)?

---

## 2. Input slots

- `gfr`: number (mL/min)
- `crrt`: bool
- `ihd`: bool

**Verify:** Are the correct slots present? Any clinical modifier that can affect dose is missing?

---

## 3. Dosing tiers — verbatim from YAML vs source

| Tier | Dose | When | Admin |
|------|------|------|-------|
| LOADING *(always_show)* | 1.25 g once | start of therapy, then continue scheduled dosing | — |
| NORMAL | 4 x 1.25 g | GFR >=90 | — |
| GFR_60_TO_90 | 4 x 1 g | GFR 60-90 | — |
| GFR_30_TO_60 | 4 x 0.75 g | GFR 30-60 | — |
| GFR_15_TO_30 | 4 x 500 mg | GFR 15-30 | — |
| IHD | 4 x 500 mg | IHD | — |
| CRRT | 4 x 1 g | CRRT | — |

**Verify each row against source `uploaded antibiotic renal dosing PDF`:**
- [ ] Dose string exact match (value + unit)?
- [ ] When / GFR cutoff exact match?
- [ ] Admin / pump rate exact match (CI drugs)?
- [ ] `always_show` tiers correct (LOADING only)?

---

## 4. Selection ladder

  1. `if ihd` → tier `IHD`
  2. `if crrt` → tier `CRRT`
  3. `if gfr >= 90` → tier `NORMAL`
  4. `if gfr >= 60 and gfr < 90` → tier `GFR_60_TO_90`
  5. `if gfr >= 30 and gfr < 60` → tier `GFR_30_TO_60`
  6. `if gfr >= 15 and gfr < 30` → tier `GFR_15_TO_30`
  7. `default` → `DEFAULT_ANSWER`

**Verify priority order against source SELECTION_RULES:**
- [ ] IHD has highest priority (or correct distinct tier)?
- [ ] CRRT second?
- [ ] GFR cutoffs in source order?
- [ ] Any STEP_UP / tdm_low_level rule correct priority vs renal rules?
- [ ] `default` → `DEFAULT_ANSWER` as terminal rung?

---

## 5. Guard expressions

All guards use Python boolean syntax (`and`, `or`, `not`, `>=`, `<=`, `>`, `<`, `==`).
A `None` operand (un-supplied slot) makes a comparison `False` → rung skips silently.

**Verify:**
- [ ] Strict vs ≥ boundaries match source (e.g. `gfr > 20` vs `gfr >= 20`)?
- [ ] AND boundaries (e.g. `gfr >= 30 and gfr <= 90`) cover the right range?
- [ ] Any intentional gap (GFR range with no tier) preserved — falls to DEFAULT_ANSWER?

---

## 6. Guardrails

- dosing outside the listed tiers

**Verify:** Are the `never` items complete and clinically appropriate?

---

## 7. Footer / prep notes

**Footer:** 1.25 g = imipenem/cilastatin/relebactam 500/500/250 mg. Use only the dose tiers listed in this protocol.

**Prep:** *(none)*

**Verify:** Clinical notes accurate? Preparation instructions match source? Footer placeholder text (if any) must be replaced before go-live.

---

## 8. Sign-off checklist

- [ ] All tier doses correct
- [ ] All tier GFR cutoffs / RRT conditions correct
- [ ] Admin / pump rates correct (for CI drugs)
- [ ] Selection priority correct
- [ ] Aliases complete
- [ ] Guardrails appropriate
- [ ] Footer / prep notes accurate

**Sign-off:** _______________________________ Date: _______________

**Signed by (owner L):** ☐ Approved as-is ☐ Approved with corrections noted below

**Corrections / notes:**

