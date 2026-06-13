# Routing Architecture

ID_bot routes user text through typed evidence and protocol-owned route claims. The router decides whether the request can be answered by an uploaded protocol; the selected protocol still owns the answer content.

## Flow

```text
chat text
  -> typed evidence
  -> protocol route claims
  -> route / clarify / unsupported / use active / fallback
  -> existing deterministic protocol dispatcher or RAG answer path
```

The core rule is:

```text
Aliases create typed evidence. Protocols claim evidence. The router decides. Protocols answer.
```

## Responsibilities

Aliases do not directly own routing decisions. `protocols/aliases.json` maps words and phrases to typed evidence such as drug, syndrome, test panel, perioperative medication, steroid, or calculator evidence. Broad syndrome aliases can be marked weak so they do not win just because they are long or common.

Protocols own routing through adjacent `*.route_claims.json` files or an inline `## ROUTE_CLAIMS` block. Claims describe what intents and evidence a protocol can answer: dose, empiric treatment, test interpretation, perioperative advice, calculator use, conversion, and so on.

The router compares the typed evidence with loaded protocol claims and returns one decision:

- `route`: a protocol explicitly claims the evidence.
- `clarify`: the evidence is probably answerable, but an essential source is missing, such as the PCR panel.
- `unsupported`: no uploaded protocol claims the request, or an unsupported syndrome policy blocks it.
- `use_active_context`: no fresh route was claimed, but an active protocol exists and the turn looks like a follow-up.
- `fallthrough`: no claim matched, so legacy alias/RAG fallback may continue.

Protocols produce answers after routing. A route claim only says "this protocol owns this kind of request"; it must not invent clinical content outside the protocol.

## Adding Route Claims For A New Protocol

Create `protocols/<protocol_id>.route_claims.json` next to the protocol text file. Use explicit claims and keep them narrower than the alias list.

Required shape:

```json
{
  "intents": ["dose"],
  "subjects": ["drug"],
  "owns": {
    "drugs": ["example_drug"]
  },
  "requires": ["drug"],
  "excludes": ["coverage_question", "targeted_treatment"],
  "clarify_if_missing": []
}
```

Guidelines:

- `intents` says what question type the protocol can answer.
- `subjects` says what kind of subject evidence it handles.
- `owns` lists the exact evidence the protocol claims, or references a protocol alias panel with `{"source": "pcr_organism_aliases"}`.
- `requires` lists evidence needed before the router should route.
- `excludes` documents evidence that must not route here.
- `clarify_if_missing` names missing evidence that should produce a clarification prompt rather than unsafe fallback.
- Use `{"opt_out": ["reason"]}` only for files that are intentionally not routable protocols.

Run the route-claims schema tests and linter after adding or changing claims.

## Adding Aliases As Evidence

Add aliases in `protocols/aliases.json` to help the evidence extractor recognize the user's words. Do not rely on aliases as direct routes.

When adding an alias:

- Put drug names under `drugs`; put syndrome, panel, periop, and calculator names under `conditions`.
- Keep aliases specific when possible, for example `biofire pn`, `pneumonia pcr`, `meropenem dose`, or `body size calculator`.
- Put broad syndrome terms such as `pneumonia` in `weak_aliases` when they can collide with tests, organisms, or unsupported syndromes.
- Add unsupported syndrome policies for broad phrases that are not covered by uploaded protocols, such as HAP, VAP, or immunosuppressed pneumonia.
- Let route claims decide whether the evidence is enough to route.

## Safety Rules

Broad syndrome aliases are weak evidence. `pneumonia` can help CAP route only when the query is an empiric CAP-style request or a weak fallback with no stronger test, result, organism, marker, or drug coverage evidence.

Drug dosing protocols are not coverage protocols unless explicitly authored as such. A meropenem dosing protocol may answer `meropenem dose GFR 35`; it must not answer `is meropenem good vs staphylococcal pneumonia` unless a targeted-treatment or coverage claim exists.

PCR, BioFire, result, microbe, and resistance-marker evidence must not route to CAP. If a user says `PCR Proteus`, `pneumonia result Proteus`, or `Proteus pneumonia`, the router should route to a PCR protocol only when the panel and organism/marker are claimed, ask for the panel when needed, or mark the request unsupported. It must not select CAP just because `pneumonia` appeared.

Keep legacy alias fallback only where tests prove it is still needed. Current fallback is retained for ordinary non-conflict turns and compatibility paths, while route claims intercept high-risk conflicts before longest-alias behavior can choose a protocol.

## Examples

### BioFire PN

`protocols/pneumonia_pcr.route_claims.json` claims `test_interpretation` for PCR/BioFire/FilmArray evidence, owns the PN/pneumonia panel, and owns microbes/markers from the protocol's PCR alias panels.

Expected behavior:

- `BioFire PN Proteus` routes to `protocols/pneumonia_pcr.txt`.
- `Pneumonia PCR Proteus` routes to PN, even though `pneumonia` is also CAP evidence.
- `PCR Proteus` asks which PCR/BioFire panel because panel source is missing.

### BioFire JI

`protocols/joint_infection_pcr.route_claims.json` also claims `test_interpretation`, but owns the JI/joint-infection panel. Joint infection aliases create test-panel evidence; organisms and markers still come from typed PCR evidence.

Expected behavior:

- `BioFire JI staph aureus` routes to `protocols/joint_infection_pcr.txt` when the organism is claimed there.
- `joint infection pcr mecA` routes to JI if the marker is claimed.
- A joint infection phrase alone must not route to the pneumonia PCR protocol.

### CAP

`protocols/cap.route_claims.json` claims `empiric_treatment` for syndrome evidence owned by CAP/community-acquired pneumonia. It excludes test interpretation and microbe-present-without-targeted-therapy cases.

Expected behavior:

- `pneumonia what antibiotics` routes to CAP.
- `pneumonia` can route to CAP as weak fallback when no stronger evidence is present.
- `pneumonia result Proteus` and `Proteus pneumonia` do not route to CAP.

### Antibiotic Dosing

Each dosing protocol, such as `protocols/antibiotics/meropenem.route_claims.json`, claims `dose`, subject `drug`, and its own drug name. Drug dosing claims exclude `coverage_question` and `targeted_treatment`.

Expected behavior:

- `meropenem dose GFR 35` routes to the meropenem dosing protocol.
- `meropenem GFR 35` can route to dosing because renal-function evidence implies a dosing question.
- `is meropenem good vs pneumonia` is unsupported unless a protocol explicitly claims coverage or targeted treatment for that evidence.

### Periop Medications

`protocols/periop_gyogyszerek.route_claims.json` claims perioperative medication management and owns perioperative contexts such as preoperative, surgery day, and neuraxial. Aliases such as `before surgery`, `periop medication`, or `ASA` create evidence; the periop protocol decides whether it can answer.

Expected behavior:

- `aspirin before surgery` routes to perioperative medications.
- Perioperative steroid stress-dose questions should route to the steroid-specific periop protocol when steroid evidence is present.
- A medication alias without perioperative context should not become periop advice by alias length alone.

### Calculators

Calculator protocols claim `calculator` or conversion intents and own calculator names plus required measurements.

Examples:

- `protocols/body_size_calculators.route_claims.json` owns body size, BMI, BSA, IBW, ABW, and AdjBW calculators and requires height/weight.
- `protocols/echo_ava.route_claims.json` owns AVA/continuity-equation calculator evidence and requires LVOT diameter, LVOT VTI, and AV VTI.
- `protocols/steroid_equivalence.route_claims.json` owns steroid equivalence conversion and steroid drug evidence, requiring a steroid and dose.

Expected behavior:

- `190cm, 130kg` routes to body size calculations by input signature.
- `Calculate AVA LVOT VTI 20cm, AV VTI 80cm LVOT diam 18mm` routes to echo AVA.
- `hydrocortisone to prednisolone` routes to steroid equivalence and asks for the missing dose.
