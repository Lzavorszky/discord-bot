# Clinical hand-check — `sbp.yaml` (spontaneous bacterial peritonitis)

**Source:** `protocols/sbp.txt` (SBP, ID team, v0.1, draft) → `id_bot2/protocols/sbp.yaml` (kind: pathway)
**Tool:** `select_pathway` · **Status: SIGN-OFF PENDING (owner L).**

SBP has a single rule in the source (RULE DEFAULT → WHOLE_SBP): any SBP request
returns the whole protocol chunk verbatim.

## Selection ladder
| # | Source RULE | Guard | Output |
|---|-------------|-------|--------|
| 1 | DEFAULT (1) | terminal `default` | WHOLE_SBP |

## Output (verbatim chunk — verify against source DEFAULT_ANSWER / WHOLE_SBP)
WHOLE_SBP →
- Diagnosis = paracentesis.
- Send: microbiology (10 mL ascites in blood culture bottle), chemistry (LDH,
  glucose, bilirubin, amylase, albumin, WBC), cell count, cytology, ascites into
  blood gas machine if macroscopically clear.
- Diagnosis: neutrophils > 500/uL, low pH, low glucose + high lactate, high albumin gradient.
- Treatment (abdominal infection table): ceftriaxone; dosing via the ceftriaxone protocol.

## Modelling deviations
1. Source DEFAULT_ANSWER and the WHOLE_SBP chunk are the same content; collapsed to a
   single output WHOLE_SBP (default → WHOLE_SBP). No content lost.
2. LINKS (ceftriaxone dosing) / RESTRICTED_OUTPUTS / SAFETY_RULES not stored (no
   schema field); "do not dose ceftriaxone from SBP" is structural (select_pathway
   emits no dose; router forwards dosing to `get_dose`).

## Linter note
No item warnings expected beyond `ceftriaxone` (which IS migrated → resolves).

## Sign-off checklist (owner)
- [ ] WHOLE_SBP chunk matches the source sample list + diagnostic criteria + ceftriaxone verbatim.
- [ ] Deviations acceptable.
