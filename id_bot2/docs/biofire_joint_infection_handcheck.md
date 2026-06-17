# Clinical hand-check — biofire_joint_infection.yaml (Phase 2.5)

**Source:** `protocols/joint_infection_pcr.txt` (v0.2) + `joint_infection_pcr.route_claims.json`
**Migrated file:** `id_bot2/protocols/biofire_joint_infection.yaml` (kind: pcr_panel)
**Owner sign-off:** ☐ PENDING (owner L)

This sheet is the human's non-delegable clinical check: confirm every organism →
tier → therapy, every resistance rule, and every verbatim output string matches the
source. The engine (`interpret_pcr`) only ever **selects** among the verbatim strings
below; it never composes a novel recommendation.

## 1. Panel identity & gate
| Field | Source | Migrated | OK |
|---|---|---|---|
| canonical_name | BioFire Joint Infection Panel | same | ☐ |
| required input | at_least_one_detected_pathogen | `requires:` same | ☐ |
| no-pathogen output | DEFAULT_ANSWER (EN + HU) | `default_answer` / `_hu` verbatim | ☐ |
| marker-without-pathogen | MARKER_WITHOUT_PATHOGEN (EN + HU) | `marker_without_pathogen` / `_hu` verbatim | ☐ |
| footer | DEFAULT_FOOTER (skin-soft-tissue / nosocomial intra-abdominal) | `footer` verbatim | ☐ |
| dosing | allows_dosing: no — use LINKS | `dose_via: drug_dose`; panel emits NO dose | ☐ |

## 2. Spectrum hierarchy (polymicrobial escalation) — verbatim TIER_1..TIER_4
| Tier | Source agent / answer | Migrated `spectrum_tiers` | OK |
|---|---|---|---|
| 1 | ceftriaxone — "Tier 1 - ceftriaxone." | same | ☐ |
| 2 | cefepime — "Tier 2 - cefepime." | same | ☐ |
| 3 | meropenem — "Tier 3 - meropenem." | same | ☐ |
| 4 | meropenem + colistin — "Tier 4 - meropenem + colistin. High MDR risk; ID consultation recommended." | same | ☐ |

**NOTE (panel-specific):** in THIS panel CTX-M escalates the gram-negative backbone to
**meropenem** (the pneumonia panel uses ertapenem). Confirm this is intended. ☐

## 3. Organisms — base tier + baseline therapy + verbatim single-organism answer
(All copied from source PCR_ORGANISM_MAPPING + SELECTED_OUTPUTS.)

| Organism | Type | Tier | Therapy | OK |
|---|---|---|---|---|
| Anaerococcus prevotii/vaginalis | anaerobe | 1 | anaerobe guidance | ☐ |
| Clostridium perfringens | anaerobe | 1 | anaerobe guidance | ☐ |
| Cutibacterium avidum/granulosum | anaerobe | 1 | anaerobe guidance | ☐ |
| Finegoldia magna | anaerobe | 1 | anaerobe guidance | ☐ |
| Parvimonas micra | anaerobe | 1 | anaerobe guidance | ☐ |
| Peptoniphilus | anaerobe | 1 | anaerobe guidance | ☐ |
| Peptostreptococcus anaerobius | anaerobe | 1 | anaerobe guidance | ☐ |
| Bacteroides fragilis | anaerobe | 1 | anaerobe guidance | ☐ |
| Enterococcus faecalis | bacteria | 1 | ampicillin + vancomycin (de-escalate if ampS) | ☐ |
| Enterococcus faecium | bacteria | 1 | linezolid | ☐ |
| Staphylococcus aureus | bacteria | 1 | cefazolin (MSSA) | ☐ |
| Staphylococcus lugdunensis | bacteria | 1 | cefazolin ("Cefazolin.") | ☐ |
| Streptococcus agalactiae | bacteria | 1 | ceftriaxone | ☐ |
| Streptococcus pneumoniae | bacteria | 1 | ceftriaxone | ☐ |
| Streptococcus pyogenes | bacteria | 2 | penicillin + clindamycin | ☐ |
| Citrobacter | gram_neg (entero) | 1 | ceftriaxone | ☐ |
| Enterobacter cloacae | gram_neg (entero) | 2 | cefepime | ☐ |
| Escherichia coli | gram_neg (entero) | 1 | ceftriaxone | ☐ |
| Haemophilus influenzae | gram_neg | 1 | ceftriaxone | ☐ |
| Kingella kingae | gram_neg | 1 | ceftriaxone | ☐ |
| Klebsiella aerogenes | gram_neg (entero) | 2 | cefepime | ☐ |
| Klebsiella oxytoca | gram_neg (entero) | 1 | ceftriaxone | ☐ |
| Klebsiella pneumoniae group | gram_neg (entero) | 1 | ceftriaxone | ☐ |
| Morganella morganii | gram_neg (entero) | 1 | ceftriaxone | ☐ |
| Neisseria gonorrhoeae | gram_neg | 1 | ceftriaxone | ☐ |
| Proteus spp. | gram_neg (entero) | 2 | cefepime | ☐ |
| Pseudomonas aeruginosa | gram_neg | 2 | cefepime | ☐ |
| Salmonella spp. | gram_neg (entero) | 1 | ceftriaxone | ☐ |
| Serratia marcescens | gram_neg (entero) | 2 | cefepime | ☐ |

(29 organisms — matches source mapping table count.)

## 4. Resistance markers (PCR_RESISTANCE_RULES, verbatim)
| Marker | rule | Action / therapy | Aliases | OK |
|---|---|---|---|---|
| mecA/C & MREJ | mrsa | Staph aureus → vancomycin (MRSA) | mecA/C, mecA/B, mecA, mecB, mecC, MREJ | ☐ |
| VanA/B | vre | Enterococcus → linezolid; E. faecalis → marker_answer | VanA, VanB, VanA/B | ☐ |
| CTX-M | ctx_m | Enterobacterales → meropenem ("Tier 3 - meropenem.") | CTX-M, CTXM, ESBL | ☐ |
| carbapenemase | carbapenemase | meropenem + colistin (Tier 4) + ID consult | IMP, KPC, NDM, VIM, OXA-48-like, OXA-48, OXA48 | ☐ |

## 5. Special verbatim outputs to confirm word-for-word
- **STAPH MSSA:** "Staphylococcus aureus without mecA/C, mecA/B, or MREJ - MSSA likely - cefazolin."
- **STAPH MRSA (marker_answer):** "Staphylococcus aureus + mecA/C, mecA/B, or MREJ - MRSA likely - vancomycin."
- **E. faecalis:** "Enterococcus faecalis - ampicillin + vancomycin. Vancomycin can be de-escalated if the isolate is ampicillin-susceptible."
- **E. faecalis + VanA/B (marker_answer):** "Enterococcus faecalis + VanA/B - linezolid; baseline ampicillin + vancomycin context requires susceptibility review."
- **E. faecium:** "Enterococcus faecium - linezolid."
- **Strep pyogenes:** "Streptococcus pyogenes - penicillin + clindamycin."
- **Anaerobe guidance:** "Anaerobe detected. If using cephalosporin therapy, add metronidazole. No additional anaerobe agent is needed with a penicillin derivative such as amoxicillin/clavulanate or piperacillin/tazobactam, or with a carbapenem such as imipenem or meropenem."

## 6. Disambiguation (F5)
- Bare **Klebsiella** → ask which species: Klebsiella pneumoniae group / Klebsiella oxytoca / Klebsiella aerogenes (source CHECK_AMBIGUOUS_GENUS: "ask species only when the species changes antimicrobial selection"). ☐

## 7. Modelling deviations (flag for owner)
1. **HU translations:** stored EN `answer` for every organism (operational); HU stored only for E. faecalis and the anaerobe guidance where the source HU is materially distinct. The bilingual phrasing model renders HU at answer time. Confirm acceptable, or request full HU per organism. ☐
2. **`marker_rules` (organism-level):** kept as human-readable strings (e.g. "CTX-M positive -> meropenem") for traceability; the engine applies the structured `markers:` + organism flags, not these strings. ☐
3. **Anaerobe handling in polymicrobial escalation:** anaerobes carry tier 1 but their output is the orthogonal "add metronidazole" guidance, not a numbered spectrum agent. The numeric escalation (tiers 1–4) covers only the cephalosporin/carbapenem backbone, per source "Spectrum hierarchy". Confirm. ☐

## Sign-off
☐ I (owner L) confirm the migrated joint-infection panel matches the source.
