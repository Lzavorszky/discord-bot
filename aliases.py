"""
Alias loading and query-normalization boundary.

This facade delegates to ``bot_core`` during the compatibility phase so legacy
monkeypatches against ``telegram_bot`` and newer imports from ``aliases`` see
the same runtime state.
"""

import bot_core as _core


def load_aliases(path="protocols/aliases.json"):
    return _core.load_aliases(path)


def normalize_question(question):
    return _core.normalize_question(question)


def _build_alias_index(alias_data):
    return _core._build_alias_index(alias_data)


def __getattr__(name):
    return getattr(_core, name)


__all__ = [
    "ALIASES",
    "ALIAS_INDEX",
    "PROTOCOL_FILE_TO_LABEL",
    "load_aliases",
    "normalize_question",
    "_build_alias_index",
]
