# Clinical hand-check — `intraabdominal_infections.yaml` (intra-abdominal infections)

**Source:** `protocols/intraabdominal_infections.txt` (HASURI INFEKCIOK, ID team, v0.1,
draft) → `id_bot2/protocols/intraabdominal_infections.yaml` (kind: pathway)
**Tool:** `select_pathway` · **Status: SIGN-OFF PENDING (owner L).**

select_pathway returns one of the protocol's own verbatim outputs (list order = source
priority). Does not dose drugs.

## Selection ladder
| Priority | Guard (`iai_context == …`) | Output |
|----------|----------------------------|--------|
| 100 | `'cdiff'` | CDIFF (→ use the C. difficile protocol) |
| 95 | `'sbp'` | SBP (→ ceftriaxone; use SBP protocol for diagnostics) |
| 90 | `'splenectomy_prophylaxis'` | SPLENECTOMY_PROPHYLAXIS (phenoxymethylpenicillin 2x1 MU) |
| 85 | `'varix_bleeding_prophylaxis'` | VARIX_BLEEDING_PROPHYLAXIS (ceftriaxone) |
| 80 | `'pancreatitis'` | PANCREATITIS (no antibiotic at presentation) |
| 75 | `'complex_nosocomial'` | COMPLEX_NOSOCOMIAL (meropenem; BioFire JI PCR escalation only) |
| 70 | `'hospitalized_source_control'` | HOSPITALIZED_SOURCE_CONTROL (ceftriaxone + metronidazole) |
| 60 | `'dischargeable'` | DISCHARGEABLE (amoxicillin/clavulanate; alt cefixime + metronidazole) |
| 1 | terminal `default` | DEFAULT_ANSWER (quick map) |

## Output → therapy (verify verbatim)
As above; full `text_hu`/`text_en` carried verbatim from source recommendation lines.
Note COMPLEX_NOSOCOMIAL preserves "escalation if needed, NOT de-escalation" caveat;
PANCREATITIS preserves "only if septic complication"; SPLENECTOMY preserves
"phenoxymethylpenicillin 2x1 MU".

## Modelling deviations
1. SLOT_ALIASES (source) → router `_PATHWAY_SLOT_VOCAB` (iai_context values).
2. Cross-protocol references (CDIFF → cdiff protocol, SBP → sbp protocol) are kept as
   verbatim text instructions in the output + as `items` (`cdiff_protocol`,
   `sbp_protocol`); the router does not auto-chain protocols (a follow-up message
   selects the target protocol). Matches source LINK semantics (hand-off, not merge).
3. LINKS / INFO_BLOCKS / RESTRICTED_OUTPUTS / SAFETY_RULES not stored (no schema field);
   "no dosing from this pathway" is structural.

## Linter note
`amoxicillin/clavulanate`, `cefixime`, `phenoxymethylpenicillin` (not migrated) and the
non-drug items (`BioFire JI PCR`, `antibiotics_not_required_at_presentation`,
`sbp_protocol`, `cdiff_protocol`) produce expected warnings only (ceftriaxone,
metronidazole, meropenem, vancomycin ARE migrated).

## Sign-off checklist (owner)
- [ ] Ladder priority order matches source.
- [ ] Each context's therapy + caveat matches source verbatim.
- [ ] Deviations 1–3 acceptable.
