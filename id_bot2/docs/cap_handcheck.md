# Clinical hand-check — `cap.yaml` (community-acquired respiratory infection)

**Source:** `protocols/cap.txt` (CAP, ID team, v0.1, status draft)
**Migrated:** `id_bot2/protocols/cap.yaml` (kind: pathway) · **Tool:** `select_pathway`
**Status: SIGN-OFF PENDING (owner L).** Nothing clinical ships on the migrator's say-so.

The migrated YAML SELECTS one of the protocol's own verbatim outputs; it never
composes a recommendation. `select_pathway` walks the `select:` ladder top-to-bottom
(list order = the source SELECTION_RULES priority) and returns the first matching
output's verbatim `text_en`/`text_hu`. CAP does not dose drugs — a follow-up dose
request routes to the relevant `drug_dose` protocol via `get_dose`.

## Selection ladder (verify priority order vs source)

| # | Source RULE (priority) | Guard in YAML | Output |
|---|------------------------|---------------|--------|
| 1 | INTUBATED_CAP (100) | `intubated or patient_status == 'intubated'` | INTUBATED_CAP |
| 2 | INFLUENZA (90) | `influenza` | INFLUENZA |
| 3 | ASPIRATION_PNEUMONIA (85) | `aspiration_event` | ASPIRATION_PNEUMONIA |
| 4 | COPD_ACUTE_EXACERBATION (80) | `copd_exacerbation` | COPD_ACUTE_EXACERBATION |
| 5 | HOSPITALIZED_NOSOCOMIAL_RISK (70) | `patient_status == 'hospitalized' and nosocomial_risk` | HOSPITALIZED_NOSOCOMIAL_RISK |
| 6 | HOSPITALIZED_STANDARD (60) | `patient_status == 'hospitalized'` | HOSPITALIZED_STANDARD |
| 7 | OUTPATIENT_VIRAL_POSITIVE (50) | `patient_status == 'dischargeable' and viral_test_result == 'positive'` | OUTPATIENT_VIRAL_POSITIVE |
| 8 | OUTPATIENT_ATYPICAL (45) | `patient_status == 'dischargeable' and atypical_suspicion` | OUTPATIENT_ATYPICAL |
| 9 | OUTPATIENT_STANDARD (40) | `patient_status == 'dischargeable'` | OUTPATIENT_STANDARD |
| 10 | DEFAULT (1) | terminal `default` | DEFAULT_ANSWER (quick map) |

## Output → therapy (verify each verbatim string matches source)

- INTUBATED_CAP → BioFire Pneumonia Panel from trachea/BAL; ceftriaxone until result.
- HOSPITALIZED_STANDARD → ceftriaxone + clarithromycin.
- HOSPITALIZED_NOSOCOMIAL_RISK → levofloxacin.
- OUTPATIENT_VIRAL_POSITIVE → antibiotics not required.
- OUTPATIENT_STANDARD → amoxicillin.
- OUTPATIENT_ATYPICAL → azithromycin.
- INFLUENZA → oseltamivir.
- ASPIRATION_PNEUMONIA → ceftriaxone only if secondary infection.
- COPD_ACUTE_EXACERBATION → ceftriaxone + clarithromycin.

## Modelling deviations (confirm acceptable)

1. **Redundant `viral_test_result != positive` guards dropped (rungs 8–9).** The
   source OUTPATIENT_ATYPICAL/OUTPATIENT_STANDARD rules carried a `viral_test_result
   != positive` clause. Because OUTPATIENT_VIRAL_POSITIVE (rung 7) is tested first,
   list-order priority already guarantees a viral-positive case never reaches rungs
   8–9 — so the clause is redundant and was removed (same engine convention used by
   the drug ladders). **Behaviourally identical.**
2. **LINKS / INFO_BLOCKS / RESTRICTED_OUTPUTS / SAFETY_RULES / `forbidden` lists not
   stored as schema fields.** The pathway schema has no field for them. Functionally
   preserved: select_pathway emits no dose (the "do not dose from CAP" rule is
   structural); dosing is forwarded by the router to `get_dose`; the router's shared
   safety prompt carries the no-outside-knowledge / no-identifiers / escalate-on-conflict
   rules. The HAP/VAP "do not add aliases" TODO and the steroid/methylprednisolone note
   are advisory and live only in the source `.txt`.
3. **`text_en`/`text_hu` carry the verbatim recommendation lines** (source
   `recommendation_hu`/`recommendation_en`), newline-joined. No wording changed.

## Linter note
`amoxicillin`, `azithromycin` and the non-drug item `BioFire Pneumonia Panel`,
`antibiotics_not_required` produce expected "does not resolve to a kind:drug_dose
protocol" **warnings** (those drugs aren't in the migrated formulary; the items are
not drugs). Warnings only — not errors.

## Sign-off checklist (owner)
- [ ] Ladder priority order matches source SELECTION_RULES.
- [ ] Each output's therapy text matches the source verbatim.
- [ ] Deviation 1 (dropped redundant guards) is clinically equivalent.
- [ ] Deviations 2–3 acceptable.
