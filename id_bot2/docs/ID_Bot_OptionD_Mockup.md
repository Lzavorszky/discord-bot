# Option D — Concrete migration target

This shows what ID Bot looks like under the recommended hybrid: **one structured file per protocol**, a **small set of deterministic tools**, and a **tool-calling LLM router**. All clinical values below are copied verbatim from your current `meropenem.txt` and `joint_infection_pcr.txt` — nothing is invented.

The shift in one line: today a protocol is a `.txt` DSL + `.route_claims.json` + alias entries + sometimes Python; here it is **one validated file**, and the router/answer logic is **shared** across all protocols instead of re-implemented per gate.

---

## 1. The protocol files

Each protocol declares a `kind`, which tells the shared engine how to answer it. Three kinds cover almost everything you have: `drug_dose`, `pcr_panel`, `pathway`. A fourth, `prose`, handles bounded-text protocols like periop meds.

### 1a. `meropenem.yaml` — kind: drug_dose

(Replaces `meropenem.txt` **and** `meropenem.route_claims.json`. The dose table is now data the tool reads, not text the LLM reads.)

```yaml
id: meropenem
kind: drug_dose
source_label: meropenem
canonical_name: meropenem
status: draft
version: 0.3

aliases: [meropenem, mero, MEM, meronem, meropenemum, "meropenem iv"]

# --- routing metadata (was route_claims.json) ---
answers_intents: [dose]
refuses_intents: [coverage_question, targeted_treatment]   # "is mero good vs X?" -> not this protocol

# --- slots the tool accepts, with validation (was SLOT_SCHEMA) ---
slots:
  gfr:           { type: number, unit: mL/min, min: 0, max: 250, on_out_of_range: ask_confirmation }
  crrt:          { type: bool }
  ihd:           { type: bool }
  cns_infection: { type: bool }
  tdm_low_level: { type: bool }

# --- the actual dose data (was DEFAULT_ANSWER table + SELECTED_OUTPUTS) ---
tiers:
  LOADING:    { dose: "1 g once",  when: "start of therapy",         admin: "start continuous infusion immediately", always_show: true }
  NORMAL:     { dose: "3 g/day",   when: "GFR 20+",                   admin: "1 g/50 mL, 6.3 mL/h" }
  SEVERE_AKI: { dose: "1 g/day",   when: "GFR <20 or IHD",           admin: "0.5 g/50 mL, 4.2 mL/h" }
  CRRT:       { dose: "3 g/day",   when: "CRRT",                      admin: "1 g/50 mL, 6.3 mL/h" }
  STEP_UP:    { dose: "6 g/day",   when: "low levels or CNS infection", admin: "1 g/50 mL, 12.5 mL/h" }

# --- selection: ordered, declarative (was SELECTION_RULES) ---
select:
  - { if: "cns_infection or tdm_low_level", tier: STEP_UP }   # priority 110
  - { if: "ihd",        tier: SEVERE_AKI }                     # priority 100
  - { if: "crrt",       tier: CRRT }                           # priority  95
  - { if: "gfr >= 20",  tier: NORMAL }                         # priority  70
  - { if: "gfr < 20",   tier: SEVERE_AKI }                     # priority  60
  - { default: DEFAULT_ANSWER }                                # show full table

never:                       # was RESTRICTED_OUTPUTS
  - dosing outside the listed tiers
  - STEP_UP unless cns_infection or tdm_low_level is present
  - alternative antibiotics, duration, toxicity, or indication advice

footer: >
  Step-up dose is only for low exposure/TDM concern or CNS infection.
  Numeric GFR cutoff: Normal GFR 20+, Severe AKI GFR <20 or IHD.
```

Note: the priority numbers became list order; the IHD→SEVERE_AKI and "IHD beats CRRT beats GFR" safety rule is now just *the order of the list* — easy to read, hard to get wrong.

### 1b. `joint_infection_pcr.yaml` — kind: pcr_panel

(Replaces the 729-line `joint_infection_pcr.txt`. The organism table is verbatim from your file; I've shown a representative subset — the real file would carry all rows.)

```yaml
id: biofire_joint_infection
kind: pcr_panel
source_label: BioFire JI
canonical_name: BioFire Joint Infection Panel
status: draft

aliases: ["BioFire JI", "BioFire joint infection", "joint infection panel",
          "joint infection PCR", "JI panel", "JI PCR", "FilmArray JI"]

answers_intents: [test_interpretation]
requires: [detected_pathogen]          # marker alone -> ask for pathogen

# --- panel contents: this IS the "panel list" answer (was PCR_ORGANISM_MAPPING) ---
organisms:
  - { name: "Staphylococcus aureus",          tier: 1, therapy: "cefazolin",
      aliases: ["Staph aureus", "S. aureus"],
      marker_rules: ["mecA/C|mecA/B|MREJ -> vancomycin"] }
  - { name: "Klebsiella pneumoniae group",    tier: 1, therapy: "ceftriaxone",
      aliases: ["K. pneumoniae", "K pneumoniae"],
      marker_rules: ["CTX-M -> meropenem"] }
  - { name: "Klebsiella oxytoca",             tier: 1, therapy: "ceftriaxone",
      aliases: ["K. oxytoca"],
      marker_rules: ["CTX-M -> meropenem"] }
  - { name: "Enterobacter cloacae",           tier: 2, therapy: "cefepime",
      marker_rules: ["CTX-M -> meropenem"] }
  - { name: "Pseudomonas aeruginosa",         tier: 2, therapy: "cefepime" }
  - { name: "Enterococcus faecium",           tier: 1, therapy: "linezolid" }
  - { name: "Streptococcus agalactiae",       tier: 1, therapy: "ceftriaxone",
      aliases: ["GBS", "group B strep"] }
  # ... remaining rows copied verbatim from PCR_ORGANISM_MAPPING ...

markers: [CTX-M, KPC, NDM, OXA-48, mecA, MREJ, VanA, VanB]

# two rows whose genus alias collides -> engine asks which species
disambiguate_genus: [Klebsiella, Enterococcus, Streptococcus]

dose_via: link        # JI never doses; dosing is a separate get_dose call
```

### 1c. `cap.yaml` — kind: pathway (sketch)

```yaml
id: cap
kind: pathway
source_label: CAP
aliases: [CAP, "community acquired pneumonia", pneumonia, tudogyulladas, ...]
answers_intents: [empiric_treatment]
refuses_intents: [test_interpretation]      # "PCR Proteus" must NOT land here
slots:
  patient_status: { enum: [intubated, hospitalized, dischargeable] }
  nosocomial_risk: { type: bool }
  # ...
select:                                       # priority order = list order
  - { if: "patient_status == intubated", output: INTUBATED_CAP }
  - { if: "influenza", output: INFLUENZA }
  - { if: "patient_status == hospitalized and nosocomial_risk", output: HOSPITALIZED_NOSOCOMIAL_RISK }
  - { if: "patient_status == hospitalized", output: HOSPITALIZED_STANDARD }
  - { default: DEFAULT_ANSWER }
outputs:
  INTUBATED_CAP: { items: ["BioFire PN", ceftriaxone], text_hu: "...", text_en: "..." }
  # ...
doses: false        # CAP names drugs but never doses them -> get_dose is a separate call
```

---

## 2. The tools (the only things that touch clinical data)

The LLM may call these; it may never produce a dose, tier, or therapy itself. Each tool is thin — it reads a structured protocol file and runs your **existing, tested** `selection_engine` logic.

```python
def get_dose(drug_id: str, gfr: float|None=None, crrt: bool=False,
             ihd: bool=False, cns_infection: bool=False,
             tdm_low_level: bool=False) -> DoseResult
    # drug_id is a closed enum built from all kind:drug_dose files.
    # Runs the `select:` ladder. Returns the matched tier verbatim + source_label.
    # If GFR is out of range -> {needs_confirmation: true}. Never computes a novel dose.

def interpret_pcr(panel: str, organisms: list[str], markers: list[str]=[]) -> PcrResult
    # panel is a closed enum (joint_infection, pneumonia, ...).
    # If an organism string matches a `disambiguate_genus` with >1 species
    #   -> {needs_disambiguation: ["Klebsiella pneumoniae group", "Klebsiella oxytoca"]}
    # Applies marker_rules (e.g. CTX-M -> meropenem). Returns therapy verbatim per organism.

def select_pathway(protocol_id: str, slots: dict) -> PathwayResult
    # Runs a kind:pathway `select:` ladder (CAP, UTI, ...). Returns the named output.

def answer_from_section(protocol_id: str, section: str, lang: str) -> str
    # For kind:prose protocols (periop meds). Returns the section text for the LLM to format.
    # This is the C-style path: bounded text in, clean prose out.

def list_panel(panel: str) -> list[OrganismRow]          # "JiPCR panel list" -> the actual list
def ask_clarification(question: str) -> None              # explicit "I need X before answering"
```

The **router prompt** is short and stable (it does not grow per protocol):

> You are a router for a hospital protocol assistant. Classify the user's intent and call exactly one tool. You may never state a dose, tier, organism therapy, or recommendation that did not come from a tool result. If a drug or organism name is ambiguous, call `ask_clarification`. If no tool fits, say the request is not covered by the uploaded protocols. Conversation state: `{active_protocol, slots}`.

---

## 3. Your logged failures, traced through this design

**`Tazobactam dose`** → model maps "tazobactam" to the `get_dose` enum, finds **two** members (`piperacillin_tazobactam`, `ceftolozane_tazobactam`) → calls `ask_clarification("Which one — piperacillin/tazobactam or ceftolozane/tazobactam?")`. The silent wrong-drug pick is now structurally impossible, because `get_dose` takes one unambiguous `drug_id`.

**`Mi a meropenem dózisa?`** (TMP/SMX active, no slots) → state shows `active_protocol: tmpsmx, slots: {}`; "meropenem" is an exact different drug → model calls `get_dose("meropenem")` directly. No yes/no gate. (If TMP/SMX held unsaved patient slots, the prompt rule tells it to confirm first.)

**`JiPCR Klebsiella`** → `interpret_pcr(panel="joint_infection", organisms=["Klebsiella"])` → "Klebsiella" hits `disambiguate_genus` with two species → returns `needs_disambiguation` → model asks "K. pneumoniae group or K. oxytoca?". Organism can't be "ignored" because it's a required, validated argument.

**`JiPCR panel list`** → `list_panel("joint_infection")` → returns the organism table, not a priority recommendation.

**`ASA` (periop)** → `answer_from_section("periop_meds", "antithrombotic", lang)` → LLM formats the section into clean prose. The `"Aspirin; acetylsalicylic acid; ASA; …:"` heading-dump disappears because the alias chain is metadata, never rendered.

**`Tmpsmx high dose`** → `get_dose("tmpsmx", indication=..., request_type="high")` runs the same renal table you have, but now a regression test asserts `tier == HIGH_DOSE` — so the prophylaxis mis-pick gets caught in CI, not in a Telegram session.

---

## 4. Why config now shrinks (your stated goal)

Adding **cefepime dosing** today vs. under D:

| Step | Today | Option D |
|---|---|---|
| Protocol text | `cefepime.txt` (DSL) | `cefepime.yaml` |
| Route claims | `cefepime.route_claims.json` | (same file) |
| Aliases | edit `aliases.json` | (same file) |
| Router code | maybe a constant in `routing.py` | none |
| Selection code | maybe in `selection_engine.py` | none (generic `drug_dose` engine) |
| Drug enum | n/a | auto-built from files at load |
| Validation | linter (partial) | loader rejects malformed file in CI |

One file, validated on load. The router prompt, the tools, and the engines are untouched. That is the definition of *additive* configuration.

---

## 5. What you reuse vs. rebuild

**Reuse (repackage, don't rewrite):** the `selection_engine` priority-rules / table-lookup / pcr-mapping / calculator logic (it's correct where tested — it just becomes the body of `get_dose` / `interpret_pcr` / `select_pathway`); the post-processor; the embeddings cache (still used for `answer_from_section` retrieval on prose protocols); the audit envelope.

**Rebuild:** the protocol loader (now one schema + validator instead of the dual old/new DSL parser); the router (LLM tool-calling replaces `routing.resolve_route` + the 15 orchestrator gates + the alias cascade); a small grounding verifier on the LLM-prose path.

**Delete eventually:** `routing.py`'s regex/alias cascade, the old protocol schema in `protocol_parser.py`, the `.route_claims.json` sidecars, and most of the per-gate boilerplate in `_ask_ai_impl`.

---

## 6. Suggested first migration step

Pick **meropenem** as the pilot: it's a pure `drug_dose`, fully self-contained, and already in your logs. Convert it to `meropenem.yaml`, implement `get_dose` over the one file, wire it behind a flag, and run the meropenem rows of your regression set against both the old and new path. When they match (plus the switch-friction case now passes), you've validated the whole `drug_dose` kind — and ~20 of your 48 protocols are that kind, so they migrate almost mechanically afterward.
