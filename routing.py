"""
Routing, intent classification, tree dispatch, and answer orchestration.

The dataclasses are owned here. Other public functions remain compatibility
wrappers during the split and import ``bot_core`` lazily to avoid cycles.
"""

from dataclasses import dataclass, field
import importlib


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
