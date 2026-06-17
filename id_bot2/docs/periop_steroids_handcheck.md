# periop_steroids — clinical hand-check sheet

**Source:** `protocols/periop_steroids.txt` (v0.2, status: draft, owner: ID team)
**Migrated to:** `id_bot2/protocols/periop_steroids.yaml` (`kind: prose`)
**Tool:** `answer_from_section` (SELECTS one verbatim section; never composes, never computes)
**Migrated:** 2026-06-17 · **Owner sign-off: PENDING**

---

## What this protocol is

Info-only, **single-block** guide. Any perioperative chronic-steroid / stress-dose question returns the
complete source table (the `steroid_guide_full` INFO_BLOCK) verbatim. Modelled as ONE prose section
`steroid_guide` with `default_section: steroid_guide`.

## Side-by-side check (please verify against source)

| Source `steroid_guide_full` INFO_BLOCK | In YAML `sections.steroid_guide.text_en` |
|---|---|
| methylprednisone < 8 mg → Continue usual dose; no additional steroid | ✅ verbatim |
| methylprednisone ≥ 8 mg for ≥ 3 months → Usual dose + hydrocortisone supplementation | ✅ verbatim |
| small surgery (inguinal hernia) → 1×25 mg hydrocortisone at start | ✅ verbatim |
| medium surgery (lap chole) → 1×25 mg + 4×25 mg for 24 h | ✅ verbatim |
| major surgery (PPPD) → 1×25 mg + 4×25 mg for 48 h | ✅ verbatim |

## Flagged engine decisions (owner to confirm)

1. **Footer = source DEFAULT_FOOTER verbatim:** "Curious about steroid equivalency? Ask: dexamethasone
   6mg equivalent?" — points users to the **separate** `steroid_equivalence` calculator. This protocol
   does NOT do equivalence/conversion (source RESTRICTED_OUTPUTS + SAFETY_RULES); that is a different tool.
2. **No surgery-type question first** (source SAFETY_RULE) — the whole table is returned and the clinician
   picks the row. Enforced by always returning the single block.
3. **NON_PERIOP_STEROID refusal** ("I only have perioperative steroid guidance…") is handled at the
   router level (a non-perioperative steroid question never matches this protocol's aliases → unsupported),
   not stored as a section. Confirm this is acceptable.

## Routing / sign-off checklist

- Aliases require a steroid + perioperative signal; the prose stage resolves it after the drug/calc stages,
  and a steroid-**equivalence** phrasing routes to the calculator instead (verified: calc aliases require
  "equivalence/conversion/equivalent", not "perioperative steroid").
- Confirm: (a) both table sections reproduced exactly; (b) it is correct that equivalence is deferred to
  the separate calculator with this footer pointer.
