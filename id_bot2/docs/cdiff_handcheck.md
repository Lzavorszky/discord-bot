# Clinical hand-check — `cdiff.yaml` (Clostridium difficile)

**Source:** `protocols/cdiff.txt` (C. difficile, ID team, v0.1, draft) → `id_bot2/protocols/cdiff.yaml` (kind: pathway)
**Tool:** `select_pathway` · **Status: SIGN-OFF PENDING (owner L).**

cdiff requires a section choice (source answer_mode: required_slots_then_selected_output).
No choice → DEFAULT_ANSWER asks for diagnosis vs treatment.

## Selection ladder
| # | Source RULE (priority) | Guard | Output |
|---|------------------------|-------|--------|
| 1 | DIAGNOSIS (100) | `cdiff_request_type == 'diagnosis'` | DIAGNOSIS_CHUNK |
| 2 | TREATMENT (100) | `cdiff_request_type == 'treatment'` | TREATMENT_CHUNK |
| 3 | DEFAULT (1) | terminal `default` | DEFAULT_ANSWER (choose a section) |

## Outputs (verbatim — verify against source chunks)
- DIAGNOSIS_CHUNK → >3 loose stools/day (Bristol 5-7), positive toxin; do NOT treat
  toxin/antigen positivity without diarrhea; repeat if toxin-neg/antigen-pos; sample
  transport caveat; contact isolation + soap-and-water until 48h after diarrhea stops.
- TREATMENT_CHUNK → **NG vancomycin 4x125 mg** (verbatim source dose, preserved);
  add metronidazole in severe / unreliable NG; stop conventional antibiotics; tigecycline
  for toxic megacolon/septic (ID consult); fidaxomicin / FMT for resistant (Szt. Laszlo
  consult); probiotics not effective; duration ≥10 days + 48h after diarrhea stops.

## Modelling deviations
1. **Inline dose retained verbatim.** cdiff `allows_dosing: yes`; the source prints
   "NG vancomycin 4x125 mg" inside the treatment chunk. It is preserved verbatim (the
   grounding verifier soft-flags `pathway`, so this source text is not stripped). This
   is the protocol's own text, not a composed dose.
2. LINKS (vancomycin/metronidazole/tigecycline/fidaxomicin) / INFO_BLOCKS /
   RESTRICTED_OUTPUTS / SAFETY_RULES not stored (no schema field); dosing still
   forwarded to `get_dose` by the router.

## Linter note
`fidaxomicin` and the non-drug items (`cdiff_diagnosis`, `fecal_microbiota_transplantation`)
produce expected warnings only (vancomycin/metronidazole/tigecycline ARE migrated).

## Sign-off checklist (owner)
- [ ] Diagnosis chunk matches source verbatim (incl. the "no treat without diarrhea" rule).
- [ ] Treatment chunk matches source verbatim, incl. **NG vancomycin 4x125 mg**.
- [ ] Default correctly asks diagnosis vs treatment.
- [ ] Deviations acceptable.
