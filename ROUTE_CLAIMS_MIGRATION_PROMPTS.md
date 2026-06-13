# Route Claims Migration Prompts

## Shared Context For Every Slice

We are migrating `ID_bot` from a fragile "longest alias wins" router to an evidence + protocol-owned route claims router.

Current problem:

- `aliases.py::normalize_question()` collects exact alias matches, sorts aliases by string length, and chooses the first match.
- This means broad aliases such as `pneumonia`, typo aliases such as `penumonia`, or other long aliases can win over clinically stronger evidence.
- Example failure: `Penumonia PCR Proteus` can route to CAP instead of BioFire/PCR interpretation or PCR-panel clarification.

Target architecture:

```text
chat text
-> extract typed evidence
-> compare evidence with protocol route claims
-> route / clarify / unsupported / active-context fallback
-> dispatch existing deterministic protocol machinery
```

Core principle:

```text
Aliases create evidence.
Protocols claim evidence.
The router picks the owner or asks clarification.
Protocols generate the answer.
```

Keep global routing entities small:

```text
intent
subject
microbe
marker
test
context
```

Suggested evidence shape:

```python
RoutingEvidence(
    intent="dose | empiric_treatment | targeted_treatment | test_interpretation | periop_advice | calculator | conversion | diagnosis | unknown",
    subject={
        "kind": "drug | syndrome | test_panel | calculator | periop_med | steroid | unknown",
        "name": None,
    },
    microbes=[],
    markers=[],
    test={
        "family": None,
        "panel": None,
    },
    context={},
)
```

Suggested route decision shape:

```python
RouteDecision(kind="route", protocol_file="protocols/pneumonia_pcr.txt")
RouteDecision(kind="clarify", message="Which PCR panel: BioFire PN or BioFire JI?")
RouteDecision(kind="unsupported", message="No uploaded protocol covers...")
RouteDecision(kind="use_active_context")
RouteDecision(kind="fallthrough")
```

Important safety constraints:

- Do not broaden clinical behavior unless a protocol explicitly claims it.
- Do not let drug coverage questions become general antimicrobial advice.
- Do not route `PCR/BioFire/result + microbe/marker` to CAP.
- Do not route a bare microbe to BioFire without test/result context.
- Do not silently reinterpret dangerous typos like `staphylococcus penumonia`.
- Keep existing deterministic answer machinery where possible: `dispatch_tree()`, `selection_engine.py`, dosing shortcuts, footers, and sources.

Use these examples throughout the migration:

```text
Penumonia PCR Proteus
BioFire PN Proteus
PCR Proteus
Proteus pneumonia
meropenem dose GFR 35
is meropenem good vs staphylococcal pneumonia
pneumonia what antibiotics
aspirin before surgery
hydrocortisone to prednisolone
```

Expected target behavior examples:

```text
BioFire PN Proteus
-> BioFire PN protocol

PCR Proteus
-> clarify panel if multiple PCR panels could own Proteus

Penumonia PCR Proteus
-> test/PCR + microbe evidence must not route to CAP
-> clarify panel or BioFire PN depending claims/strictness

Proteus pneumonia
-> no targeted pneumonia protocol currently
-> if Proteus is on a relevant PCR panel, ask if this is a PCR/BioFire result

meropenem dose GFR 35
-> meropenem dosing protocol

is meropenem good vs staphylococcal pneumonia
-> drug coverage/targeted-treatment intent
-> no uploaded protocol covers that targeted coverage question
-> ask whether user means BioFire PN/PCR positive for Staphylococcus aureus, if relevant

pneumonia what antibiotics
-> CAP for now
-> future HAP/VAP protocols may require CAP/HAP/VAP clarification

aspirin before surgery
-> perioperative medication protocol

hydrocortisone to prednisolone
-> steroid equivalence calculator
```

## Slice 1: Characterize Current Routing

Copy-paste prompt:

```text
We are migrating ID_bot from longest-alias-wins routing to evidence + protocol-owned route claims. First, do not change routing behavior. Add characterization tests that document current behavior for alias conflicts.

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

Definition of done:

- Tests exist for the listed examples.
- Existing behavior is documented without changing runtime behavior.
- Unsafe current behaviors are named clearly.

## Slice 2: Add Evidence Types, No Behavior Change

Copy-paste prompt:

```text
Add a new routing evidence layer without changing production routing.

Create small data structures for:
- RoutingEvidence
- RoutingSubject
- RoutingTest
- RouteDecision
- EvidenceMatch

Add extract_routing_evidence(question, state=None) that collects typed evidence from existing aliases and simple regexes, but do not use it to route yet.

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

Add unit tests for evidence extraction only. Existing bot answers must not change.
```

Definition of done:

- Evidence structures exist.
- Evidence extraction works in tests.
- Existing routing and answers are unchanged.

## Slice 3: Collect All Alias Matches

Copy-paste prompt:

```text
Refactor alias recognition so the system can collect all matching aliases instead of only the longest winner.

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

Definition of done:

- Multiple alias matches can be inspected.
- Existing `normalize_question()` still behaves as before.
- Evidence extraction can see conflicts that old routing hid.

## Slice 4: Add Protocol Route Claims Schema

Copy-paste prompt:

```text
Add support for protocol-owned ROUTE_CLAIMS metadata, but do not route with it yet.

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

Definition of done:

- Claims can be parsed or loaded.
- Claims are tested.
- Runtime routing is unchanged.

## Slice 5: Build Resolver In Shadow Mode

Copy-paste prompt:

```text
Implement resolve_route(evidence, protocol_claims, state) in shadow mode only.

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

Definition of done:

- Resolver returns intended decisions in tests.
- Runtime still uses old routing unless explicitly in test/debug mode.
- Shadow output includes enough reason text to debug.

## Slice 6: Enable High-Risk Router Gates

Copy-paste prompt:

```text
Enable the new evidence resolver only for high-risk ambiguity cases.

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

Keep all other messages on the old routing path.

Add regression tests proving:
- "Penumonia PCR Proteus" cannot route to CAP
- "PCR Proteus" asks panel/source clarification if needed
- "BioFire PN Proteus" routes to BioFire PN
- "Proteus pneumonia" does not become CAP targeted therapy
- "is meropenem good vs staphylococcal pneumonia" does not provide general spectrum advice
- "meropenem dose GFR 35" still routes to meropenem dosing
```

Definition of done:

- High-risk ambiguity cases are controlled by the new resolver.
- Low-risk existing behavior remains intact.
- Tests prove unsafe routes are blocked.

## Slice 7: Expand Claims Across Current Protocols

Copy-paste prompt:

```text
Add route claims for all current protocol families:
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

Definition of done:

- All current protocols have minimal route claims or explicit opt-out.
- Linter enforces the most important safety constraints.
- Full tests pass.

## Slice 8: Replace Longest-Alias Routing

Copy-paste prompt:

```text
Switch normal routing from longest-alias-wins to evidence + route claims.

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

Run the full test suite. Compare outputs for known safe flows and confirm high-risk flows now use the new router.
```

Definition of done:

- The new router is the primary routing mechanism.
- Legacy alias routing is fallback only.
- Debug trace makes route decisions auditable.

## Slice 9: Remove Legacy Footguns

Copy-paste prompt:

```text
After the new router passes regression tests, demote or remove dangerous broad aliases from direct routing:
- pneumonia
- penumonia
- pneuomonia
- tüdőgyulladás
- tudogyulladas
- leguti
- lrti

These may still produce weak syndrome evidence, but must not directly select CAP when stronger evidence exists.

They must not override:
- PCR/BioFire/result evidence
- microbe/marker evidence
- drug coverage intent
- unsupported syndrome policy

Update tests so:
- "Penumonia PCR Proteus" can never route to CAP
- "pneumonia" alone still routes to CAP for now
- future HAP/VAP support can change "pneumonia" alone to a CAP/HAP/VAP clarification without rewriting the router

Run the full test suite and remove any obsolete characterization expectations from Slice 1 that documented unsafe behavior.
```

Definition of done:

- Broad aliases are weak evidence, not protocol owners.
- Known unsafe failure cases are impossible by tests.
- Safe existing simple queries still work.

## Slice 10: Cleanup And Documentation

Copy-paste prompt:

```text
Clean up the migration after route claims are primary.

Tasks:
- document the routing architecture in a markdown file
- document how to add route claims for a new protocol
- document how to add aliases as evidence rather than direct routes
- remove dead code if any legacy routing path is no longer used
- keep legacy fallback only if there are tests proving it is needed
- add examples for BioFire PN, BioFire JI, CAP, antibiotic dosing, periop medications, and calculators

The documentation should explain:
Aliases create evidence.
Protocols claim evidence.
The router picks route/clarify/unsupported.
Protocols produce answers.

Run the full test suite and include a short final summary of changed routing behavior.
```

Definition of done:

- Future protocol authors have clear route-claim instructions.
- Dead or dangerous legacy behavior is removed or fenced.
- The system is easier to extend without modifying central routing code for every new protocol.

## Recommended Overall Order

```text
1. Characterization tests
2. Evidence data structures
3. All alias matches
4. Route claims schema
5. Shadow resolver
6. High-risk gates
7. Expand claims
8. Primary router switch
9. Remove legacy footguns
10. Cleanup docs
```

## Practical Review Checklist For Each Slice

Before accepting any slice, check:

```text
- Did it keep clinical behavior unchanged unless the slice explicitly enables a new safety gate?
- Are unsupported requests blocked rather than answered from general knowledge?
- Are BioFire/PCR/result queries prevented from accidentally routing to CAP?
- Are drug dosing questions still routed to drug dosing protocols?
- Are drug coverage questions prevented from using dosing protocols as coverage sources?
- Are broad syndrome aliases treated as weak evidence?
- Does every clarification ask a narrow, clinically relevant question?
- Do footers and source labels still work?
- Did tests run?
```

