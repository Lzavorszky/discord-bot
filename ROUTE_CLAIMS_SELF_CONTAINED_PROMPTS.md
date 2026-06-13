# Route Claims Migration: Self-Contained Slice Prompts

Use this file when you want to copy-paste exactly one prompt per migration slice. Each prompt includes the necessary context, target architecture, and safety constraints for that slice.

Core migration idea repeated in every slice:

```text
Aliases create typed evidence.
Protocols claim evidence.
The router decides: route, clarify, unsupported, active context, or fallback.
Protocols generate the answer.
```

Target evidence entities stay intentionally small:

```text
intent, subject, microbe, marker, test, context
```

Do not broaden clinical behavior unless an uploaded protocol explicitly claims it.

## Slice 1: Characterize Current Routing

```text
We are working in ID_bot. The goal is to migrate routing from "longest alias wins" to an evidence + protocol-owned route claims architecture.

Current problem:
- aliases.py::normalize_question() effectively chooses one alias winner, often by exact match and alias length.
- Broad aliases such as "pneumonia" or typo aliases such as "penumonia" can beat stronger clinical evidence such as "PCR + Proteus".
- Example unsafe behavior: "Penumonia PCR Proteus" may route to CAP instead of BioFire/PCR interpretation or panel clarification.

Target architecture later:
chat text -> typed evidence -> protocol route claims -> route/clarify/unsupported -> existing protocol dispatcher.

For this slice, do not change production routing behavior. Add characterization tests that document current behavior for alias conflicts.

Focus cases:
- "Penumonia PCR Proteus"
- "BioFire PN Proteus"
- "PCR Proteus"
- "Proteus pneumonia"
- "meropenem dose GFR 35"
- "is meropenem good vs staphylococcal pneumonia"
- "pneumonia what antibiotics"
- "aspirin before surgery"
- "hydrocortisone to prednisolone"

Add tests showing current route/answer class, even if behavior is wrong. Do not fix yet. Run the relevant test suite. Report exactly which current behaviors are unsafe and which are already acceptable.
```

## Slice 2: Add Evidence Types, No Behavior Change

```text
We are working in ID_bot. The goal is to migrate routing from "longest alias wins" to an evidence + protocol-owned route claims architecture.

Core principle:
Aliases create typed evidence. Protocols claim evidence. The router decides route/clarify/unsupported. Protocols generate answers.

Current problem:
- aliases.py::normalize_question() chooses one winning alias too early.
- We need a typed evidence layer before deciding protocol ownership.

For this slice, add a new routing evidence layer without changing production routing.

Create small data structures for:
- RoutingEvidence
- RoutingSubject
- RoutingTest
- RouteDecision
- EvidenceMatch

Keep global entity types small:
- intent
- subject
- microbe
- marker
- test
- context

Suggested evidence shape:

RoutingEvidence(
    intent="dose | empiric_treatment | targeted_treatment | test_interpretation | periop_advice | calculator | conversion | diagnosis | unknown",
    subject={"kind": "drug | syndrome | test_panel | calculator | periop_med | steroid | unknown", "name": None},
    microbes=[],
    markers=[],
    test={"family": None, "panel": None},
    context={},
)

Suggested route decision shape:

RouteDecision(kind="route", protocol_file="...")
RouteDecision(kind="clarify", message="...")
RouteDecision(kind="unsupported", message="...")
RouteDecision(kind="use_active_context")
RouteDecision(kind="fallthrough")

Add extract_routing_evidence(question, state=None) that collects typed evidence from existing aliases and simple regexes, but do not use it to route yet.

Add unit tests for evidence extraction only. Existing bot answers must not change.
```

## Slice 3: Collect All Alias Matches

```text
We are working in ID_bot. The goal is to migrate routing from "longest alias wins" to an evidence + protocol-owned route claims architecture.

Current problem:
- aliases.py::normalize_question() can find multiple exact matches but chooses one winner too early.
- We need to collect all relevant alias evidence before routing.

Core principle:
Aliases create typed evidence. They should not directly choose the final protocol when multiple clinical meanings are present.

For this slice, refactor alias recognition so the system can collect all matching aliases instead of only the longest winner.

Do not replace normalize_question() yet. Add a new function, e.g. collect_alias_matches(question), that returns all exact and high-confidence fuzzy matches with:
- alias
- category
- protocol_file
- canonical
- display
- confidence
- span or matched text if easy

Use collect_alias_matches() inside extract_routing_evidence().

Keep current normalize_question() behavior unchanged for production routing.

Add tests proving multiple matches are visible for:
- "Penumonia PCR Proteus"
- "BioFire PN Proteus"
- "pneumonia pcr proteus"
- "meropenem dose GFR 35"
- "aspirin before surgery"

Run tests and confirm existing bot answers still do not change.
```

## Slice 4: Add Protocol Route Claims Schema

```text
We are working in ID_bot. The goal is to migrate routing from "longest alias wins" to an evidence + protocol-owned route claims architecture.

Core principle:
Aliases create typed evidence. Protocols claim evidence. The router decides route/clarify/unsupported.

For this slice, add support for protocol-owned ROUTE_CLAIMS metadata, but do not route with it yet.

Start with only a minimal schema:
- intents
- subjects
- owns
- requires
- excludes
- clarify_if_missing

Use either protocol-file sections or a sidecar JSON/YAML file, whichever fits the current parser best. Prefer the path that is easiest to lint and test.

Add initial claims for:
- BioFire PN
- BioFire JI
- CAP
- meropenem as one representative antibiotic dosing protocol
- periop medications
- steroid equivalence

Example BioFire PN claim:

ROUTE_CLAIMS:
  intents:
    - test_interpretation
  subjects:
    - test_panel
  owns:
    tests:
      - biofire
      - pcr
      - filmarray
    panels:
      - pn
      - pneumonia
    microbes:
      source: pcr_organism_aliases
    markers:
      source: pcr_resistance_marker_aliases
  requires:
    - test
    - microbe_or_marker
  clarify_if_missing:
    - panel

Example CAP claim:

ROUTE_CLAIMS:
  intents:
    - empiric_treatment
  subjects:
    - syndrome
  owns:
    syndromes:
      - cap
      - community_acquired_pneumonia
      - pneumonia
  excludes:
    - test_interpretation
    - microbe_present_without_targeted_therapy

Example meropenem claim:

ROUTE_CLAIMS:
  intents:
    - dose
  subjects:
    - drug
  owns:
    drugs:
      - meropenem
  excludes:
    - targeted_treatment
    - coverage_question

Add parser/linter tests. Existing runtime behavior must not change.
```

## Slice 5: Build Resolver In Shadow Mode

```text
We are working in ID_bot. The goal is to migrate routing from "longest alias wins" to an evidence + protocol-owned route claims architecture.

Core principle:
Aliases create typed evidence. Protocols claim evidence. The router decides route/clarify/unsupported. Protocols generate answers.

For this slice, implement resolve_route(evidence, protocol_claims, state) in shadow mode only.

It should return one of:
- RouteDecision(kind="route", protocol_file="...")
- RouteDecision(kind="clarify", message="...")
- RouteDecision(kind="unsupported", message="...")
- RouteDecision(kind="use_active_context")
- RouteDecision(kind="fallthrough")

Decision table:
1. drug + dose intent -> drug dosing protocol
2. drug + coverage/targeted-treatment intent + microbe/syndrome -> route only if a protocol explicitly claims that coverage/targeted question; otherwise unsupported or clarify PCR/result if relevant
3. test + microbe/marker + known panel -> matching PCR protocol
4. test + microbe/marker + unknown panel -> find PCR protocols that contain the microbe/marker; if multiple, ask panel clarification
5. microbe + syndrome -> targeted syndrome protocol if explicitly claimed; otherwise if microbe is on a relevant PCR panel, ask if this is PCR/BioFire result; otherwise unsupported
6. syndrome + empiric intent -> matching syndrome protocol
7. surgery/periop + medication/steroid -> periop medication or periop steroid protocol
8. conversion/calculator intent -> calculator/conversion protocol
9. unsupported syndrome -> block unless explicit drug dosing question

Safety constraints:
- Do not route PCR/BioFire/result + microbe/marker to CAP.
- Do not answer drug coverage questions from a dosing protocol.
- Do not route a bare microbe to BioFire without test/result context.
- Do not silently reinterpret dangerous typos like "staphylococcus penumonia".

Expose shadow decisions in tests or debug output, but do not affect live answers yet.

Add resolver tests for:
- "Penumonia PCR Proteus"
- "BioFire PN Proteus"
- "PCR Proteus"
- "Proteus pneumonia"
- "meropenem dose GFR 35"
- "is meropenem good vs staphylococcal pneumonia"
- "pneumonia what antibiotics"
- "aspirin before surgery"
- "hydrocortisone to prednisolone"
```

## Slice 6: Enable High-Risk Router Gates

```text
We are working in ID_bot. The goal is to migrate routing from "longest alias wins" to an evidence + protocol-owned route claims architecture.

Core principle:
Aliases create typed evidence. Protocols claim evidence. The router decides route/clarify/unsupported.

The resolver from previous slices should now be enabled only for high-risk ambiguity cases. Keep all other messages on the old routing path.

Intercept before normalize_question() winner routing for:
- test/PCR/BioFire/result + microbe or marker
- microbe + pneumonia or another infectious syndrome
- drug + coverage intent, e.g. "good against", "covers", "vs", "active against"
- unsupported pneumonia variants such as HAP/VAP/immunosuppressed pneumonia

For these intercepted cases:
- route deterministically if the route is safe and explicit
- ask narrow clarification if panel/source/syndrome is unclear
- return unsupported if no uploaded protocol claims the requested behavior
- do not let RAG/LLM invent antimicrobial coverage advice

Expected target behavior:
- "Penumonia PCR Proteus" must not route to CAP.
- "PCR Proteus" should ask panel/source clarification if needed.
- "BioFire PN Proteus" should route to BioFire PN.
- "Proteus pneumonia" should not become CAP targeted therapy.
- "is meropenem good vs staphylococcal pneumonia" should not provide general spectrum advice.
- "meropenem dose GFR 35" should still route to meropenem dosing.

Add regression tests for all of the above. Run the relevant test suite.
```

## Slice 7: Expand Claims Across Current Protocols

```text
We are working in ID_bot. The goal is to migrate routing from "longest alias wins" to an evidence + protocol-owned route claims architecture.

Core principle:
Aliases create typed evidence. Protocols claim evidence. The router decides route/clarify/unsupported.

For this slice, add route claims for all current protocol families:
- antibiotic dosing protocols
- infectious syndrome/pathway protocols
- BioFire PN and JI PCR
- periop medications
- periop steroids
- steroid equivalence
- body size and echo calculators
- diagnostic protocols such as SBP/C. diff where applicable

Keep claims minimal. Do not add new clinical behavior. Claims should only describe what each uploaded protocol already owns.

Claim rules:
- drug dosing protocols own dose intent for their drug
- drug dosing protocols exclude coverage_question and targeted_treatment unless explicitly authored
- microbiology interpretation protocols own test_interpretation and require microbe_or_marker
- syndrome protocols own empiric_treatment or diagnosis for their syndrome
- syndrome protocols do not own targeted microbe therapy unless explicitly authored
- calculator protocols own calculator/conversion intent and their required measurements/entities
- periop protocols own periop_advice intent and surgery/periop context

Add linter checks:
- every protocol has route claims or explicitly opts out
- drug dosing protocols exclude coverage/targeted-treatment intent
- microbiology protocols require microbe_or_marker
- broad syndrome aliases are weak/fallback and must not override test/microbe evidence

Run the full test suite.
```

## Slice 8: Replace Longest-Alias Routing

```text
We are working in ID_bot. The goal is to migrate routing from "longest alias wins" to an evidence + protocol-owned route claims architecture.

Core principle:
Aliases create typed evidence. Protocols claim evidence. The router decides route/clarify/unsupported. Protocols generate answers.

For this slice, switch normal routing from longest-alias-wins to evidence + route claims.

Primary selection should be:
1. extract_routing_evidence(question, state)
2. resolve_route(evidence, protocol_claims, state)
3. if route: dispatch selected protocol through existing machinery
4. if clarify: return clarification
5. if unsupported: return unsupported protocol message
6. if use_active_context: continue active context
7. if fallthrough: use legacy routing only as fallback

Preserve existing deterministic engines:
- dispatch_tree()
- selection_engine.py
- dosing shortcuts
- protocol footers/sources
- safety footer behavior

Add a debug trace showing:
- evidence found
- candidate protocols
- route decision
- reason

Safety constraints:
- PCR/BioFire/result + microbe/marker must not route to CAP.
- Drug coverage questions must not be answered from dosing protocols.
- Bare microbes must not auto-route to BioFire.
- Unsupported syndrome policy must still block unsupported requests.

Run the full test suite. Compare outputs for known safe flows and confirm high-risk flows now use the new router.
```

## Slice 9: Remove Legacy Footguns

```text
We are working in ID_bot. The goal is to migrate routing from "longest alias wins" to an evidence + protocol-owned route claims architecture.

At this point, the new evidence + route claims router should be primary.

For this slice, demote or remove dangerous broad aliases from direct routing:
- pneumonia
- penumonia
- pneuomonia
- tüdőgyulladás / tudogyulladas
- leguti
- lrti

These terms may still produce weak syndrome evidence, but must not directly select CAP when stronger evidence exists.

They must not override:
- PCR/BioFire/result evidence
- microbe/marker evidence
- drug coverage intent
- unsupported syndrome policy

Update tests so:
- "Penumonia PCR Proteus" can never route to CAP
- "pneumonia" alone still routes to CAP for now
- future HAP/VAP support can change "pneumonia" alone to a CAP/HAP/VAP clarification without rewriting the router

Remove or update obsolete characterization expectations from Slice 1 that documented unsafe behavior.

Run the full test suite.
```

## Slice 10: Cleanup And Documentation

```text
We are working in ID_bot. The migration goal was to replace "longest alias wins" routing with an evidence + protocol-owned route claims architecture.

Core architecture:
chat text -> typed evidence -> protocol claims -> route/clarify/unsupported/use active/fallback -> existing protocol dispatcher.

Core principle:
Aliases create typed evidence. Protocols claim evidence. The router decides route/clarify/unsupported. Protocols generate answers.

For this slice, clean up the migration.

Tasks:
- document the routing architecture in a markdown file
- document how to add route claims for a new protocol
- document how to add aliases as evidence rather than direct routes
- remove dead code if any legacy routing path is no longer used
- keep legacy fallback only if there are tests proving it is needed
- add examples for BioFire PN, BioFire JI, CAP, antibiotic dosing, periop medications, and calculators

Documentation should explain:
- aliases create evidence
- protocols claim evidence
- the router picks route/clarify/unsupported
- protocols produce answers
- broad syndrome aliases are weak evidence
- drug dosing protocols are not coverage protocols unless explicitly authored
- PCR/BioFire/result + microbe/marker must not route to CAP

Run the full test suite and include a short final summary of changed routing behavior.
```

