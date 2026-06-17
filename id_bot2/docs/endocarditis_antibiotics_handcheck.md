# Clinical hand-check — `endocarditis_antibiotics.yaml` (infective endocarditis)

**Source:** `protocols/endocarditis_antibiotics.txt` (2023 ESC IE antibiotic section,
local ID simplification, v0.1, draft) → `id_bot2/protocols/endocarditis_antibiotics.yaml`
(kind: pathway) · **Tool:** `select_pathway` · **Status: SIGN-OFF PENDING (owner L).**

This is the largest pathway (21 outputs, 21 select rungs). select_pathway returns one
of the protocol's own verbatim outputs (list order = source priority 300→80→default).
`allows_dosing: yes` — the source prints short STARTING-dose strings inside each output;
they are preserved **verbatim** (the verifier soft-flags `pathway`, so they are not
stripped). These are the protocol's own text, never composed.

## Selection ladder (verify priority order — pathogen-specific beats empiric)

| Priority | Guard | Output |
|----------|-------|--------|
| 300 | `unsupported_topic == 'culture_negative'` | NOT_COVERED_CULTURE_NEGATIVE |
| 300 | `unsupported_topic == 'fungal'` | NOT_COVERED_FUNGAL |
| 300 | `unsupported_topic == 'opat'` | NOT_COVERED_OPAT |
| 250 | `pathogen_group == 'vre' or resistance_profile == 'vre'` | ENTERO_VRE |
| 240 | `resistance_profile == 'mrsa' and valve_context == 'pve'` | MRSA_PVE |
| 235 | `resistance_profile == 'mrsa' and valve_context == 'nve'` | MRSA_NVE |
| 230 | `pathogen_group == 'mrsa' or (pathogen_group == 'staphylococcus_aureus' and resistance_profile == 'mrsa')` | MRSA_BOTH_VALVES |
| 220 | `resistance_profile == 'mssa' and valve_context == 'pve'` | MSSA_PVE |
| 215 | `resistance_profile == 'mssa' and valve_context == 'nve'` | MSSA_NVE |
| 210 | `pathogen_group == 'mssa' or (pathogen_group == 'staphylococcus_aureus' and resistance_profile == 'mssa')` | MSSA_BOTH_VALVES |
| 200 | `pathogen_group == 'staphylococcus_aureus'` | STAPH_AUREUS_MSSA_MRSA |
| 190 | `pathogen_group == 'enterococcus' and resistance_profile == 'beta_lactam_sensitive'` | ENTERO_BETA_LACTAM_SENSITIVE |
| 185 | `pathogen_group == 'enterococcus' and resistance_profile == 'beta_lactam_resistant_not_vre'` | ENTERO_BETA_LACTAM_RESISTANT_NOT_VRE |
| 180 | `pathogen_group == 'enterococcus'` | ENTEROCOCCUS_ALL_THREE |
| 170 | `pathogen_group == 'streptococcus'` | STREPTOCOCCUS_CEFTRIAXONE |
| 160 | `pathogen_group == 'unsupported'` | PATHOGEN_NOT_COVERED |
| 120 | `pve_timing == 'early' or valve_context == 'early_pve'` | EMPIRIC_EARLY_PVE |
| 110 | `valve_context == 'pve'` | EMPIRIC_PVE_TIMING_OPTIONS |
| 100 | `penicillin_allergy` | EMPIRIC_NVE_LATE_PVE_PENICILLIN_ALLERGY |
| 80 | `treatment_mode == 'empiric' or valve_context == 'nve' or pve_timing == 'late'` | EMPIRIC_NVE_LATE_PVE |
| 1 | terminal `default` | DEFAULT_ANSWER (empiric starting options) |

## Starting doses (verify each verbatim against source — the clinically critical bit)

- EMPIRIC_NVE_LATE_PVE → ampicillin 12 g/day IV + ceftriaxone 4 g/day IV/IM in 2 doses + gentamicin 3 mg/kg.
- EMPIRIC_NVE_LATE_PVE_PENICILLIN_ALLERGY / EMPIRIC_EARLY_PVE → cefazolin 6 g/day IV + vancomycin 2 g load + 2 g/24h (see local guideline) + gentamicin 3 mg/kg.
- MSSA_NVE → cefazolin 6 g/day IV. · MSSA_PVE → + gentamicin 3 mg/kg; rifampicin 900-1200 mg/day after bacteremia clears.
- MRSA_NVE → vancomycin 2 g load + 2 g/24h. · MRSA_PVE → + gentamicin 3 mg/kg; rifampicin 900-1200 mg/day after bacteremia clears.
- ENTERO_BETA_LACTAM_SENSITIVE → ampicillin 12 g/day IV + ceftriaxone 4 g/day IV/IM in 2 doses.
- ENTERO_BETA_LACTAM_RESISTANT_NOT_VRE → vancomycin 2 g load + 2 g/24h + gentamicin 3 mg/kg.
- ENTERO_VRE → daptomycin 10-12 mg/kg/day + ampicillin 12 g/day IV; ID consult please.
- STREPTOCOCCUS_CEFTRIAXONE → ceftriaxone 2 g/day IV.
- The "…_BOTH_VALVES" / "…_ALL_THREE" / STAPH_AUREUS_MSSA_MRSA / EMPIRIC_PVE_TIMING_OPTIONS outputs show the combined option sets verbatim.
- NOT_COVERED_* / PATHOGEN_NOT_COVERED → "not covered in this bot guideline; refer to specialist/full guideline."

## Modelling deviations
1. **OUTPUT_TEMPLATE applied at migration.** The source stores each output as
   `display_name`/`regimen`/`doses`/`note` + a FINAL_SELECTED template. The YAML stores
   the **template already rendered** into `text_en` (the source's own template + field
   values — no wording added). HU display_name == EN display_name in source, so a
   single rendered text is used.
2. **Compound guards parenthesised.** `... mrsa OR ... AND ...` rules are written with
   explicit parentheses matching Python precedence (`a or (b and c)`) = source intent.
3. **pve_timing vs valve_context disambiguation (router only).** When the message pins
   "early/late PVE", the router sets `pve_timing` and suppresses `valve_context=pve` so
   the timing rungs win over EMPIRIC_PVE_TIMING_OPTIONS (matches source priority).
4. LINKS / INFO_BLOCKS / RESTRICTED_OUTPUTS / SAFETY_RULES / footer-as-field not stored
   beyond `footer`; the penicillin-allergy-severity footer IS kept in `footer`.

## Linter note
`gentamicin`, `rifampicin`, `daptomycin` (not in migrated formulary) produce expected
warnings only (ampicillin, ceftriaxone, cefazolin, vancomycin ARE migrated).

## Sign-off checklist (owner)
- [ ] Ladder priority order matches source (pathogen-specific > empiric; not-covered first).
- [ ] **Every starting-dose string matches the source verbatim** (the critical check).
- [ ] Rifampicin "only after bacteremia clears" preserved in MSSA/MRSA PVE outputs.
- [ ] Deviations 1–4 acceptable.
