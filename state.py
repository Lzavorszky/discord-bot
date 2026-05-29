"""
Conversation and decision-tree state lifecycle helpers.
"""

import bot_core as _core


def _now_iso():
    return _core._now_iso()


def _parse_iso(ts):
    return _core._parse_iso(ts)


def get_chat_state(chat_id):
    return _core.get_chat_state(chat_id)


def init_tree_state(state, parsed_protocol, recognized):
    return _core.init_tree_state(state, parsed_protocol, recognized)


def advance_tree_state(state, next_node_id, collected_updates=None):
    return _core.advance_tree_state(state, next_node_id, collected_updates)


def reset_tree_state(state):
    return _core.reset_tree_state(state)


def is_tree_idle_timeout(state):
    return _core.is_tree_idle_timeout(state)


def is_explicit_reset_phrase(text):
    return _core.is_explicit_reset_phrase(text)


def maybe_auto_reset_tree(state):
    return _core.maybe_auto_reset_tree(state)


def __getattr__(name):
    return getattr(_core, name)


__all__ = [
    "CONVERSATION_STATE",
    "TREE_IDLE_TIMEOUT_SECONDS",
    "EXPLICIT_RESET_RE",
    "_now_iso",
    "_parse_iso",
    "get_chat_state",
    "init_tree_state",
    "advance_tree_state",
    "reset_tree_state",
    "is_tree_idle_timeout",
    "is_explicit_reset_phrase",
    "maybe_auto_reset_tree",
]
