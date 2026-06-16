# ampicillin/sulbactam (high-dose) — Clinical Hand-Check Sheet

**Protocol ID:** `ampsul`
**Source:** ampicillin/sulbactam
**YAML version:** 0.1
**Date migrated:** 2026-06-16
**Sign-off status:** ☐ PENDING — owner L must verify every row before go-live

---

## 1. Aliases (check completeness)

`ampicillin/sulbactam`, `ampicillin sulbactam`, `ampicillin-sulbactam`, `amp sul`, `amp/sul`, `amp-sul`, `ampsul`, `high-dose sulbactam`, `sulbactam high-dose`, `CRAB protocol`, `MACI protocol`, `unasyn`, `unacid`, `sultamicillin`

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
| LOADING *(always_show)* | sulbactam 1.5 g + ampicillin 3 g once | start of therapy, then start continuous infusion immediately | — |
| CRRT_OR_GFR_GE_60 | sulbactam 9 g/day + ampicillin 18 g/day | CRRT or GFR >=60 | continuous infusion; dissolve total daily dose in 250 mL NaCl 0.9%, 11 mL/h |
| GFR_30_TO_60 | sulbactam 6 g/day + ampicillin 12 g/day | GFR 30-60 | continuous infusion preferred |
| GFR_15_TO_30 | sulbactam 3 g/day + ampicillin 6 g/day | GFR 15-30 | continuous infusion preferred |
| GFR_LT_15 | sulbactam 2 g/day + ampicillin 4 g/day | GFR <15 | administer once daily; dilute in 100 mL NaCl 0.9% |
| IHD | sulbactam 1.5 g/day + ampicillin 3 g/day | IHD | administer once daily after IHD (if scheduled); dilute in 100 mL NaCl 0.9% |

**Verify each row against source `ampicillin/sulbactam`:**
- [ ] Dose string exact match (value + unit)?
- [ ] When / GFR cutoff exact match?
- [ ] Admin / pump rate exact match (CI drugs)?
- [ ] `always_show` tiers correct (LOADING only)?

---

## 4. Selection ladder

  1. `if ihd` → tier `IHD`
  2. `if crrt` → tier `CRRT_OR_GFR_GE_60`
  3. `if gfr >= 60` → tier `CRRT_OR_GFR_GE_60`
  4. `if gfr >= 30 and gfr < 60` → tier `GFR_30_TO_60`
  5. `if gfr >= 15 and gfr < 30` → tier `GFR_15_TO_30`
  6. `if gfr < 15` → tier `GFR_LT_15`
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
- treatment advice for non-Acinetobacter infections from this protocol
- monotherapy advice for severe MACI/CRAB infection

**Verify:** Are the `never` items complete and clinically appropriate?

---

## 7. Footer / prep notes

**Footer:** High-dose sulbactam for severe MACI/CRAB only. Requires: proven MACI infection, sulbactam inhibition zone >=11 mm, combination with a second active agent (e.g. colistin, tigecycline).

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

