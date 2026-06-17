# Clinical hand-check — `uti.yaml` (urinary tract infection)

**Source:** `protocols/uti.txt` (UTI, ID team, v0.1, draft) → `id_bot2/protocols/uti.yaml` (kind: pathway)
**Tool:** `select_pathway` · **Status: SIGN-OFF PENDING (owner L).**

select_pathway returns one of the protocol's own verbatim outputs (list order =
source priority). UTI does not dose drugs.

## Selection ladder

| # | Source RULE (priority) | Guard in YAML | Output |
|---|------------------------|---------------|--------|
| 1 | ASYMPTOMATIC_BACTERIURIA (100) | `asymptomatic_bacteriuria or syndrome_class == 'asymptomatic_bacteriuria'` | ASYMPTOMATIC_BACTERIURIA |
| 2 | COMPLICATED_HOSP_NOSOCOMIAL (90/89) | `(complicated or syndrome_class == 'complicated_uti') and patient_status == 'hospitalized' and nosocomial_risk` | COMPLICATED_HOSPITALIZED_NOSOCOMIAL_RISK |
| 3 | COMPLICATED_HOSPITALIZED (80/79) | `(complicated or syndrome_class == 'complicated_uti') and patient_status == 'hospitalized'` | COMPLICATED_HOSPITALIZED |
| 4 | COMPLICATED_DISCHARGEABLE (70/69) | `(complicated or syndrome_class == 'complicated_uti') and patient_status == 'dischargeable'` | COMPLICATED_DISCHARGEABLE |
| 5 | CATHETER_ASSOCIATED (65) | `catheter_associated` | CATHETER_ASSOCIATED_UTI_DIAGNOSTICS |
| 6 | UNCOMPLICATED (60) | `uncomplicated or syndrome_class == 'uncomplicated_uti'` | UNCOMPLICATED_UTI |
| 7 | DEFAULT (1) | terminal `default` | DEFAULT_ANSWER (quick map) |

## Output → therapy
- ASYMPTOMATIC_BACTERIURIA → do not treat.
- UNCOMPLICATED_UTI → fosfomycin or nitrofurantoin.
- COMPLICATED_DISCHARGEABLE → cefuroxime.
- COMPLICATED_HOSPITALIZED → ceftriaxone.
- COMPLICATED_HOSPITALIZED_NOSOCOMIAL_RISK → ertapenem.
- CATHETER_ASSOCIATED_UTI_DIAGNOSTICS → catheter change + fresh-sample urine culture/chemistry (diagnostic note, no antibiotic from catheter status alone).

## Modelling deviations (confirm acceptable)
1. **Duplicate BY_STATUS rules merged.** Source had pairs of rules (e.g.
   COMPLICATED_HOSPITALIZED vs COMPLICATED_HOSPITALIZED_BY_STATUS) firing the SAME
   output from either the boolean `complicated` slot OR `syndrome_class ==
   complicated_uti`. Each pair is merged into one rung with an `or`. Behaviourally
   identical, same target output.
2. **SLOT_ALIASES (source) → router slot-keyword vocab.** The natural-language → slot
   value mapping lives in the router's `_PATHWAY_SLOT_VOCAB` (same place the drug slot
   keywords live), not the YAML.
3. **LINKS / INFO_BLOCKS / RESTRICTED_OUTPUTS / SAFETY_RULES / forbidden** not stored
   (no schema field); functionally preserved as for CAP (no dosing from UTI is
   structural; dosing forwarded to `get_dose`).

## Linter note
`nitrofurantoin`, `ertapenem`, `cefuroxime` (not in migrated formulary) and the
non-drug items (`antibiotics_not_required`, `urine_culture`, `urine_chemistry`,
`catheter_change_before_sampling`) produce expected warnings only.

## Sign-off checklist (owner)
- [ ] Ladder priority order matches source.
- [ ] Each output's therapy matches source verbatim.
- [ ] Deviation 1 (merged BY_STATUS pairs) clinically equivalent.
- [ ] Deviations 2–3 acceptable.
