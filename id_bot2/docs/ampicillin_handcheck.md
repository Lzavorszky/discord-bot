# Ampicillin — Clinical Hand-Check Sheet

**Protocol ID:** `ampicillin`
**Source:** uploaded antibiotic renal dosing DOCX - ampicillin
**YAML version:** 0.1
**Date migrated:** 2026-06-16
**Sign-off status:** ☐ PENDING — owner L must verify every row before go-live

---

## 1. Aliases (check completeness)

`ampicillin`, `ampicillin dose`, `ampicillin dosing`, `ampicillin iv`

**Verify:** Do these aliases capture all names clinicians use? Any missing (trade names, abbreviations, language variants)?

---

## 2. Input slots

- `gfr`: number (mL/min)
- `crrt`: bool
- `ihd`: bool
- `tdm_low_level`: bool

**Verify:** Are the correct slots present? Any clinical modifier that can affect dose is missing?

---

## 3. Dosing tiers — verbatim from YAML vs source

| Tier | Dose | When | Admin |
|------|------|------|-------|
| LOADING *(always_show)* | 4 g once | start of therapy | start continuous infusion immediately |
| NORMAL | 12 g/day | GFR >=20 | 4 g/250 mL, 31.3 mL/h |
| SEVERE_AKI | 8 g/day | GFR <20 | 4 g/250 mL, 20.8 mL/h |
| ANURIA_IHD | 4 g/day | anuria or IHD | 4 g/250 mL, 10.4 mL/h |
| CRRT | 12 g/day | CRRT | 4 g/250 mL, 31.3 mL/h |
| STEP_UP | 16 g/day | low levels | 4 g/250 mL, 41.6 mL/h |

**Verify each row against source `uploaded antibiotic renal dosing DOCX - ampicillin`:**
- [ ] Dose string exact match (value + unit)?
- [ ] When / GFR cutoff exact match?
- [ ] Admin / pump rate exact match (CI drugs)?
- [ ] `always_show` tiers correct (LOADING only)?

---

## 4. Selection ladder

  1. `if tdm_low_level` → tier `STEP_UP`
  2. `if ihd` → tier `ANURIA_IHD`
  3. `if crrt` → tier `CRRT`
  4. `if gfr >= 20` → tier `NORMAL`
  5. `if gfr < 20` → tier `SEVERE_AKI`
  6. `default` → `DEFAULT_ANSWER`

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
- Step-up dose unless low levels or low exposure/TDM context is explicitly present
- alternative antibiotics, duration, or indication advice

**Verify:** Are the `never` items complete and clinically appropriate?

---

## 7. Footer / prep notes

**Footer:** Doses for severe invasive infections; for routine infections or prophylaxis use textbook dosing.

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

