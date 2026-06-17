# tmpsmx — clinical hand-check (Plan D, the final migration phase)

**Source:** `protocols/antibiotics/tmpsmx.txt` (v0.1) + `tmpsmx.route_claims.json`
**Migrated to:** `id_bot2/protocols/tmpsmx.yaml` (**kind: `table_lookup`** — a new engine mode)
**Tool:** `id_bot2/tools/get_table_dose.py`  ·  **Tests:** `id_bot2/tests/test_get_table_dose.py` (51)
**Status:** owner sign-off **PENDING** (owner L). Nothing clinical ships on the migrator's say-so.

This is the non-delegable clinical check: confirm every migrated dose / table / rule
matches the source, and confirm the three flagged **engine decisions** below.

---

## 0. Why this protocol is different

tmpsmx is not a single renal ladder (like the antibiotics) — it is a **2-D table
lookup**: `dose = f(indication_tier, renal_category, body_weight_band)`. The tool:

1. **gates** on required inputs (no indication → ask),
2. **classifies** the indication into a tier (HIGH / MODERATE / STANDARD / PROPHYLAXIS),
3. **classifies** renal function into a category (GFR>30/CRRT, GFR15–30, GFR<15, IHD, or UNKNOWN),
4. **selects** the `{tier}_{renal_category}` table and the **closest weight row**, and
5. returns it **verbatim** (slotting verbatim cells into the source FINAL_SELECTED template).

It carries the original **F3/F4** bug fixes (old bot returned PROPHYLAXIS where a
treatment dose was required). See §4.

---

## 1. Indication classification (source `### INDICATION_RULES`)

Keyword *contains* match on the indication text; first matching rule (in list order) wins.

| Tier | Source keywords (verbatim) | Source target |
|------|----------------------------|---------------|
| HIGH_DOSE | pcp, pjp, pneumocystis, pneumocystis jirovecii, steno, stenotrophomonas, stenotrophomonas maltophilia, bloodstream infection, bsi, bacteraemia, bacteremia, nocardia | 15–20 mg/kg/day TMP |
| MODERATE_DOSE | severe, cns, meningitis, brain abscess, bone, joint, osteomyelitis, septic arthritis, refractory, icu, critically ill, deep-seated, severe ssti, severe skin soft tissue | 8–10 mg/kg/day TMP |
| STANDARD_DOSE | standard, susceptible, non-septic, nonseptic, oral step-down, stepdown, step down, uncomplicated, susceptible infection | fixed practical dose |
| PROPHYLAXIS | prophylaxis, prophylactic, pcp prophylaxis, pjp prophylaxis, immunosuppressed, immunosuppression, hematology, haematology, transplant | prophylactic fixed dose |

> ⚠ **ENGINE DECISION 1 — DEVIATION FROM SOURCE RULE ORDER (please confirm).**
> The source listed **HIGH_DOSE first**. Read literally ("first match wins"), the string
> *"PCP prophylaxis"* contains `pcp` → would classify **HIGH_DOSE** (15–20 mg/kg/day) — a
> dangerous over-dose of what is a 1-tablet prophylaxis order.
> **Migration splits PROPHYLAXIS into two rungs:** an *explicit-intent* rung
> (`prophylaxis`, `prophylactic`, `pcp prophylaxis`, `pjp prophylaxis`) is checked **first**
> (so "PCP prophylaxis" → PROPHYLAXIS); the remaining, weaker prophylaxis signals
> (`immunosuppressed`, `hematology`, `transplant`, …) stay **last** (so an immunosuppressed
> patient *with a treatment indication*, e.g. "PCP pneumonia", still classifies HIGH_DOSE).
> A pure treatment indication never contains `prophylaxis`/`prophylactic`, so the **F3/F4
> fix is preserved**. → Confirm this precedence is clinically correct.

---

## 2. Renal classification (source `### RENAL_RULES`)

Ordered ladder; first firing guard wins (same restricted-AST evaluator as `get_dose`).

| # | Source rule | Guard (YAML) | Category | Source adjustment |
|---|-------------|--------------|----------|-------------------|
| 1 | CRRT | `crrt` | GFR_GT_30_OR_CRRT | 100% (full dose) |
| 2 | IHD | `ihd` | IHD | individualized, dose after dialysis |
| 3 | GFR_GT_30 | `gfr > 30` | GFR_GT_30_OR_CRRT | 100% |
| 4 | GFR_15_TO_30 | `gfr >= 15 and gfr <= 30` | GFR_15_TO_30 | ~50% |
| 5 | GFR_LT_15 | `gfr < 15` | GFR_LT_15_WITHOUT_CRRT | avoid if alternative |
| — | (none provided) | `default` | UNKNOWN | — |

Boundaries tested: GFR 15 and 30 → reduced (50%); GFR 31 → full; GFR 10 → avoid warning.
CRRT is checked before IHD (matches source rule order).

---

## 3. Dose tables (source `## SELECTED_OUTPUTS`) — verbatim row-by-row

Every cell below is copied **character-for-character** from the source. Please diff against the `.txt`.

### HIGH_DOSE, GFR>30/CRRT (target 15–20 mg/kg/day, 100%)
| Weight | Practical dose | Total daily TMP/SMX |
|--------|----------------|---------------------|
| 40 | 4 x 2 amp | 640/3200 mg daily |
| 50 | 3 x 3 amp | 720/3600 mg daily |
| 60 | 3 x 4 amp | 960/4800 mg daily |
| 70 | 3 x 4 amp | 960/4800 mg daily |
| 80 | 3 x 5 amp | 1200/6000 mg daily |
| 90 | 4 x 4 amp | 1280/6400 mg daily |
| 100 | 3 x 6 amp | 1440/7200 mg daily |

### HIGH_DOSE, GFR 15–30 (~50%)
40→2x2/320·1600 · 50→2x3/480·2400 · 60→2x3/480·2400 · 70→2x4/640·3200 · 80→3x3/720·3600 · 90→3x3/720·3600 · 100→3x4/960·4800

### MODERATE_DOSE, GFR>30/CRRT (target 8–10 mg/kg/day)
40→2x2/320·1600 · 50→2x3/480·2400 · 60→2x3/480·2400 · 70→2x4/640·3200 · 80→3x3/720·3600 · 90→3x3/720·3600 · 100→3x4/960·4800

### MODERATE_DOSE, GFR 15–30 (~50%)
40→2x1/160·800 · 50→3x1/240·1200 · 60→3x1/240·1200 · 70→2x2/320·1600 · 80→2x2/320·1600 · 90→2x3/480·2400 · 100→2x3/480·2400

### STANDARD_DOSE (fixed)
- GFR>30/CRRT: **2 x 2 amp; 320/1600 mg daily**
- GFR 15–30 (~50%): **2 x 1 amp; 160/800 mg daily**

### PROPHYLAXIS (fixed, no weight)
- GENERAL / GFR>30/CRRT: **1 tablet daily** OR **1 tablet three times weekly**
- GFR 15–30: **1 tablet three times weekly**

### Renal warnings (verbatim)
- **GFR<15 without CRRT:** avoid TMP/SMX if alternative exists; if unavoidable, individualized ID/pharmacy decision.
- **IHD:** individualized dosing; dose after dialysis; ID/pharmacy decision recommended.

---

## 4. Required-input gating & the F3/F4 fixes

| Situation | Behaviour | Why |
|-----------|-----------|-----|
| No indication | verbatim `DEFAULT_ANSWER` (indication groups), `clarifies` | required-input gate (source STEP CHECK_REQUIRED_INPUTS) |
| Indication unclassifiable | verbatim `DEFAULT_ANSWER` | never guess a tier |
| Treatment tier, **renal unknown** | verbatim `MISSING_INPUTS` ask | can't pick GFR>30 vs 15–30 table |
| Treatment dosing_table, **weight missing** | `MISSING_INPUTS` ask, **no full-table dump** | source RESTRICTED_OUTPUTS: "never full tables unless requested" |
| **PCP treatment** | HIGH_DOSE treatment dose | **F3/F4**: was returning prophylaxis |
| **PCP prophylaxis** | PROPHYLAXIS 1-tablet | **F3/F4 inverse**: must NOT escalate to mg/kg dose |
| Prophylaxis | allowed without weight/renal | source RESTRICTED_OUTPUTS allowance |

---

## 5. Weight banding (source `## weight` INFO_BLOCK)

> ⚠ **ENGINE DECISION 2 — tie-break (please confirm).** Rows exist at 40–100 kg.
> Within range the **closest** row by |row−weight| is chosen; an **exact tie**
> (e.g. 55 kg between 50 and 60) **rounds UP** to the higher-weight row (higher dose),
> because the source says *"Avoid underdosing severe infection."* Tested: 55 kg → 60 kg row.

> ⚠ **ENGINE DECISION 3 — out-of-range weight (please confirm).**
> - weight **1–39 kg**: clamp to the 40 kg row + verbatim note "below the table range; individualized dosing decision".
> - weight **101–300 kg**: clamp to the 100 kg row + verbatim note ">100 kg requires an individualized dosing decision and ID/pharmacy consultation" (source `weight` block).
> - weight **<1 or >300 kg** (outside clinical range): **needs confirmation**, no band selected (source SLOT `out_of_clinical_policy: ask_confirmation`).

---

## 6. Things NOT migrated as engine logic (stored verbatim, no behaviour)

- `## INFO_BLOCKS` (toxicity, monitoring, renal, formulation, weight) — stored verbatim in
  `info_blocks:` for future info-intent answering. **Info routing is not wired this session**
  (the dose engine is the priority); the tool's job here is dosing.
- `## SAFETY_RULES` / `## RESTRICTED_OUTPUTS` — encoded as `never:` list + enforced by the
  gating logic above; the cross-cutting safety prompt also carries them in the router.
- `## DEFAULT_FOOTER` — verbatim in `footer:`.

---

## 7. Sign-off checklist (owner L)

- [ ] §1 indication keywords + tiers match source.
- [ ] **ENGINE DECISION 1**: explicit-prophylaxis-pre-empts ordering is clinically correct.
- [ ] §2 renal thresholds + adjustments match source (incl. CRRT-before-IHD).
- [ ] §3 every dose-table cell matches the source `.txt` (diff).
- [ ] §4 gating (ask vs dose) and the F3/F4 treatment-vs-prophylaxis split are correct.
- [ ] **ENGINE DECISION 2**: weight tie-break rounds UP (avoid underdosing).
- [ ] **ENGINE DECISION 3**: out-of-range weight clamp + notes are acceptable.
- [ ] Confirm it is acceptable that info-intent answering is deferred (dosing only this phase).
