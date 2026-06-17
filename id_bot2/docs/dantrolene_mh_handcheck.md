# dantrolene_mh — clinical hand-check sheet

**Source:** `protocols/dantrolene_mh.txt` (v0.1, status: draft, owner: ID team)
**Migrated to:** `id_bot2/protocols/dantrolene_mh.yaml` (`kind: prose`)
**Tool:** `answer_from_section` (SELECTS one verbatim section; never composes, never computes)
**Migrated:** 2026-06-17 · **Owner sign-off: PENDING**

---

## What this protocol is

An info-only, **single-block** guide. ANY dantrolene / Dantrium / Agilus / malignant-hyperthermia
request returns the **whole** Hungarian guideline verbatim. There is no selection and no computation.

Modelled as ONE prose section `guideline` with `default_section: guideline`, so the guide is always
returned whole regardless of how the question is phrased.

## Flagged engine decisions (owner to confirm)

1. **Modelled as `prose`, NOT `calculator` — deliberate.** The source is labelled
   `protocol_type: drug_dosing_protocol`, but its SAFETY_RULE states: *"If a body weight is supplied,
   still return the full guideline text. Do not calculate new ampoule counts or Agilus volumes beyond
   the explicit 60 kg, 80 kg, and 100 kg examples."* So it is verbatim-return, not a calculator. A
   supplied body weight does **not** change the answer (test: `test_dantrolene_with_weight_still_returns_whole_guide`).
2. **No phrasing rewrite.** Prose answers are returned verbatim (verifier soft-mode); the HU text is
   shown exactly as in source.

## Side-by-side check (every clinical fact — please verify against source)

| Source `## DEFAULT_ANSWER` | In YAML `sections.guideline.text_hu` |
|---|---|
| Dantrium: 12 ampulla / doboz (össz: 240 mg / doboz); 20 mg / amp | ✅ verbatim |
| Dantrium kezdő adag: 2,5 mg/ttkg; 60 kg→8 amp, 80 kg→10 amp, 100 kg→12 amp | ✅ verbatim |
| Agilus: 6 ampulla / doboz (össz: 720 mg / doboz); 120 mg (!) / amp | ✅ verbatim |
| Agilus kezdő adag: 2,5 mg/ttkg; 60 kg→25 ml, 80 kg→32 ml, 100 kg→40 ml | ✅ verbatim |
| Oldat elkészítése — Dantrium: 60 ml víz, szűrő; Agilus: 20 ml víz, narancssárga | ✅ verbatim |
| Fenntartó dózis: 10 mg/ttkg / 24 h; 2,5 mg/ttkg 10 percenként | ✅ verbatim |
| Eltarthatóság — Dantrium 6 óra; Agilus 24 óra; NEM HŰTHETŐ; FÉNYTŐL VÉDENI | ✅ verbatim |

## Routing

- Aliases (protocol + section): dantrolene/dantrolen/dantrium/agilus/malignant+malignus hyperthermia/MH.
- Resolved in the **prose** stage (after the drug stage). No antibiotic dose request matches these aliases.
- Sign-off checklist: confirm (a) the full HU guideline is reproduced exactly, including the `(!)` on the
  Agilus 120 mg ampoule strength and the "NEM HŰTHETŐ! / FÉNYTŐL VÉDENI KELL!" warnings; (b) it is correct
  that a body weight never triggers a computed ampoule count.
