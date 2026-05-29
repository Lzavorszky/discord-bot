"""
Protocol loading, embedding cache, and semantic retrieval boundary.
"""

import bot_core as _core


def normalize_path(path):
    return _core.normalize_path(path)


def derive_source_label(file_path):
    return _core.derive_source_label(file_path)


def extract_source_label_from_text(text):
    return _core.extract_source_label_from_text(text)


def load_protocols():
    return _core.load_protocols()


def search_protocols(question, top_k=3, preferred_file=None, guaranteed_slots=2):
    return _core.search_protocols(question, top_k, preferred_file, guaranteed_slots)


def _compute_file_hash(file_path):
    return _core._compute_file_hash(file_path)


def _init_cache_db():
    return _core._init_cache_db()


def _load_from_cache(file_hash):
    return _core._load_from_cache(file_hash)


def _save_to_cache(file_hash, chunks):
    return _core._save_to_cache(file_hash, chunks)


def __getattr__(name):
    return getattr(_core, name)


__all__ = [
    "PROTOCOL_CHUNKS",
    "PROTOCOL_POLICY_BY_FILE",
    "PROTOCOL_PARSED_BY_FILE",
    "EXCLUDED_FROM_PROTOCOLS",
    "normalize_path",
    "derive_source_label",
    "extract_source_label_from_text",
    "load_protocols",
    "search_protocols",
    "_compute_file_hash",
    "_init_cache_db",
    "_load_from_cache",
    "_save_to_cache",
]
