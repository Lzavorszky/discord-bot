"""
Alias loading, query normalization, and unsupported-syndrome detection.

``bot_core`` still exposes the historical names, but the matching policy lives
here so future protocol additions do not need to touch routing internals.
"""

import json
import os
import re
from pathlib import Path

try:
    from rapidfuzz import fuzz, process

    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False
    fuzz = None
    process = None


ALIASES = {}
ALIAS_INDEX = {}
BLOCKED_ALIASES = set()
UNSUPPORTED_SYNDROMES = {}
PROTOCOL_FILE_TO_LABEL = {}

_HIGH_CONFIDENCE_FUZZY_SCORE = 90
_MEDIUM_CONFIDENCE_FUZZY_SCORE = 84
_BLOCKED_FUZZY_SCORE = 90
_MIN_FUZZY_ALIAS_CHARS = 5

_CLINICAL_INTENT_TERMS = {
    "ab",
    "abx",
    "antibiotic",
    "antibiotics",
    "antimicrobial",
    "bacteremia",
    "bsi",
    "cap",
    "crcl",
    "creatinine",
    "culture",
    "dose",
    "dosing",
    "egfr",
    "gfr",
    "infection",
    "infusion",
    "iv",
    "outpatient",
    "panel",
    "pathogen",
    "pcr",
    "pneumonia",
    "positive",
    "renal",
    "resistance",
    "result",
    "sepsis",
    "susceptible",
    "susceptibility",
    "tdm",
    "therapy",
    "treatment",
}

_BLOCKED_DISTINCTIVE_TERMS = {
    "hap",
    "vap",
    "hospital",
    "ventilator",
    "nosocomial",
    "nozokomialis",
    "immunosuppressed",
    "immunocompromised",
    "joint",
    "ji",
}

_CLINICAL_STRUCTURE_RE = re.compile(
    r"\b(?:gfr|egfr|crcl|aki|ckd|icu|iv|po|tdm|ctx-m|oxa|mec-a|mrsa|vre)\b"
    r"|\b\d+(?:\.\d+)?\s*(?:mg|g|kg|ml/min|ml\/min|hours?|h)\b"
    r"|\bq\d{1,2}h\b",
    re.IGNORECASE,
)


def normalize_path(path):
    return str(Path(path)).replace("\\", "/").lower()


def load_aliases(path="protocols/aliases.json"):
    global ALIASES, ALIAS_INDEX, BLOCKED_ALIASES, UNSUPPORTED_SYNDROMES, PROTOCOL_FILE_TO_LABEL
    if not os.path.exists(path):
        print("No aliases.json found. Alias recognition disabled.")
        ALIASES = {}
        ALIAS_INDEX = {}
        BLOCKED_ALIASES = set()
        UNSUPPORTED_SYNDROMES = {}
        PROTOCOL_FILE_TO_LABEL = {}
        return
    with open(path, "r", encoding="utf-8") as f:
        ALIASES = json.load(f)
    (
        ALIAS_INDEX,
        BLOCKED_ALIASES,
        UNSUPPORTED_SYNDROMES,
        PROTOCOL_FILE_TO_LABEL,
    ) = _build_alias_index(ALIASES)
    print(f"Loaded {len(ALIAS_INDEX)} aliases")
    if not RAPIDFUZZ_AVAILABLE:
        print("rapidfuzz not installed - fuzzy matching disabled, exact matching only.")


def _build_alias_index(alias_data, normalize_path_fn=normalize_path):
    alias_index = {}
    weak_aliases = {
        term.lower()
        for term in alias_data.get("weak_aliases", [])
        if isinstance(term, str) and term.strip()
    }
    blocked_aliases = {
        term.lower()
        for term in alias_data.get("blocked_aliases", [])
        if isinstance(term, str) and term.strip()
    }
    unsupported_syndromes = _build_unsupported_syndromes(alias_data)
    protocol_file_to_label = {}
    for category in ["drugs", "conditions"]:
        for key, item in alias_data.get(category, {}).items():
            display = item.get("display", key)
            canonical = item.get("canonical", display)
            source_label = item.get("source_label", display)
            protocol_file = item.get("protocol_file", "")
            data = {
                "key": key,
                "category": category,
                "display": display,
                "canonical": canonical,
                "source_label": source_label,
                "protocol_file": protocol_file,
            }
            if protocol_file:
                protocol_file_to_label[normalize_path_fn(protocol_file)] = source_label
            terms = [display, canonical] + item.get("aliases", [])
            for term in terms:
                if term:
                    indexed = dict(data)
                    if term.lower() in weak_aliases:
                        indexed["routing_strength"] = "weak"
                    alias_index[term.lower()] = indexed
    return alias_index, blocked_aliases, unsupported_syndromes, protocol_file_to_label


def _default_unsupported_message(label):
    return (
        f"No uploaded protocol supports {label.upper()} antibiotic selection. "
        "I cannot recommend antibiotics for that unsupported syndrome from the uploaded protocols. "
        "Please provide an explicit supported drug/protocol, or use local ID/pharmacy review."
    )


def _build_unsupported_syndromes(alias_data):
    policies = {}
    raw_policies = alias_data.get("unsupported_syndromes", {})
    if not isinstance(raw_policies, dict):
        return policies

    for key, entry in raw_policies.items():
        if not isinstance(entry, dict):
            continue
        policy_key = str(key).strip().lower()
        if not policy_key:
            continue
        terms = []
        for term in entry.get("terms", []):
            if isinstance(term, str) and term.strip():
                lowered = term.strip().lower()
                if lowered not in terms:
                    terms.append(lowered)
        if not terms:
            continue
        message = entry.get("message")
        if not isinstance(message, str) or not message.strip():
            message = _default_unsupported_message(policy_key)
        policies[policy_key] = {
            "key": policy_key,
            "terms": terms,
            "message": message.strip(),
            "allowed_if_explicit_drug": bool(entry.get("allowed_if_explicit_drug", True)),
            "allow_supported_alias_collision": bool(entry.get("allow_supported_alias_collision", False)),
        }
    return policies


def _alias_term_matches(term, text):
    return re.search(r"\b" + re.escape(term) + r"\b", text) is not None


def _tokens(text):
    return re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)?", (text or "").lower())


def _compact_len(text):
    return len(re.sub(r"[^a-z0-9]+", "", (text or "").lower()))


def _levenshtein_distance(left, right, max_distance=None):
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    if max_distance is not None and abs(len(left) - len(right)) > max_distance:
        return max_distance + 1

    previous = list(range(len(right) + 1))
    for i, lc in enumerate(left, 1):
        current = [i]
        row_min = current[0]
        for j, rc in enumerate(right, 1):
            cost = 0 if lc == rc else 1
            value = min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + cost,
            )
            current.append(value)
            row_min = min(row_min, value)
        if max_distance is not None and row_min > max_distance:
            return max_distance + 1
        previous = current
    return previous[-1]


def _max_plausible_edit_distance(term):
    if len(term) <= 6:
        return 1
    if len(term) <= 10:
        return 2
    return 3


def _has_medically_plausible_edit(query_tokens, alias_tokens):
    for query_token in query_tokens:
        if len(query_token) < _MIN_FUZZY_ALIAS_CHARS:
            continue
        for alias_token in alias_tokens:
            if len(alias_token) < _MIN_FUZZY_ALIAS_CHARS:
                continue
            max_distance = _max_plausible_edit_distance(alias_token)
            if _levenshtein_distance(query_token, alias_token, max_distance) <= max_distance:
                return True
    return False


def _has_clinical_intent_or_structure(question):
    if _is_obvious_nonclinical_message(question):
        return False
    text = question or ""
    query_tokens = set(_tokens(text))
    if query_tokens & _CLINICAL_INTENT_TERMS:
        return True
    if _CLINICAL_STRUCTURE_RE.search(text):
        return True
    clinical_like_count = sum(1 for token in query_tokens if token in _CLINICAL_INTENT_TERMS)
    return clinical_like_count >= 2


def _fuzzy_candidate_quality_ok(question, alias):
    if _compact_len(alias) < _MIN_FUZZY_ALIAS_CHARS:
        return False
    query_tokens = set(_tokens(question))
    alias_tokens = set(_tokens(alias))
    if not query_tokens or not alias_tokens:
        return False
    if query_tokens & alias_tokens:
        return True
    return _has_medically_plausible_edit(query_tokens, alias_tokens)


def _fuzzy_blocked_quality_ok(question, alias):
    query_tokens = set(_tokens(question))
    alias_tokens = set(_tokens(alias))
    distinctive_alias_tokens = alias_tokens & _BLOCKED_DISTINCTIVE_TERMS
    if not distinctive_alias_tokens:
        return _fuzzy_candidate_quality_ok(question, alias)
    if query_tokens & distinctive_alias_tokens:
        return True
    return _has_medically_plausible_edit(query_tokens, distinctive_alias_tokens)


def _extract_best_fuzzy_choice(text, choices, *, process_module, fuzz_module):
    best = {"alias": None, "score": 0, "source": ""}
    if not choices:
        return best

    for word in re.findall(r"\w+", text):
        if len(word) < _MIN_FUZZY_ALIAS_CHARS:
            continue
        match = process_module.extractOne(word, choices, scorer=fuzz_module.WRatio)
        if match and match[1] > best["score"]:
            best = {"alias": match[0], "score": match[1], "source": f"word:{word}"}

    meaningful_text = re.sub(r"\W+", "", text)
    if len(meaningful_text) >= _MIN_FUZZY_ALIAS_CHARS:
        match = process_module.extractOne(text, choices, scorer=fuzz_module.partial_ratio)
        if match and match[1] > best["score"]:
            best = {"alias": match[0], "score": match[1], "source": "partial_ratio"}

    return best


def _detect_fuzzy_blocked_alias(
    question,
    *,
    blocked_aliases=None,
    rapidfuzz_available=None,
    process_module=None,
    fuzz_module=None,
):
    active_blocked_aliases = BLOCKED_ALIASES if blocked_aliases is None else blocked_aliases
    fuzzy_enabled = RAPIDFUZZ_AVAILABLE if rapidfuzz_available is None else rapidfuzz_available
    active_process = process if process_module is None else process_module
    active_fuzz = fuzz if fuzz_module is None else fuzz_module
    text = (question or "").lower().strip()

    if not fuzzy_enabled or active_process is None or active_fuzz is None:
        return None
    if not _has_clinical_intent_or_structure(text):
        return None

    blocked_keys = [
        alias
        for alias in active_blocked_aliases
        if _compact_len(alias) >= _MIN_FUZZY_ALIAS_CHARS
    ]
    best = _extract_best_fuzzy_choice(
        text,
        blocked_keys,
        process_module=active_process,
        fuzz_module=active_fuzz,
    )
    alias = best["alias"]
    if (
        alias
        and best["score"] >= _BLOCKED_FUZZY_SCORE
        and _fuzzy_blocked_quality_ok(text, alias)
    ):
        return alias
    return None


def _alias_match_payload(alias, data, confidence, score, *, matched_text=None, span=None, source=None):
    match = {
        **data,
        "alias": alias,
        "matched_alias": alias,
        "confidence": confidence,
        "score": score,
        "matched_text": matched_text or alias,
    }
    if span is not None:
        match["span"] = span
    if source:
        match["fuzzy_source"] = source
    return match


def _exact_alias_span(alias, text):
    match = re.search(r"\b" + re.escape(alias) + r"\b", text)
    if not match:
        return None
    return match.span()


def _fuzzy_alias_score(text, alias, *, fuzz_module):
    best = {"score": 0, "source": ""}
    for word in re.findall(r"\w+", text):
        if len(word) < _MIN_FUZZY_ALIAS_CHARS:
            continue
        score = fuzz_module.WRatio(word, alias)
        if score > best["score"]:
            best = {"score": score, "source": f"word:{word}"}

    meaningful_text = re.sub(r"\W+", "", text)
    if len(meaningful_text) >= _MIN_FUZZY_ALIAS_CHARS:
        score = fuzz_module.partial_ratio(text, alias)
        if score > best["score"]:
            best = {"score": score, "source": "partial_ratio"}
    return best


def collect_alias_matches(
    question,
    *,
    alias_index=None,
    rapidfuzz_available=None,
    process_module=None,
    fuzz_module=None,
):
    active_alias_index = ALIAS_INDEX if alias_index is None else alias_index
    fuzzy_enabled = RAPIDFUZZ_AVAILABLE if rapidfuzz_available is None else rapidfuzz_available
    active_fuzz = fuzz if fuzz_module is None else fuzz_module

    text = (question or "").lower().strip()
    if not text or not active_alias_index:
        return []

    matches = []
    exact_aliases = set()
    exact_keys = set()
    for alias in sorted(active_alias_index.keys(), key=len, reverse=True):
        span = _exact_alias_span(alias, text)
        if span is None:
            continue
        data = active_alias_index[alias]
        exact_aliases.add(alias)
        exact_keys.add((data.get("category"), data.get("key")))
        matches.append(_alias_match_payload(
            alias,
            data,
            "exact",
            100,
            matched_text=text[span[0]:span[1]],
            span=span,
        ))

    if (
        not fuzzy_enabled
        or active_fuzz is None
        or not _has_clinical_intent_or_structure(text)
    ):
        return matches

    fuzzy_by_key = {}
    for alias in sorted(active_alias_index.keys(), key=len, reverse=True):
        if alias in exact_aliases or _compact_len(alias) < _MIN_FUZZY_ALIAS_CHARS:
            continue
        data = active_alias_index[alias]
        key = (data.get("category"), data.get("key"))
        if key in exact_keys:
            continue
        scored = _fuzzy_alias_score(text, alias, fuzz_module=active_fuzz)
        score = scored["score"]
        if score < _HIGH_CONFIDENCE_FUZZY_SCORE:
            continue
        if not _fuzzy_candidate_quality_ok(text, alias):
            continue
        existing = fuzzy_by_key.get(key)
        if existing and (
            existing["score"] > score
            or (existing["score"] == score and len(existing["alias"]) >= len(alias))
        ):
            continue
        fuzzy_by_key[key] = _alias_match_payload(
            alias,
            data,
            "high",
            score,
            matched_text=alias,
            source=scored["source"],
        )
    matches.extend(sorted(
        fuzzy_by_key.values(),
        key=lambda match: (match["score"], len(match["alias"])),
        reverse=True,
    ))
    return matches


def _iter_unsupported_terms(unsupported_policies=None, blocked_aliases=None):
    active_policies = UNSUPPORTED_SYNDROMES if unsupported_policies is None else unsupported_policies
    active_blocked_aliases = BLOCKED_ALIASES if blocked_aliases is None else blocked_aliases
    seen = set()

    for key, policy in (active_policies or {}).items():
        if not isinstance(policy, dict):
            continue
        for term in policy.get("terms", []):
            if not isinstance(term, str) or not term.strip():
                continue
            lowered = term.strip().lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            yield lowered, policy

    for term in active_blocked_aliases or []:
        if not isinstance(term, str) or not term.strip():
            continue
        lowered = term.strip().lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        yield lowered, None


def _legacy_unsupported_policy(term):
    return {
        "key": term,
        "terms": [term],
        "message": _default_unsupported_message(term),
        "allowed_if_explicit_drug": True,
        "allow_supported_alias_collision": False,
    }


def _policy_hit(policy, matched_term):
    active_policy = policy or _legacy_unsupported_policy(matched_term)
    key = str(active_policy.get("key") or matched_term).strip().lower()
    message = active_policy.get("message")
    if not isinstance(message, str) or not message.strip():
        message = _default_unsupported_message(key)
    return {
        "key": key,
        "matched_term": matched_term,
        "message": message.strip(),
        "allowed_if_explicit_drug": bool(active_policy.get("allowed_if_explicit_drug", True)),
    }


def _detect_unsupported_policy(
    question,
    *,
    unsupported_policies=None,
    blocked_aliases=None,
    rapidfuzz_available=None,
    process_module=None,
    fuzz_module=None,
):
    text = (question or "").lower().strip()
    terms = list(_iter_unsupported_terms(unsupported_policies, blocked_aliases))

    for term, policy in sorted(terms, key=lambda item: len(item[0]), reverse=True):
        if _alias_term_matches(term, text):
            return _policy_hit(policy, term)

    fuzzy_enabled = RAPIDFUZZ_AVAILABLE if rapidfuzz_available is None else rapidfuzz_available
    active_process = process if process_module is None else process_module
    active_fuzz = fuzz if fuzz_module is None else fuzz_module
    if not fuzzy_enabled or active_process is None or active_fuzz is None:
        return None
    if not _has_clinical_intent_or_structure(text):
        return None

    candidates = [
        term for term, _policy in terms
        if _compact_len(term) >= _MIN_FUZZY_ALIAS_CHARS
    ]
    best = _extract_best_fuzzy_choice(
        text,
        candidates,
        process_module=active_process,
        fuzz_module=active_fuzz,
    )
    matched = best["alias"]
    if (
        matched
        and best["score"] >= _BLOCKED_FUZZY_SCORE
        and _fuzzy_blocked_quality_ok(text, matched)
    ):
        for term, policy in terms:
            if term == matched:
                return _policy_hit(policy, matched)
    return None


def normalize_question(
    question,
    *,
    alias_index=None,
    blocked_aliases=None,
    unsupported_policies=None,
    rapidfuzz_available=None,
    process_module=None,
    fuzz_module=None,
):
    active_alias_index = ALIAS_INDEX if alias_index is None else alias_index
    active_blocked_aliases = BLOCKED_ALIASES if blocked_aliases is None else blocked_aliases
    active_unsupported_policies = (
        UNSUPPORTED_SYNDROMES if unsupported_policies is None else unsupported_policies
    )
    fuzzy_enabled = RAPIDFUZZ_AVAILABLE if rapidfuzz_available is None else rapidfuzz_available
    active_process = process if process_module is None else process_module
    active_fuzz = fuzz if fuzz_module is None else fuzz_module

    text = question.lower().strip()
    unsupported_hit = _detect_unsupported_policy(
        text,
        unsupported_policies=active_unsupported_policies,
        blocked_aliases=active_blocked_aliases,
        rapidfuzz_available=False,
    )

    if not active_alias_index:
        return question, None

    exact_matches = []
    weak_exact_hit = False
    for alias in sorted(active_alias_index.keys(), key=len, reverse=True):
        data = active_alias_index[alias]
        if _alias_term_matches(alias, text) and data.get("routing_strength") == "weak":
            weak_exact_hit = True
            continue
        if _alias_term_matches(alias, text):
            exact_matches.append((alias, data))

    if (
        not unsupported_hit
        and fuzzy_enabled
        and active_process is not None
        and active_fuzz is not None
    ):
        unsupported_hit = _detect_unsupported_policy(
            text,
            unsupported_policies=active_unsupported_policies,
            blocked_aliases=active_blocked_aliases,
            rapidfuzz_available=fuzzy_enabled,
            process_module=active_process,
            fuzz_module=active_fuzz,
        )

    if exact_matches:
        if unsupported_hit:
            drug_match = next(
                ((alias, data) for alias, data in exact_matches if data.get("category") == "drugs"),
                None,
            )
            if drug_match and unsupported_hit.get("allowed_if_explicit_drug", True):
                alias, data = drug_match
            else:
                return question, None
        else:
            alias, data = exact_matches[0]
        normalized_question = (
            question
            + f"\n\nRecognized term: {data['display']}"
            + f"\nCanonical term: {data['canonical']}"
        )
        return normalized_question, {
            **data,
            "matched_alias": alias,
            "confidence": "exact",
            "score": 100,
        }

    if weak_exact_hit:
        return question, None

    if not fuzzy_enabled or active_process is None or active_fuzz is None:
        return question, None

    if not _has_clinical_intent_or_structure(text):
        return question, None

    fuzzy_blocked_hit = _detect_unsupported_policy(
        text,
        unsupported_policies=active_unsupported_policies,
        blocked_aliases=active_blocked_aliases,
        rapidfuzz_available=fuzzy_enabled,
        process_module=active_process,
        fuzz_module=active_fuzz,
    )
    if fuzzy_blocked_hit and not unsupported_hit:
        unsupported_hit = fuzzy_blocked_hit

    alias_keys = [
        a for a in active_alias_index.keys()
        if _compact_len(a) >= _MIN_FUZZY_ALIAS_CHARS
        and active_alias_index[a].get("routing_strength") != "weak"
    ]
    best = _extract_best_fuzzy_choice(
        text,
        alias_keys,
        process_module=active_process,
        fuzz_module=active_fuzz,
    )

    if not best["alias"]:
        return question, None

    alias = best["alias"]
    score = best["score"]
    data = active_alias_index[alias]

    if unsupported_hit and (
        data.get("category") != "drugs"
        or not unsupported_hit.get("allowed_if_explicit_drug", True)
    ):
        return question, None

    if not _fuzzy_candidate_quality_ok(text, alias):
        return question, None

    if score >= _HIGH_CONFIDENCE_FUZZY_SCORE:
        normalized_question = (
            question
            + f"\n\nRecognized term: {data['display']}"
            + f"\nCanonical term: {data['canonical']}"
        )
        return normalized_question, {
            **data,
            "matched_alias": alias,
            "confidence": "high",
            "score": score,
            "fuzzy_source": best["source"],
        }
    if score >= _MEDIUM_CONFIDENCE_FUZZY_SCORE:
        normalized_question = (
            question
            + f"\nPossible recognized term: {data['display']}"
            + f"\nCanonical term: {data['canonical']}"
        )
        return normalized_question, {
            **data,
            "matched_alias": alias,
            "confidence": "medium",
            "score": score,
            "fuzzy_source": best["source"],
        }
    return question, None


def _detect_unsupported_syndrome(question, *, blocked_aliases=None, unsupported_policies=None):
    hit = _detect_unsupported_policy(
        question,
        blocked_aliases=blocked_aliases,
        unsupported_policies=unsupported_policies,
    )
    return hit.get("key") if hit else None


_OBVIOUS_NONCLINICAL_RE = re.compile(
    r"^\s*(?:"
    r"hi|hello|hey|good morning|good afternoon|good evening|"
    r"how are you\??|thanks?|thank you|ok|okay|"
    r"what is the capital of .+|capital of .+|"
    r"tell me a joke|what'?s the weather\??"
    r")\s*$",
    re.IGNORECASE,
)


def _is_obvious_nonclinical_message(question):
    return bool(_OBVIOUS_NONCLINICAL_RE.match(question or ""))


__all__ = [
    "ALIASES",
    "ALIAS_INDEX",
    "BLOCKED_ALIASES",
    "UNSUPPORTED_SYNDROMES",
    "PROTOCOL_FILE_TO_LABEL",
    "load_aliases",
    "normalize_path",
    "collect_alias_matches",
    "normalize_question",
    "_build_alias_index",
    "_alias_term_matches",
    "_detect_unsupported_policy",
    "_detect_unsupported_syndrome",
    "_is_obvious_nonclinical_message",
    "_has_clinical_intent_or_structure",
]
