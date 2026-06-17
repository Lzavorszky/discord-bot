# periop_gyogyszerek — clinical hand-check sheet

**Source:** `protocols/periop_gyogyszerek.txt` (v0.2, status: draft, owner: ID team)
**Migrated to:** `id_bot2/protocols/periop_gyogyszerek.yaml` (`kind: prose`, **134 sections**)
**Tool:** `answer_from_section` (SELECTS one verbatim section; never composes, never computes)
**Migrated:** 2026-06-17 · **Owner sign-off: PENDING**

---

## What this protocol is

Info-only, **section-addressable** perioperative medication reference. The addressable unit is the
individual **medication / drug-class ENTRY** — per the source: *"answer only that medication/class
entry … Return only the short entry for the named drug or drug class. Do not list the whole protocol."*
Each source entry (across the cardiovascular, respiratory, endocrine, CNS/pain, urology,
immunomodulator, GI, insulin/glucose, antidiabetic, and antithrombotic blocks) is one prose section,
keyed by a snake-case name, with the entry's drug/brand names as section `aliases` and the entry text
copied **verbatim** into `text_en`.

- `default_answer` = the source DEFAULT_QUESTION verbatim ("Which medication or medication class are you
  asking about perioperatively?") — returned when no drug/class is named.
- No `default_section` (this is the multi-entry guide): a topic-less request **asks**, never dumps the
  whole protocol.

## How verbatim-return holds the safety line

- Antithrombotic entries (antiplatelets + anticoagulants) are returned **COMPLETE** — every timing row
  (low-risk, high-risk/neuraxial, epidural-catheter status, post-puncture restart, GFR splits, the
  coronary-stent sub-rules) is inside the single section `text_en` (source ANSWER_POLICY / SAFETY_RULES).
  No row is summarised away; the clinician reads the whole entry.
- No bridging, CHA₂DS₂-VASc, bleeding-risk, renal-category, or insulin-dose **computation** — the source
  forbids it; the tool only returns text.
- The **ticagrelor source conflict** (overview table 5 days vs detailed table 3 days) is preserved
  verbatim, with the "do not give a definitive single timing without local review" instruction.

## Flagged engine decisions (owner to confirm)

1. **Cross-listed drugs → clarify (not a silent pick).** A few drugs appear in TWO source entries:
   - **selexipag** — its own respiratory entry AND the triflusal/cilostazol/dipyridamole antiplatelet entry;
   - **prazosin, doxazosin** — the cardiovascular alpha-1-blocker entry AND the urology alpha-1-antagonist entry;
   - **sildenafil, tadalafil** — the respiratory PDE5 entry AND the urology PDE5 entry.

   These are aliased to **both** sections, so a query naming one returns a **clarify** ("which entry do
   you mean?") rather than guessing. This is the safe, faithful behaviour but adds a clarification step —
   please confirm it is acceptable (alternative: pin each to a single preferred entry).
2. **`no_match_answer` wording.** When a drug not in the protocol is named, the tool returns
   "This medication or class is not specified in the uploaded perioperative protocol." — a faithful
   rendering of the source SAFETY_RULE ("say it is not specified in the uploaded perioperative protocol"),
   which the source states as an instruction rather than a fixed output string. Confirm wording.
3. **Routing gate.** This protocol is active only with perioperative context — a bare drug name with no
   surgery/perioperative signal does NOT match (e.g. "aspirin dose" → unsupported, not this guide). The
   source REQUIRED_INFORMATION / routing_gate demands perioperative context.
4. **Steroid hand-off.** Systemic steroid stress-dosing is deferred to the separate `periop_steroids`
   guide (source PATHWAY_PRIORITY); inhaled corticosteroids ARE covered here (their own entry). A
   "perioperative steroid" query routes to `periop_steroids`, not here (alias span containment).
5. **Entries with no English-only label tokens** (organ-system block headers like
   "PERIOPERATIVE_CONTEXT_REQUIRED: yes") are NOT stored — only the drug/class entries are addressable.

## Spot-check sample (please verify a representative set against source)

| Section | Source entry anchor | In YAML |
|---|---|---|
| `aspirin` | "Usually no need to omit. / Omit only if high bleeding risk: spinal, intracranial, ophtalmic, TURP" | ✅ verbatim |
| `clopidogrel` | "Omit 5 days … coronary-stent sub-rules … Epidural catheter: forbidden … 6 hours if loading dose" | ✅ verbatim |
| `dabigatran` | "omit 1 day if GFR ≥50, 2 days if <50 / high-risk 2 vs 4 days / catheter forbidden / 2 hours" | ✅ verbatim |
| `warfarin` | "INR <1.5 … Overview 5 days … 2 hours … Bridging intentionally omitted" | ✅ verbatim |
| `metformin` | "omit if GFR <60, iodinated contrast, high-risk surgery, critical illness, organ failure" | ✅ verbatim |
| `sglt2_inhibitors` | "Omit 2 days before + morning of surgery … euglycemic ketoacidosis" | ✅ verbatim |
| `beta_blockers` | "Take/continue … reduces perioperative myocardial ischemia" | ✅ verbatim |

## Sign-off checklist

- [ ] Antithrombotic entries are complete (no dropped timing row) — spot-check several anticoagulants/antiplatelets.
- [ ] Cross-listed-drug clarify behaviour (selexipag/prazosin/doxazosin/sildenafil/tadalafil) is acceptable.
- [ ] `no_match_answer` and `default_answer` wording approved.
- [ ] Confirm no entry was paraphrased or summarised away from source.
