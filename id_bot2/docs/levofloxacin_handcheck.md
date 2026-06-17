# Levofloxacin — Clinical Hand-Check Sheet

> **SIGNED OFF — owner L, 2026-06-17.** Doses/tiers/selection confirmed against source; faithful migration. (Batch 3c.)

**Protocol ID:** `levofloxacin`
**Source:** uploaded antibiotic renal dosing PDF
**YAML version:** 0.1
**Date migrated:** 2026-06-16
**Sign-off status:** ☐ PENDING — owner L must verify every row before go-live

---

## 1. Aliases (check completeness)

`levofloxacin`, `levofloxacin dose`, `levofloxacin dosing`

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
| NORMAL | 2 x 750 mg | GFR >=50 | — |
| GFR_20_TO_50 | 2 x 250 mg | GFR 20-50 | — |
| GFR_10_TO_20 | 2 x 125 mg | GFR 10-20 | — |
| GFR_LT_10 | 1 x 125 mg | GFR <10 | — |
| IHD | 1 x 125 mg | IHD | — |
| CRRT | 1 x 500 mg | CRRT | — |

**Verify each row against source `uploaded antibiotic renal dosing PDF`:**
- [ ] Dose string exact match (value + unit)?
- [ ] When / GFR cutoff exact match?
- [ ] Admin / pump rate exact match (CI drugs)?
- [ ] `always_show` tiers correct (LOADING only)?

---

## 4. Selection ladder

  1. `if ihd` → tier `IHD`
  2. `if crrt` → tier `CRRT`
  3. `if gfr >= 50` → tier `NORMAL`
  4. `if gfr >= 20 and gfr < 50` → tier `GFR_20_TO_50`
  5. `if gfr >= 10 and gfr < 20` → tier `GFR_10_TO_20`
  6. `if gfr < 10` → tier `GFR_LT_10`
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

**Footer:** Use only the dose tiers listed in this protocol.

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

