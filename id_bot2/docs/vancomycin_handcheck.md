# Clinical hand-check — `vancomycin.yaml` vs source `vancomycin.txt`

**Phase 3.4 sign-off sheet (2026-06-16).** The non-delegable human check: confirm every dose,
band, and TDM action in `id_bot2/protocols/vancomycin.yaml` matches the source
`protocols/antibiotics/vancomycin.txt`. Validator green (`python id_bot2/validate_protocols.py`);
`get_dose` exercised across all 24 tiers + out-of-range (see §6). Nothing ships on the bot's say-so.

- **Source:** `protocols/antibiotics/vancomycin.txt` + `vancomycin.route_claims.json`
- **Migrated:** `id_bot2/protocols/vancomycin.yaml` (kind: drug_dose, 24 tiers, 25-rung ladder)
- **No owner edits.** Every value is transcribed verbatim. Modelling deviations are in §7.

---

## 1. Loading dose (not affected by renal function) — verbatim

| Body weight | Source loading dose | In YAML |
|---|---|---|
| TBW <65 kg | 1.5 g | `loading 1.5 g` (in every <65 kg tier; `1.5 g` option in renal-only tiers) |
| TBW >=65 kg | 2 g | `loading 2 g` (in every >=65 kg tier; `2 g` option in renal-only tiers) |

## 2. Continuous-infusion starting dose by renal status — verbatim

| Renal | Source maintenance | YAML tier(s) |
|---|---|---|
| GFR >=50 | 2 g/24h | GFR_GE_50_* |
| GFR 20-49 | 1.5 g/24h | GFR_20_TO_49_* |
| GFR 10-19 | 1 g/24h | GFR_10_TO_19_* |
| GFR <10 (no IHD/CRRT) | not specified in protocol → individualized review | GFR_LT_10_UNSPECIFIED |
| CRRT | 2 g/24h | CRRT_* |
| IHD | 500 mg bolus after HD | IHD_* |

Each tier exists in two weight variants (loading 1.5 g vs 2 g) plus a renal-only variant (shows both
loading options) for when weight is not supplied. 18 first-dose tiers total.

## 3. TDM adjustment table (when a vancomycin level is supplied) — verbatim

| Level | Source action | YAML tier `dose` |
|---|---|---|
| <10 ug/L | Check sampling and administration. | TDM_LEVEL_LT_10 |
| exactly 10 ug/L | Not specified; verify value, use clinical/TDM review. | TDM_LEVEL_10_UNSPECIFIED |
| 11-20 ug/L | Increase by 500 mg/24h. | TDM_LEVEL_11_TO_20 |
| 21-25 ug/L | No change. | TDM_LEVEL_21_TO_25 |
| 26-29 ug/L | Decrease by 500 mg/24h. | TDM_LEVEL_26_TO_29 |
| >=30 ug/L | Decrease by 1000 mg/24h. | TDM_LEVEL_GE_30 |

## 4. Selection priority — source SELECTION_RULES → YAML `select` (list order = priority)

| Source priority | Condition | Selects | Match |
|---|---|---|:--:|
| 300 | vancomycin_level band | TDM_LEVEL_* (level wins over renal) | ✓ |
| 220 | ihd + weight band | IHD_TBW_LT_65 / GE_65 | ✓ |
| 210 | crrt + weight band | CRRT_TBW_LT_65 / GE_65 | ✓ |
| 200/190/180 | gfr band + weight band | GFR_*_TBW_* | ✓ |
| 120/110/100/90/80 | ihd / crrt / gfr only (no weight) | *_ONLY | ✓ |
| 60 | weight only (no renal) | TBW_*_ONLY (loading + ask renal) | ✓ |
| 50 | gfr <10 (no IHD/CRRT) | GFR_LT_10_UNSPECIFIED | ✓ |
| 1 | no input | DEFAULT_ANSWER (full table) | ✓ |

Safety ordering preserved (source SAFETY_RULES): level supplied → TDM only, not a first-dose tier;
IHD > CRRT > numeric GFR; weight-qualified tiers beat the weight-agnostic `*_ONLY` fallbacks.

## 5. Slots / routing / guardrails / aliases

- **Slots:** body_weight_kg (1-300 kg), gfr (0-250), vancomycin_level (0-150 mg/L), mic (0-32), crrt, ihd — all `ask_confirmation` out of range (now enforced for every numeric slot, not just gfr).
- **Routing:** answers `[dose]`; refuses `[coverage_question, targeted_treatment]` (route_claims verbatim).
- **never:** dosing outside listed tiers; MIC >1.0 recommendation; alternative agent; TDM outside listed ranges; toxicity mgmt not in protocol.
- **Aliases:** vancomycin, vanco, vancomycin dose, vancomycin dosing, vancomycin TDM (verbatim; no collisions).

## 6. Engine verification (offline, deterministic)

`get_dose` returns the correct tier for: all 6 TDM bands; level-beats-renal; IHD/CRRT/GFR × weight bands;
weight-only and renal-only fallbacks; GFR<10; IHD-beats-GFR priority; and **needs_confirmation** for
out-of-range vancomycin_level / body_weight / gfr / mic. No-input → full table.

## 7. Modelling deviations (no clinical value changed — please confirm acceptable)

1. **Composite `dose` string.** The schema tier has one `dose` field; the source splits loading_dose and
   maintenance_dose. Migrated as `"loading <X>, then <maintenance>"`. No value altered.
2. **`when` carries the weight context** for weight-qualified tiers (e.g. `TBW <65 kg + GFR >=50`,
   from the source display_name) so the tier is self-describing.
3. **Footer transcribed verbatim, including a source typo** ("Ae you sure", "Does this pt has MRSA").
   Flagged for an optional copy-edit before go-live — kept verbatim here per the no-edit migration rule.
4. **INFO_BLOCKS / OUTPUT_TEMPLATES not migrated** (target/monitoring/MIC info, render templates). The key
   MIC>1.0 and target-level guidance is preserved in `notes` + `footer`; the rest is rendering/meta the
   phrasing layer will own.
5. **MIC is declared as a slot but does not drive tier selection** — matching the source, where MIC>1.0 is
   a SAFETY_RULE boundary (in `never`/`notes`), not a SELECTION_RULE.

---

## Sign-off

- [ ] §1 Loading doses (1.5 g / 2 g by weight) match source
- [ ] §2 Continuous-infusion maintenance by renal status matches source
- [ ] §3 TDM adjustment actions match source exactly
- [ ] §4 Selection priority (level > IHD > CRRT > GFR; weight-qualified > only) matches source
- [ ] §5 Slots / routing / guardrails / aliases match
- [ ] §7 Modelling deviations acceptable (esp. confirm the verbatim footer typo or request a copy-edit)

Signed: ____________________   Date: __________
