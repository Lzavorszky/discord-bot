"""
Routing, intent classification, tree dispatch, and answer orchestration.

The dataclasses are owned here. Other public functions remain compatibility
wrappers during the split and import ``bot_core`` lazily to avoid cycles.
"""

import importlib
import re
from dataclasses import dataclass, field

import aliases as alias_helpers


@dataclass
class TurnContext:
    raw_user_text: str
    chat_id: object
    active_before: dict = field(default_factory=dict)
    fresh_recognized: dict | None = None
    selected_recognized: dict | None = None
    unsupported_syndrome: str | None = None
    unsupported_matched_term: str | None = None
    unsupported_message: str | None = None
    intent: str = "unknown"
    correction_intent: bool = False
    clear_intent: bool = False
    normalized_question: str = ""
    protocol_slots_before: dict = field(default_factory=dict)
    protocol_slots_after: dict = field(default_factory=dict)
    confirmation_pending: bool = False
    confirmation_required: bool = False


@dataclass
class AnswerEnvelope:
    final_body: str
    final_answer: str
    selected_protocol_id: str | None = None
    selected_protocol_file: str | None = None
    selected_source: str | None = None
    selected_output_key: str | None = None
    selection_mode: str | None = None
    deterministic_or_llm: str = "unknown"
    llm_called: bool = False
    retrieved_chunks: list = field(default_factory=list)
    blocked_reason: str | None = None
    unsupported_action: str | None = None
    unsupported_syndrome: str | None = None
    unsupported_matched_term: str | None = None
    unsupported_message: str | None = None
    trace: dict = field(default_factory=dict)


@dataclass
class RoutingSubject:
    kind: str = "unknown"
    name: str | None = None


@dataclass
class RoutingTest:
    family: str | None = None
    panel: str | None = None


@dataclass
class EvidenceMatch:
    entity_type: str
    value: str
    source: str
    matched_text: str
    confidence: str = "regex"
    score: float | int | None = None
    protocol_file: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class RoutingEvidence:
    intent: str = "unknown"
    subject: RoutingSubject = field(default_factory=RoutingSubject)
    microbes: list[str] = field(default_factory=list)
    markers: list[str] = field(default_factory=list)
    test: RoutingTest = field(default_factory=RoutingTest)
    context: dict = field(default_factory=dict)
    matches: list[EvidenceMatch] = field(default_factory=list)


@dataclass
class RouteDecision:
    kind: str = "fallthrough"
    protocol_file: str | None = None
    message: str | None = None
    reason: str | None = None


_DOSE_INTENT_RE = re.compile(
    r"\b(?:dose|dosing|adag|d[oó]zis|mennyi|how much|adagol[aá]s|"
    r"gfr|egfr|crcl|crrt|ihd|renal|vesefunkci[oó])\b",
    re.IGNORECASE,
)
_PERIOP_INTENT_RE = re.compile(
    r"\b(?:periop(?:erative)?|preop(?:erative)?|before surgery|prior to surgery|"
    r"surgery day|day of surgery|hold before surgery|stop before surgery|"
    r"m[uű]t[eé]t|mutet|perioperat[ií]v|preoperat[ií]v)\b",
    re.IGNORECASE,
)
_CALCULATOR_INTENT_RE = re.compile(
    r"\b(?:calculator|kalkulator|bmi|bsa|ibw|abw|adjbw|"
    r"cardiac output|lvot|ava|eroa?|rvol|pisa)\b",
    re.IGNORECASE,
)
_CONVERSION_INTENT_RE = re.compile(
    r"\b(?:conversion|convert|equivalent|equivalence|equivalency|"
    r"ekvivalens|ekvivalencia|atvaltas|konverzio)\b",
    re.IGNORECASE,
)
_TEST_INTENT_RE = re.compile(
    r"\b(?:biofire|film\s*array|filmarray|pcr|multiplex|panel|"
    r"result|positive|detected|marker)\b",
    re.IGNORECASE,
)
_TARGETED_INTENT_RE = re.compile(
    r"\b(?:targeted|culture[-\s]?directed|susceptib(?:le|ility)|"
    r"cover|coverage|active against|good against|good\s+vs|vs)\b",
    re.IGNORECASE,
)
_EMPIRIC_INTENT_RE = re.compile(
    r"\b(?:empiric|empirical|antibiotic|antibiotics|antimicrobial|"
    r"treatment|therapy|treat|mit adjak|which antibiotic|what.*give)\b",
    re.IGNORECASE,
)
_DIAGNOSIS_INTENT_RE = re.compile(
    r"\b(?:diagnosis|diagnose|diagnostic|diagnozis|diagnosztika)\b",
    re.IGNORECASE,
)
_RENAL_CONTEXT_RE = re.compile(r"\b(?:gfr|egfr|crcl|aki|ckd|renal|vesefunkci[oó])\b", re.IGNORECASE)
_RESULT_CONTEXT_RE = re.compile(r"\b(?:result|positive|detected|culture|marker)\b", re.IGNORECASE)
_PNEUMONIA_PANEL_RE = re.compile(
    r"\b(?:biofire\s+pn|pn\s+(?:panel|pcr)|pneumonia\s+(?:panel|pcr)|"
    r"biofire\s+pneumonia|filmarray\s+pneumonia)\b",
    re.IGNORECASE,
)
_JOINT_PANEL_RE = re.compile(
    r"\b(?:ji\s+panel|joint\s+infection\s+(?:panel|pcr)|biofire\s+ji|"
    r"biofire\s+joint\s+infection)\b",
    re.IGNORECASE,
)
_PCR_FAMILY_RE = re.compile(r"\b(?:biofire|film\s*array|filmarray|pcr|multiplex|panel)\b", re.IGNORECASE)

_MICROBE_PATTERNS = [
    ("Acinetobacter calcoaceticus-baumannii complex", r"\bacinetobacter(?:\s+calcoaceticus[-\s]baumannii\s+complex)?\b"),
    ("Escherichia coli", r"\b(?:e\.?\s*coli|escherichia\s+coli)\b"),
    ("Klebsiella pneumoniae group", r"\bklebsiella(?:\s+pneumoniae(?:\s+group)?|\s+pn)?\b"),
    ("Proteus spp.", r"\bproteus(?:\s+spp\.?)?\b"),
    ("Pseudomonas aeruginosa", r"\bpseudomonas(?:\s+aeruginosa)?\b"),
    ("Staphylococcus aureus", r"\b(?:staph(?:ylococcus)?\s+aureus|s\.?\s*aureus)\b"),
    ("Streptococcus pneumoniae", r"\b(?:strep(?:tococcus)?\s+pneum(?:oniae|o)|s\.?\s*pneumoniae)\b"),
]

_MARKER_PATTERNS = [
    ("mecA/C & MREJ", r"\b(?:meca/c\s*&\s*mrej|meca/c|meca|mrej)\b"),
    ("CTX-M", r"\bctx[-\s]?m\b"),
    ("KPC", r"\bkpc\b"),
    ("NDM", r"\bndm\b"),
    ("OXA-48-like", r"\boxa[-\s]?48(?:[-\s]?like)?\b"),
]

_CALCULATOR_PROTOCOL_IDS = {
    "body_size_calculators",
    "echo_cardiac_output",
    "echo_ava",
    "echo_ero_rvol",
    "steroid_equivalence",
}

_STEROID_DRUG_PATTERNS = [
    ("methylprednisone", r"\bmethylprednis(?:one|olone|on|olon)\b"),
    ("dexamethasone", r"\bdexamethason?e?\b|\bdexa\b"),
    ("hydrocortisone", r"\bhydrocortison?e?\b"),
    ("prednisolone", r"\bprednisolon?e?\b"),
    ("fludrocortisone", r"\bfludrocortison?e?\b"),
]

_CONVERSION_BETWEEN_RE = re.compile(r"\b(?:to|into|->|=|equiv(?:alent|alence)?|conversion|convert)\b", re.IGNORECASE)


def _append_unique(values, value):
    if value not in values:
        values.append(value)


def _classify_routing_intent(question, evidence):
    text = question or ""
    if len(evidence.context.get("steroid_drugs") or []) >= 2 and _CONVERSION_BETWEEN_RE.search(text):
        return "conversion"
    if _CONVERSION_INTENT_RE.search(text):
        return "conversion"
    if _CALCULATOR_INTENT_RE.search(text):
        return "calculator"
    if _PERIOP_INTENT_RE.search(text):
        return "periop_advice"
    if _TEST_INTENT_RE.search(text):
        return "test_interpretation"
    if _DOSE_INTENT_RE.search(text):
        return "dose"
    if _DIAGNOSIS_INTENT_RE.search(text):
        return "diagnosis"
    if _TARGETED_INTENT_RE.search(text):
        return "targeted_treatment"
    if _EMPIRIC_INTENT_RE.search(text):
        if evidence.microbes and not evidence.subject.name:
            return "targeted_treatment"
        return "empiric_treatment"
    return "unknown"


def _subject_kind_for_alias(data):
    if data.get("category") == "drugs":
        return "drug"
    key = data.get("key")
    protocol_file = alias_helpers.normalize_path(data.get("protocol_file", ""))
    if protocol_file.endswith("pneumonia_pcr.txt") or protocol_file.endswith("joint_infection_pcr.txt"):
        return "test_panel"
    if key in _CALCULATOR_PROTOCOL_IDS:
        return "calculator"
    if key == "periop_gyogyszerek":
        return "periop_med"
    if key == "periop_steroids":
        return "steroid"
    return "syndrome"


def _collect_alias_evidence(question, evidence):
    for match in alias_helpers.collect_alias_matches(question):
        kind = _subject_kind_for_alias(match)
        name = match.get("canonical") or match.get("display") or match.get("key")
        evidence.matches.append(EvidenceMatch(
            entity_type="subject",
            value=name,
            source="aliases",
            matched_text=match.get("matched_text") or match.get("alias"),
            confidence=match.get("confidence", "exact"),
            score=match.get("score"),
            protocol_file=match.get("protocol_file"),
            metadata={
                "kind": kind,
                "alias": match.get("alias"),
                "key": match.get("key"),
                "category": match.get("category"),
                "canonical": match.get("canonical"),
                "display": match.get("display"),
                "span": match.get("span"),
                "fuzzy_source": match.get("fuzzy_source"),
            },
        ))
        if evidence.subject.kind == "unknown":
            evidence.subject = RoutingSubject(kind=kind, name=name)


def _collect_pattern_values(question, evidence, patterns, target, source):
    for value, pattern in patterns:
        match = re.search(pattern, question or "", re.IGNORECASE)
        if not match:
            continue
        _append_unique(target, value)
        evidence.matches.append(EvidenceMatch(
            entity_type=source,
            value=value,
            source="regex",
            matched_text=match.group(0),
        ))


def _collect_test_evidence(question, evidence):
    text = question or ""
    panel = None
    panel_match = None
    if _PNEUMONIA_PANEL_RE.search(text):
        panel = "pneumonia"
        panel_match = _PNEUMONIA_PANEL_RE.search(text)
    elif _JOINT_PANEL_RE.search(text):
        panel = "joint_infection"
        panel_match = _JOINT_PANEL_RE.search(text)

    family_match = _PCR_FAMILY_RE.search(text)
    if family_match:
        evidence.test.family = "pcr"
        evidence.matches.append(EvidenceMatch(
            entity_type="test",
            value="pcr",
            source="regex",
            matched_text=family_match.group(0),
            metadata={"field": "family"},
        ))
    if panel:
        evidence.test.panel = panel
        evidence.matches.append(EvidenceMatch(
            entity_type="test",
            value=panel,
            source="regex",
            matched_text=panel_match.group(0),
            metadata={"field": "panel"},
        ))


def _collect_steroid_evidence(question, evidence):
    text = question or ""
    steroid_drugs = []
    for value, pattern in _STEROID_DRUG_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            continue
        _append_unique(steroid_drugs, value)
        evidence.matches.append(EvidenceMatch(
            entity_type="drug",
            value=value,
            source="regex",
            matched_text=match.group(0),
            metadata={"class": "steroid"},
        ))

    if steroid_drugs:
        evidence.context["steroid_drugs"] = steroid_drugs
    if (
        len(steroid_drugs) >= 2
        and _CONVERSION_BETWEEN_RE.search(text)
        and evidence.subject.kind == "unknown"
    ):
        evidence.subject = RoutingSubject(kind="calculator", name="steroid_equivalence")


def _collect_context(question, state, evidence):
    text = question or ""
    if _RENAL_CONTEXT_RE.search(text):
        evidence.context["renal_function"] = True
        evidence.matches.append(EvidenceMatch(
            entity_type="context",
            value="renal_function",
            source="regex",
            matched_text=_RENAL_CONTEXT_RE.search(text).group(0),
        ))
    if _PERIOP_INTENT_RE.search(text):
        evidence.context["perioperative"] = True
        evidence.matches.append(EvidenceMatch(
            entity_type="context",
            value="perioperative",
            source="regex",
            matched_text=_PERIOP_INTENT_RE.search(text).group(0),
        ))
    if _RESULT_CONTEXT_RE.search(text) or _TEST_INTENT_RE.search(text):
        evidence.context["test_or_result"] = True
        match = _RESULT_CONTEXT_RE.search(text) or _TEST_INTENT_RE.search(text)
        evidence.matches.append(EvidenceMatch(
            entity_type="context",
            value="test_or_result",
            source="regex",
            matched_text=match.group(0),
        ))
    if state and isinstance(state, dict):
        active = state.get("active_recognized")
        if isinstance(active, dict) and active.get("protocol_file"):
            evidence.context["active_protocol_file"] = active.get("protocol_file")
            evidence.matches.append(EvidenceMatch(
                entity_type="context",
                value="active_protocol_file",
                source="state",
                matched_text=active.get("protocol_file"),
            ))
    unsupported = alias_helpers._detect_unsupported_policy(question)
    if unsupported:
        evidence.context["unsupported_syndrome"] = unsupported.get("key")
        evidence.context["unsupported_message"] = unsupported.get("message")
        evidence.context["unsupported_allowed_if_explicit_drug"] = unsupported.get(
            "allowed_if_explicit_drug", True
        )
        evidence.matches.append(EvidenceMatch(
            entity_type="unsupported_syndrome",
            value=unsupported.get("key") or "",
            source="aliases",
            matched_text=unsupported.get("matched_term") or "",
            metadata={
                "message": unsupported.get("message"),
                "allowed_if_explicit_drug": unsupported.get("allowed_if_explicit_drug", True),
            },
        ))


def extract_routing_evidence(question, state=None):
    evidence = RoutingEvidence()
    _collect_alias_evidence(question, evidence)
    _collect_pattern_values(question, evidence, _MICROBE_PATTERNS, evidence.microbes, "microbe")
    _collect_pattern_values(question, evidence, _MARKER_PATTERNS, evidence.markers, "marker")
    _collect_test_evidence(question, evidence)
    _collect_steroid_evidence(question, evidence)
    _collect_context(question, state, evidence)
    evidence.intent = _classify_routing_intent(question, evidence)
    evidence.matches.append(EvidenceMatch(
        entity_type="intent",
        value=evidence.intent,
        source="regex",
        matched_text=question or "",
    ))
    return evidence


def _norm_token(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def _norm_set(values):
    if not values:
        return set()
    return {_norm_token(value) for value in values if str(value or "").strip()}


def _claim_records(protocol_claims):
    records = []
    for key, value in (protocol_claims or {}).items():
        parsed = value if isinstance(value, dict) and "route_claims" in value else None
        claims = (value.get("route_claims") if parsed else value) or {}
        if not isinstance(claims, dict) or not claims:
            continue
        protocol_file = key
        if parsed:
            protocol_file = parsed.get("path") or key
        records.append({
            "protocol_file": alias_helpers.normalize_path(protocol_file),
            "claims": claims,
            "parsed": parsed or {},
            "intents": _norm_set(claims.get("intents")),
            "subjects": _norm_set(claims.get("subjects")),
        })
    return records


def _claim_owns(record, key):
    owns = (record.get("claims") or {}).get("owns") or {}
    value = owns.get(key) or owns.get(key.lower()) or []
    return value if isinstance(value, list) else []


def _claim_owns_source(record, key, source):
    owns = (record.get("claims") or {}).get("owns") or {}
    value = owns.get(key) or owns.get(key.lower())
    return isinstance(value, dict) and value.get("source") == source


def _record_owns_any(record, key, terms):
    return bool(_norm_set(_claim_owns(record, key)) & _norm_set(terms))


def _subject_terms(evidence, kind=None):
    terms = []
    if evidence.subject.name and (kind is None or evidence.subject.kind == kind):
        terms.append(evidence.subject.name)
    for match in evidence.matches:
        if match.entity_type != "subject":
            continue
        if kind is not None and match.metadata.get("kind") != kind:
            continue
        terms.extend([
            match.value,
            match.metadata.get("alias"),
            match.metadata.get("key"),
            match.metadata.get("canonical"),
            match.metadata.get("display"),
            match.matched_text,
        ])
    return [term for term in terms if term]


def _matched_protocol_files(evidence, kind=None):
    files = []
    for match in evidence.matches:
        if match.entity_type != "subject" or not match.protocol_file:
            continue
        if kind is not None and match.metadata.get("kind") != kind:
            continue
        normalized = alias_helpers.normalize_path(match.protocol_file)
        if normalized not in files:
            files.append(normalized)
    return files


def _same_protocol_path(left, right):
    left_norm = alias_helpers.normalize_path(left)
    right_norm = alias_helpers.normalize_path(right)
    return (
        left_norm == right_norm
        or left_norm.endswith("/" + right_norm)
        or right_norm.endswith("/" + left_norm)
    )


def _protocol_file_in(protocol_file, candidates):
    return any(_same_protocol_path(protocol_file, candidate) for candidate in candidates)


def _drug_terms(evidence):
    terms = _subject_terms(evidence, "drug")
    for match in evidence.matches:
        if match.entity_type == "drug":
            terms.extend([match.value, match.matched_text])
    for drug in evidence.context.get("steroid_drugs") or []:
        terms.append(drug)
    return [term for term in terms if term]


def _syndrome_terms(evidence):
    return _subject_terms(evidence, "syndrome")


def _has_syndrome(evidence):
    return bool(_syndrome_terms(evidence))


def _has_drug(evidence):
    return bool(_drug_terms(evidence))


def _has_microbe_or_marker(evidence):
    return bool(evidence.microbes or evidence.markers)


def _is_pcr_record(record):
    claims = record.get("claims") or {}
    owns = claims.get("owns") or {}
    return (
        "test_interpretation" in record.get("intents", set())
        and (
            _norm_set(owns.get("tests") if isinstance(owns.get("tests"), list) else [])
            & {"pcr", "biofire", "filmarray"}
        )
    )


def _record_contains_microbe_or_marker(record, evidence):
    parsed = record.get("parsed") or {}
    if evidence.microbes:
        if _claim_owns_source(record, "microbes", "pcr_organism_aliases"):
            organisms = parsed.get("pcr_organism_aliases") or {}
            organism_terms = set()
            for canonical, aliases in organisms.items():
                organism_terms.add(canonical)
                organism_terms.update(aliases or [])
            if _norm_set(organism_terms) & _norm_set(evidence.microbes):
                return True
        if _record_owns_any(record, "microbes", evidence.microbes):
            return True
    if evidence.markers:
        if _claim_owns_source(record, "markers", "pcr_resistance_marker_aliases"):
            markers = parsed.get("pcr_resistance_marker_aliases") or {}
            marker_terms = set()
            for canonical, aliases in markers.items():
                marker_terms.add(canonical)
                marker_terms.update(aliases or [])
            if _norm_set(marker_terms) & _norm_set(evidence.markers):
                return True
        if _record_owns_any(record, "markers", evidence.markers):
            return True
    return False


def _pcr_records(records):
    return [record for record in records if _is_pcr_record(record)]


def _pcr_records_for_panel(records, panel):
    panel_terms = [panel]
    if panel == "pneumonia":
        panel_terms.extend(["pn", "pneumonia"])
    if panel == "joint_infection":
        panel_terms.extend(["ji", "joint_infection"])
    return [
        record for record in _pcr_records(records)
        if _record_owns_any(record, "panels", panel_terms)
    ]


def _pcr_records_for_evidence(records, evidence):
    return [
        record for record in _pcr_records(records)
        if _record_contains_microbe_or_marker(record, evidence)
    ]


def _route(record, reason):
    return RouteDecision(
        kind="route",
        protocol_file=record["protocol_file"],
        reason=reason,
    )


def _clarify(message, reason):
    return RouteDecision(kind="clarify", message=message, reason=reason)


def _unsupported(message, reason):
    return RouteDecision(kind="unsupported", message=message, reason=reason)


def _first_claiming(records, intent, owner_key=None, terms=None, subject=None):
    intent_key = _norm_token(intent)
    for record in records:
        if intent_key not in record["intents"]:
            continue
        if subject and _norm_token(subject) not in record["subjects"]:
            continue
        if owner_key and terms and not _record_owns_any(record, owner_key, terms):
            continue
        return record
    return None


def _explicit_targeted_claim(records, evidence):
    drug_terms = _drug_terms(evidence)
    syndrome_terms = _syndrome_terms(evidence)
    microbe_terms = list(evidence.microbes)
    for record in records:
        if not ({"targeted_treatment", "coverage_question"} & record["intents"]):
            continue
        if drug_terms and not _record_owns_any(record, "drugs", drug_terms):
            continue
        if syndrome_terms and not _record_owns_any(record, "syndromes", syndrome_terms):
            continue
        if microbe_terms and not _record_owns_any(record, "microbes", microbe_terms):
            continue
        return record
    return None


def _panel_clarification(records):
    labels = []
    for record in records:
        panels = _claim_owns(record, "panels")
        if panels:
            labels.append("/".join(panels))
        else:
            labels.append(record["protocol_file"])
    suffix = ": " + ", ".join(labels) if labels else ""
    return "Which PCR/BioFire panel is this result from" + suffix + "?"


def route_decision_to_dict(decision):
    if not decision:
        return None
    return {
        "kind": decision.kind,
        "protocol_file": decision.protocol_file,
        "message": decision.message,
        "reason": decision.reason,
    }


def route_candidates_for_evidence(evidence, protocol_claims):
    if not evidence:
        return []
    records = _claim_records(protocol_claims)
    candidate_intents = {_norm_token(evidence.intent)}
    if evidence.intent == "periop_advice":
        candidate_intents.update({
            "perioperative_medication_management",
            "perioperative_steroid_management",
        })
    if evidence.intent in {"conversion", "calculator"}:
        candidate_intents.update({"dose_conversion", "calculator", "conversion"})

    summaries = []
    for record in records:
        record_intents = record.get("intents", set())
        if candidate_intents and not (record_intents & candidate_intents):
            continue
        reasons = sorted(record_intents & candidate_intents)
        if _has_drug(evidence) and _record_owns_any(record, "drugs", _drug_terms(evidence)):
            reasons.append("drug_match")
        if _has_syndrome(evidence) and _record_owns_any(record, "syndromes", _syndrome_terms(evidence)):
            reasons.append("syndrome_match")
        if _has_microbe_or_marker(evidence) and _record_contains_microbe_or_marker(record, evidence):
            reasons.append("microbe_or_marker_match")
        if evidence.test.panel and _record_owns_any(record, "panels", [evidence.test.panel]):
            reasons.append("panel_match")
        if evidence.subject.kind == "calculator" and _record_owns_any(
            record, "calculators", _subject_terms(evidence, "calculator")
        ):
            reasons.append("calculator_match")
        parsed = record.get("parsed") or {}
        meta = parsed.get("metadata", {}) if parsed else {}
        summaries.append({
            "protocol_file": record["protocol_file"],
            "protocol_id": meta.get("protocol_id"),
            "intents": sorted(record_intents),
            "subjects": sorted(record.get("subjects", set())),
            "reasons": sorted(set(reasons)),
        })
    return summaries


def resolve_route(evidence, protocol_claims, state=None):
    """Resolve typed routing evidence against protocol-owned route claims.

    This is deliberately shadow-mode safe: it returns a decision object but
    does not mutate state, activate a protocol, or generate an answer.
    """
    records = _claim_records(protocol_claims)
    if not evidence:
        return RouteDecision(kind="fallthrough", reason="no evidence")

    unsupported_key = evidence.context.get("unsupported_syndrome")
    explicit_drug_dose = _has_drug(evidence) and evidence.intent == "dose"
    if unsupported_key and not explicit_drug_dose:
        return _unsupported(
            evidence.context.get("unsupported_message")
            or f"No uploaded protocol supports {unsupported_key}.",
            "unsupported syndrome policy matched",
        )

    if _has_drug(evidence) and evidence.intent == "dose":
        record = _first_claiming(records, "dose", "drugs", _drug_terms(evidence), "drug")
        if record:
            return _route(record, "drug plus dose intent claimed by dosing protocol")
        return _unsupported(
            "No uploaded dosing protocol explicitly claims that drug.",
            "drug dose request without matching dose claim",
        )

    if _has_drug(evidence) and evidence.intent == "targeted_treatment" and (
        _has_microbe_or_marker(evidence) or _has_syndrome(evidence)
    ):
        record = _explicit_targeted_claim(records, evidence)
        if record:
            return _route(record, "drug coverage question explicitly claimed")
        if evidence.test.family == "pcr" or evidence.context.get("test_or_result"):
            return _clarify(
                "Is this a PCR/BioFire result interpretation question? If yes, name the panel.",
                "drug coverage question overlaps test/result context",
            )
        return _unsupported(
            "No uploaded protocol explicitly claims drug coverage or targeted-treatment advice for that organism/syndrome.",
            "dosing protocols cannot answer coverage questions",
        )

    if evidence.test.family == "pcr" and _has_microbe_or_marker(evidence):
        panel_records = _pcr_records_for_panel(records, evidence.test.panel) if evidence.test.panel else []
        if panel_records:
            matching = [record for record in panel_records if _record_contains_microbe_or_marker(record, evidence)]
            if len(matching) == 1:
                return _route(matching[0], "PCR/result question with known panel and matching organism/marker")
            if len(matching) > 1:
                return _clarify(_panel_clarification(matching), "known panel matched multiple PCR claims")
            return _unsupported(
                "That organism/marker is not claimed by the specified PCR panel protocol.",
                "known PCR panel did not claim organism/marker",
            )

        alias_panel_files = _matched_protocol_files(evidence, "test_panel")
        alias_panel_records = [
            record for record in _pcr_records(records)
            if _protocol_file_in(record["protocol_file"], alias_panel_files)
            and _record_contains_microbe_or_marker(record, evidence)
        ]
        if len(alias_panel_records) == 1:
            return _route(alias_panel_records[0], "PCR/result question with panel inferred from alias evidence")
        if len(alias_panel_records) > 1:
            return _clarify(_panel_clarification(alias_panel_records), "alias panel evidence matched multiple PCR claims")

        matching = _pcr_records_for_evidence(records, evidence)
        if len(matching) == 1:
            return _clarify(
                _panel_clarification(matching),
                "PCR/result question has organism/marker but no explicit panel/source",
            )
        if len(matching) > 1:
            return _clarify(_panel_clarification(matching), "PCR organism/marker appears on multiple panels")
        return _unsupported(
            "No uploaded PCR/BioFire protocol claims that organism or marker.",
            "PCR/result question without matching PCR claim",
        )

    if _has_microbe_or_marker(evidence) and _has_syndrome(evidence):
        record = _explicit_targeted_claim(records, evidence)
        if record:
            return _route(record, "microbe plus syndrome explicitly claimed by targeted protocol")
        matching = _pcr_records_for_evidence(records, evidence)
        if matching:
            return _clarify(
                "Is this a PCR/BioFire result? If yes, name the panel.",
                "microbe plus syndrome could be PCR evidence but is not targeted syndrome claim",
            )
        return _unsupported(
            "No uploaded protocol explicitly claims targeted treatment for that organism and syndrome.",
            "microbe plus syndrome without targeted protocol claim",
        )

    if _has_syndrome(evidence) and evidence.intent == "empiric_treatment":
        record = _first_claiming(records, "empiric_treatment", "syndromes", _syndrome_terms(evidence), "syndrome")
        if record:
            return _route(record, "syndrome plus empiric intent claimed")
        return _unsupported(
            "No uploaded syndrome protocol claims empiric treatment for that syndrome.",
            "empiric syndrome request without matching claim",
        )

    if (
        _has_syndrome(evidence)
        and evidence.intent == "unknown"
        and not _has_microbe_or_marker(evidence)
        and not _has_drug(evidence)
        and not evidence.test.family
        and not evidence.context.get("test_or_result")
    ):
        record = _first_claiming(records, "empiric_treatment", "syndromes", _syndrome_terms(evidence), "syndrome")
        if record:
            return _route(record, "weak syndrome evidence claimed as fallback")

    if _has_syndrome(evidence) and evidence.intent == "diagnosis":
        record = _first_claiming(records, "diagnosis", "syndromes", _syndrome_terms(evidence), "syndrome")
        if record:
            return _route(record, "syndrome plus diagnosis intent claimed")
        return _unsupported(
            "No uploaded diagnostic protocol claims diagnosis for that syndrome.",
            "diagnosis request without matching syndrome claim",
        )

    if evidence.intent == "periop_advice" or evidence.context.get("perioperative"):
        steroid_terms = evidence.context.get("steroid_drugs") or []
        if steroid_terms:
            record = _first_claiming(records, "perioperative_steroid_management")
            if record:
                return _route(record, "perioperative steroid question claimed")
        record = _first_claiming(records, "perioperative_medication_management")
        if record:
            return _route(record, "perioperative medication context claimed")

    if evidence.intent in {"conversion", "calculator"}:
        steroid_terms = evidence.context.get("steroid_drugs") or []
        if steroid_terms:
            record = _first_claiming(records, "dose_conversion", "drugs", steroid_terms, "drug")
            if record:
                return _route(record, "steroid conversion claimed by calculator protocol")
        calculator_terms = _subject_terms(evidence, "calculator")
        if calculator_terms:
            for intent in ("dose_conversion", "calculator", "conversion"):
                record = _first_claiming(records, intent, "calculators", calculator_terms, "calculator")
                if record:
                    return _route(record, "calculator/conversion intent claimed by named calculator protocol")

    if state and isinstance(state, dict) and state.get("active_recognized"):
        return RouteDecision(kind="use_active_context", reason="no shadow route; active context exists")
    return RouteDecision(kind="fallthrough", reason="no route claim matched")


def _core():
    return importlib.import_module("bot_core")


def classify_intent(question: str) -> str:
    return _core().classify_intent(question)


def dispatch_tree(state, recognized, raw_question, normalized_question):
    return _core().dispatch_tree(state, recognized, raw_question, normalized_question)


def ask_ai(question, chat_id):
    return _core().ask_ai(question, chat_id)


def build_debug_trace(debug_question, chat_id):
    return _core().build_debug_trace(debug_question, chat_id)


def format_debug_output(retrieved_chunks):
    return _core().format_debug_output(retrieved_chunks)


def format_protocols_output():
    return _core().format_protocols_output()


def format_version_output():
    return _core().format_version_output()


def get_protocol_library_version():
    return _core().get_protocol_library_version()


def _build_drug_name_set():
    return _core()._build_drug_name_set()


def _handle_dosing_shortcut(state: dict, question: str, recognized):
    return _core()._handle_dosing_shortcut(state, question, recognized)


def _handle_organism_disambiguation(state: dict, question: str, recognized):
    return _core()._handle_organism_disambiguation(state, question, recognized)


def _update_routing_state(state: dict, recognized, context_source: str):
    return _core()._update_routing_state(state, recognized, context_source)


def _update_recommended_antibiotics(state: dict, response_text: str, recognized):
    return _core()._update_recommended_antibiotics(state, response_text, recognized)


def _try_deterministic_selection(state, recognized, question, lang):
    return _core()._try_deterministic_selection(state, recognized, question, lang)


__all__ = [
    "TurnContext",
    "AnswerEnvelope",
    "RoutingEvidence",
    "RoutingSubject",
    "RoutingTest",
    "RouteDecision",
    "EvidenceMatch",
    "extract_routing_evidence",
    "resolve_route",
    "route_decision_to_dict",
    "route_candidates_for_evidence",
    "classify_intent",
    "dispatch_tree",
    "ask_ai",
    "build_debug_trace",
    "format_debug_output",
    "format_protocols_output",
    "format_version_output",
    "get_protocol_library_version",
    "_build_drug_name_set",
    "_handle_dosing_shortcut",
    "_handle_organism_disambiguation",
    "_update_routing_state",
    "_update_recommended_antibiotics",
    "_try_deterministic_selection",
]
