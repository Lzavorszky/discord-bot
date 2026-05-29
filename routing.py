"""
Routing, intent classification, tree dispatch, and answer orchestration.
"""

import bot_core as _core


def classify_intent(question: str) -> str:
    return _core.classify_intent(question)


def dispatch_tree(state, recognized, raw_question, normalized_question):
    return _core.dispatch_tree(state, recognized, raw_question, normalized_question)


def ask_ai(question, chat_id):
    return _core.ask_ai(question, chat_id)


def build_debug_trace(debug_question, chat_id):
    return _core.build_debug_trace(debug_question, chat_id)


def format_debug_output(retrieved_chunks):
    return _core.format_debug_output(retrieved_chunks)


def format_protocols_output():
    return _core.format_protocols_output()


def format_version_output():
    return _core.format_version_output()


def get_protocol_library_version():
    return _core.get_protocol_library_version()


def _build_drug_name_set():
    return _core._build_drug_name_set()


def _handle_dosing_shortcut(state: dict, question: str, recognized):
    return _core._handle_dosing_shortcut(state, question, recognized)


def _handle_organism_disambiguation(state: dict, question: str, recognized):
    return _core._handle_organism_disambiguation(state, question, recognized)


def _update_routing_state(state: dict, recognized, context_source: str):
    return _core._update_routing_state(state, recognized, context_source)


def _update_recommended_antibiotics(state: dict, response_text: str, recognized):
    return _core._update_recommended_antibiotics(state, response_text, recognized)


def _try_deterministic_selection(state, recognized, question, lang):
    return _core._try_deterministic_selection(state, recognized, question, lang)


def __getattr__(name):
    return getattr(_core, name)


__all__ = [
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
