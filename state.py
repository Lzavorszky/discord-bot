"""
Conversation state and protocol-scoped slot lifecycle helpers.
"""

import datetime as _dt
import re
from pathlib import Path


CONVERSATION_STATE = {}
TREE_IDLE_TIMEOUT_SECONDS = 30 * 60
EXPLICIT_RESET_RE = re.compile(
    r"^\s*(?:"
    r"új beteg|új eset|új téma|új kérdés|"
    r"másik beteg|másik eset|másik téma|"
    r"új|másik|"
    r"new case|new patient|different patient|different case|new topic|"
    r"reset|clear"
    r")[\s\.\?!]*$",
    re.IGNORECASE,
)


def bind_state(state_store):
    global CONVERSATION_STATE
    CONVERSATION_STATE = state_store


def _normalize_path(path):
    return str(Path(path)).replace("\\", "/").lower()


def _now_iso():
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(ts):
    if not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def get_chat_state(chat_id):
    if chat_id not in CONVERSATION_STATE:
        CONVERSATION_STATE[chat_id] = {
            "history": [],
            "active_recognized": None,
            "tree": None,
            "pending_topic_switch": None,
            "pending_context_confirmation": None,
            "pending_links": None,
            "active_protocol_id": None,
            "protocol_type": None,
            "last_user_intent": None,
            "collected_slots": {},
            "slots_by_protocol": {},
            "pending_question": None,
            "last_recommended_antibiotics": [],
            "dosing_allowed": None,
            "linked_dosing_protocol_available": None,
            "context_source": None,
        }
    state = CONVERSATION_STATE[chat_id]
    state.setdefault("tree", None)
    state.setdefault("pending_topic_switch", None)
    state.setdefault("pending_context_confirmation", None)
    state.setdefault("pending_links", None)
    state.setdefault("active_protocol_id", None)
    state.setdefault("protocol_type", None)
    state.setdefault("last_user_intent", None)
    state.setdefault("collected_slots", {})
    state.setdefault("slots_by_protocol", {})
    state.setdefault("pending_question", None)
    state.setdefault("last_recommended_antibiotics", [])
    state.setdefault("dosing_allowed", None)
    state.setdefault("linked_dosing_protocol_available", None)
    state.setdefault("context_source", None)
    return state


def init_tree_state(state, parsed_protocol, recognized):
    tree_def = parsed_protocol.get("decision_tree") if parsed_protocol else None
    if not tree_def or not tree_def.get("root"):
        return
    now = _now_iso()
    state["tree"] = {
        "protocol_file": recognized.get("protocol_file", ""),
        "current_node": tree_def["root"],
        "collected": {},
        "started_at": now,
        "last_node_at": now,
    }


def advance_tree_state(state, next_node_id, collected_updates=None):
    if not state.get("tree"):
        return
    state["tree"]["current_node"] = next_node_id
    if collected_updates:
        state["tree"]["collected"].update(collected_updates)
    state["tree"]["last_node_at"] = _now_iso()


def reset_tree_state(state):
    state["tree"] = None
    state["pending_topic_switch"] = None
    state["pending_context_confirmation"] = None
    state["last_user_intent"] = None
    state["collected_slots"] = {}
    state["slots_by_protocol"] = {}
    state["pending_question"] = None
    state["last_recommended_antibiotics"] = []
    state["context_source"] = None


def reset_patient_state(state):
    """Clear all protocol/patient context while preserving chat history."""
    reset_tree_state(state)
    state["active_recognized"] = None
    state["active_protocol_id"] = None
    state["protocol_type"] = None
    state["dosing_allowed"] = None
    state["linked_dosing_protocol_available"] = None
    state["pending_links"] = None


def _slot_namespace_for_recognized(recognized):
    if not recognized:
        return None
    protocol_file = recognized.get("protocol_file", "")
    if protocol_file:
        return _normalize_path(protocol_file)
    protocol_id = recognized.get("protocol_id") or recognized.get("display")
    return str(protocol_id).lower() if protocol_id else None


def _get_protocol_slots(state, recognized):
    ns = _slot_namespace_for_recognized(recognized)
    if not ns:
        return dict(state.get("collected_slots", {}))
    slots_by_protocol = state.setdefault("slots_by_protocol", {})
    return dict(slots_by_protocol.get(ns, {}))


def _set_protocol_slots(state, recognized, slots):
    ns = _slot_namespace_for_recognized(recognized)
    clean = dict(slots or {})
    if ns:
        state.setdefault("slots_by_protocol", {})[ns] = clean
    state["collected_slots"] = clean


def mirror_active_protocol_slots(state, recognized=None):
    active = recognized or state.get("active_recognized")
    slots = _get_protocol_slots(state, active)
    state["collected_slots"] = slots
    return slots


def transfer_protocol_slots(
    state,
    source_recognized,
    target_recognized,
    transfer_slots,
    *,
    target_slot_names=None,
    extra_slots=None,
):
    """Copy an explicit allowlist of slots between protocol namespaces.

    Unknown target slots are discarded when target_slot_names is supplied,
    preventing microbiology/pathway facts from becoming dosing slots merely
    because they existed in the source protocol.
    """
    allowed = {str(name).lower() for name in (transfer_slots or []) if name}
    target_allowed = (
        {str(name).lower() for name in target_slot_names}
        if target_slot_names is not None
        else None
    )
    source_slots = _get_protocol_slots(state, source_recognized)
    merged_source = dict(source_slots)
    merged_source.update(extra_slots or {})
    carried = {}
    for key, value in merged_source.items():
        key_l = str(key).lower()
        if key_l not in allowed:
            continue
        if target_allowed is not None and key_l not in target_allowed:
            continue
        carried[key_l] = value
    _set_protocol_slots(state, target_recognized, carried)
    return carried


def is_tree_idle_timeout(state):
    tree = state.get("tree")
    if not tree:
        return False
    last = _parse_iso(tree.get("last_node_at") or tree.get("started_at"))
    if not last:
        return False
    return (_dt.datetime.now(_dt.timezone.utc) - last).total_seconds() > TREE_IDLE_TIMEOUT_SECONDS


def is_explicit_reset_phrase(text):
    return bool(EXPLICIT_RESET_RE.match(text or ""))


def maybe_auto_reset_tree(state):
    if is_tree_idle_timeout(state):
        reset_tree_state(state)
        return True
    return False


__all__ = [
    "CONVERSATION_STATE",
    "TREE_IDLE_TIMEOUT_SECONDS",
    "EXPLICIT_RESET_RE",
    "bind_state",
    "_now_iso",
    "_parse_iso",
    "get_chat_state",
    "init_tree_state",
    "advance_tree_state",
    "reset_tree_state",
    "reset_patient_state",
    "_slot_namespace_for_recognized",
    "_get_protocol_slots",
    "_set_protocol_slots",
    "mirror_active_protocol_slots",
    "transfer_protocol_slots",
    "is_tree_idle_timeout",
    "is_explicit_reset_phrase",
    "maybe_auto_reset_tree",
]
