# Clinical hand-check — biofire_pneumonia.yaml (Phase 2.5)

**Source:** `protocols/pneumonia_pcr.txt` (v0.1) + `pneumonia_pcr.route_claims.json`
**Migrated file:** `id_bot2/protocols/biofire_pneumonia.yaml` (kind: pcr_panel)
**Owner sign-off:** ☐ PENDING (owner L)

The engine (`interpret_pcr`) only ever **selects** among the verbatim strings below.

## 1. Panel identity & gate
| Field | Source | Migrated | OK |
|---|---|---|---|
| canonical_name | BioFire Pneumonia Panel | same | ☐ |
| required input | at_least_one_detected_pathogen | `requires:` same | ☐ |
| no-pathogen output | DEFAULT_ANSWER (EN + HU) | `default_answer` / `_hu` verbatim | ☐ |
| marker-without-pathogen | MARKER_WITHOUT_PATHOGEN (EN + HU) | `marker_without_pathogen` / `_hu` verbatim | ☐ |
| footer | DEFAULT_FOOTER (HU "Nézd meg…") | `footer` verbatim | ☐ |
| dosing | allows_dosing: no — use LINKS | `dose_via: drug_dose`; panel emits NO dose | ☐ |

## 2. Spectrum hierarchy — verbatim TIER_1..TIER_4
| Tier | Source agent / answer | OK |
|---|---|---|
| 1 | ceftriaxone — "Tier 1 - ceftriaxone." | ☐ |
| 2 | cefepime — "Tier 2 - cefepime." | ☐ |
| 3 | **ertapenem** — "Tier 3 - ertapenem." | ☐ |
| 4 | meropenem + colistin — "Tier 4 - meropenem + colistin. High MDR risk; ID consultation recommended." | ☐ |

**NOTE (panel-specific):** in THIS panel CTX-M escalates to **ertapenem** (Tier 3), NOT
meropenem (the joint-infection panel uses meropenem). Verbatim per source RESISTANCE_RULES /
Spectrum hierarchy. Confirm. ☐

## 3. Organisms — base tier + therapy
| Organism | Type | Tier | Therapy | OK |
|---|---|---|---|---|
| Acinetobacter calcoaceticus-baumannii complex | gram_neg | 4 | meropenem + colistin | ☐ |
| Enterobacter cloacae | gram_neg (entero) | 2 | cefepime | ☐ |
| Escherichia coli | gram_neg (entero) | 1 | ceftriaxone | ☐ |
| Haemophilus influenzae | gram_neg | 1 | ceftriaxone | ☐ |
| Klebsiella aerogenes | gram_neg (entero) | 2 | cefepime | ☐ |
| Klebsiella oxytoca | gram_neg (entero) | 1 | ceftriaxone | ☐ |
| Klebsiella pneumoniae group | gram_neg (entero) | 1 | ceftriaxone | ☐ |
| Moraxella catarrhalis | gram_neg | 1 | ceftriaxone | ☐ |
| Proteus spp. | gram_neg (entero) | 2 | cefepime | ☐ |
| Pseudomonas aeruginosa | gram_neg | 2 | cefepime | ☐ |
| Serratia marcescens | gram_neg (entero) | 2 | cefepime | ☐ |
| Staphylococcus aureus | bacteria | 1 | cefazolin (MSSA) | ☐ |
| Streptococcus agalactiae | bacteria | 1 | ceftriaxone | ☐ |
| Streptococcus pneumoniae | bacteria | 1 | ceftriaxone | ☐ |
| Streptococcus pyogenes | bacteria | 2 | penicillin + clindamycin (toxin suppression) | ☐ |
| Legionella pneumophila | atypical | — | clarithromycin | ☐ |
| Mycoplasma pneumoniae | atypical | — | clarithromycin | ☐ |
| Chlamydia pneumoniae | atypical | — | clarithromycin | ☐ |
| Influenza A/B | virus | — | oseltamivir | ☐ |
| Other respiratory virus | virus | — | supportive therapy | ☐ |

(20 organisms — matches source mapping table count.)

## 4. Resistance markers (PCR_RESISTANCE_RULES, verbatim)
| Marker | rule | Action / therapy | Aliases | OK |
|---|---|---|---|---|
| mecA/C & MREJ | mrsa | Staph aureus → vancomycin (MRSA) | mecA/C, mecA, mecC, MREJ | ☐ |
| VanA/B | vre | → linezolid | VanA, VanB, VanA/B | ☐ |
| CTX-M | ctx_m | Enterobacterales → **ertapenem** ("Tier 3 - ertapenem.") | CTX-M, CTXM, ESBL | ☐ |
| carbapenemase | carbapenemase | meropenem + colistin (Tier 4) + ID consult | IMP, KPC, NDM, VIM, OXA-48-like, OXA-48, OXA48 | ☐ |

## 5. Special verbatim outputs to confirm word-for-word
- **STAPH MSSA:** "Staphylococcus aureus without mecA/C or MREJ - MSSA likely - cefazolin."
- **STAPH MRSA (marker_answer):** "Staphylococcus aureus + mecA/C or MREJ - MRSA likely - vancomycin."
- **Strep pyogenes:** "Streptococcus pyogenes - penicillin + clindamycin for toxin suppression."
- **Atypical (Legionella/Mycoplasma/Chlamydia):** "Atypical pathogen - clarithromycin. If bacterial coinfection is present, add it to bacterial coverage."
- **Influenza:** "Influenza A/B - oseltamivir. Antibiotics only if bacterial coinfection is suspected."
- **Viral only:** "Viral pathogen only - supportive therapy; antibacterial therapy is not routinely indicated unless bacterial coinfection is suspected."

## 6. Modelling deviations (flag for owner)
1. **CTX-M for non-Enterobacterales:** source explicitly says do NOT apply CTX-M to
   Streptococcus pneumoniae / other non-Enterobacterales. The engine only escalates
   organisms with `enterobacterales: true`, so S. pneumoniae + CTX-M does NOT upgrade.
   Confirm this matches RULE: CTX_M_WITH_NON_ENTEROBACTERALES_ONLY. ☐
2. **Disambiguation added:** the PN source has no explicit CHECK_AMBIGUOUS_GENUS step, but
   the panel carries 3 Klebsiella species, so bare "Klebsiella" → ask which species
   (`disambiguate_genus`). This is a SAFETY ADDITION (asking is safer than picking).
   Confirm acceptable, or request removal. ☐
3. **Atypicals/viruses have no numeric tier:** they are orthogonal add-ons (clarithromycin /
   oseltamivir / supportive), not part of the 1–4 spectrum backbone, per source. ☐
4. **HU translations:** EN `answer` stored operationally; bilingual phrasing model renders HU
   at answer time. Confirm, or request per-organism HU. ☐

## Sign-off
☐ I (owner L) confirm the migrated pneumonia panel matches the source.
