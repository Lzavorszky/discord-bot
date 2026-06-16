# Clinical hand-check — `meropenem.yaml` vs source `meropenem.txt`

**Phase 2.4 sign-off sheet.** This is the human's non-delegable check: confirm every dose,
tier, cutoff, and administration string in the migrated `id_bot2/protocols/meropenem.yaml`
matches the source `protocols/antibiotics/meropenem.txt`. Nothing clinical ships on the
bot's say-so alone.

- **Source:** `protocols/antibiotics/meropenem.txt` (v0.3) + `meropenem.route_claims.json`
- **Migrated:** `id_bot2/protocols/meropenem.yaml` (kind: drug_dose)
- **Validator:** schema-valid, linter green (`python id_bot2/validate_protocols.py` ✓)

---

## 1. Dose tiers — every cell, source → YAML

| Tier (YAML key) | Field | Source `meropenem.txt` | `meropenem.yaml` | Match |
|---|---|---|---|:---:|
| **LOADING** | dose | `1 g once` | `1 g once` | ✓ |
|  | when | `start of therapy` | `start of therapy` | ✓ |
|  | admin | `start continuous infusion immediately` | `start continuous infusion immediately` | ✓ |
| **NORMAL** | dose | `3 g/day` | `3 g/day` | ✓ |
|  | when | `GFR 20+` | `GFR 20+` | ✓ |
|  | admin | `1 g/50 mL, 6.3 mL/h` | `1 g/50 mL, 6.3 mL/h` | ✓ |
| **SEVERE_AKI** | dose | `1 g/day` | `1 g/day` | ✓ |
|  | when | `GFR <20 or IHD` | `GFR <20 or IHD` | ✓ |
|  | admin | `0.5 g/50 mL, 4.2 mL/h` | `0.5 g/50 mL, 4.2 mL/h` | ✓ |
| **CRRT** | dose | `3 g/day` | `3 g/day` | ✓ |
|  | when | `CRRT` | `CRRT` | ✓ |
|  | admin | `1 g/50 mL, 6.3 mL/h` | `1 g/50 mL, 6.3 mL/h` | ✓ |
| **STEP_UP** | dose | `6 g/day` | `6 g/day` | ✓ |
|  | when | `low levels or CNS infection` | `low levels or CNS infection` | ✓ |
|  | admin | `1 g/50 mL, 12.5 mL/h` | `1 g/50 mL, 12.5 mL/h` | ✓ |

Note on tier key name: the source `## SELECTED_OUTPUTS` block names the step-up tier
`STEP_UP_DOSE`; the migrated file uses `STEP_UP` (the agreed mockup §1a target key). This is
an internal key only — no clinical value changes, and the displayed dose/when/admin are
identical. The LOADING row exists in the source DEFAULT_ANSWER table (no SELECTED_OUTPUTS
entry); migrated as `always_show: true` so it always appears with the table.

## 2. Selection logic — source SELECTION_RULES → YAML `select` ladder

List order = priority (highest first). Source priorities shown for cross-check.

| Order | Source rule (priority) | Condition | Selects | YAML rung | Match |
|---|---|---|---|---|:---:|
| 1 | STEP_UP_CNS / STEP_UP_LOW_LEVEL (110) | `cns_infection==true` OR `tdm_low_level==true` | STEP_UP_DOSE | `if: cns_infection or tdm_low_level → STEP_UP` | ✓ |
| 2 | IHD_AS_SEVERE_AKI (100) | `ihd==true` | SEVERE_AKI | `if: ihd → SEVERE_AKI` | ✓ |
| 3 | CRRT (95) | `crrt==true` | CRRT | `if: crrt → CRRT` | ✓ |
| 4 | NORMAL_GFR_GE_20 (70) | `gfr >= 20` | NORMAL | `if: gfr >= 20 → NORMAL` | ✓ |
| 5 | SEVERE_AKI_GFR_LT_20 (60) | `gfr < 20` | SEVERE_AKI | `if: gfr < 20 → SEVERE_AKI` | ✓ |
| 6 | DEFAULT (1) | no selection input | DEFAULT_ANSWER (full table) | `default: DEFAULT_ANSWER` | ✓ |

Safety ordering preserved: IHD (2) beats CRRT (3) beats numeric GFR (4–5), matching source
`## SAFETY_RULES` ("IHD has highest priority, then CRRT, then numeric GFR"). The two
priority-110 step-up rules (CNS / low-TDM) are merged into one `or` guard — same effect.

## 3. Slots — source SLOT_SCHEMA + optional_modifiers → YAML `slots`

| Slot | Source | YAML | Match |
|---|---|---|:---:|
| gfr | number, mL/min, clinical_min 0, clinical_max 250, out_of_clinical_policy `ask_confirmation` | `number, mL/min, min 0, max 250, on_out_of_range ask_confirmation` | ✓ |
| crrt | optional_modifier (bool) | `bool` | ✓ |
| ihd | optional_modifier (bool) | `bool` | ✓ |
| cns_infection | optional_modifier (bool) | `bool` | ✓ |
| tdm_low_level | optional_modifier (bool) | `bool` | ✓ |

## 4. Routing metadata — route_claims.json → YAML

| Field | Source (`route_claims.json`) | YAML | Match |
|---|---|---|:---:|
| answers | intents `[dose]`; owns drug `meropenem` | `answers_intents: [dose]`, `id: meropenem` | ✓ |
| refuses | excludes `targeted_treatment`, `coverage_question` | `refuses_intents: [coverage_question, targeted_treatment]` | ✓ |

## 5. Guardrails — RESTRICTED_OUTPUTS / SAFETY_RULES → YAML `never`

| Source restriction | YAML `never` entry | Match |
|---|---|:---:|
| meropenem dosing outside the listed table rows | dosing outside the listed tiers | ✓ |
| Step-up dose unless CNS infection or low exposure is explicitly present | STEP_UP unless cns_infection or tdm_low_level is present | ✓ |
| alternative antibiotics, duration, toxicity management, or indication advice | alternative antibiotics, duration, toxicity, or indication advice | ✓ |

## 6. Aliases

Migrated file carries the **full** source `## ALIASES` list verbatim (9 entries:
`meropenem, mero, MEM, meronem, meropenemum, meropenem iv, meropenem dose,
meropenem dosing, magas meropenem`). The mockup §1a showed only a 6-entry subset; the
real file keeps all of them so routing is not degraded. No collisions (linter ✓).

---

## ⚠ ONE DEVIATION TO APPROVE — reduced-dose preparation note

The source prints this line under the DEFAULT_ANSWER table and again in the `administration`
INFO_BLOCK:

> Reduced-dose preparation: dissolve 1 g in 20 mL NaCl 0.9%, withdraw 10 mL, dilute to
> 50 mL for a 0.5 g/50 mL syringe.

The `drug_dose` schema has **no dedicated prep/info field**. To avoid silently losing
clinical content, I preserved this line **verbatim at the top of `footer`** (footer is
free-text and always shown). The rest of the footer is the source DEFAULT_FOOTER verbatim.

**Your call:** (a) accept it living in `footer` as done; or (b) we add a `prep:` / `notes:`
field to the drug_dose schema in a follow-up and move it there. Either way, no clinical
value is lost or altered — only its location differs from source.

---

## Sign-off

- [ ] Tiers §1 — every dose/when/admin matches source
- [ ] Selection §2 — priority order matches source SAFETY_RULES
- [ ] Slots §3, routing §4, guardrails §5, aliases §6 — match
- [ ] Reduced-dose prep deviation — accept in `footer` (a) **or** request schema field (b)

Signed: ____________________   Date: __________
