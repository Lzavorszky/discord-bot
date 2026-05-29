"""
Protocol schema parsing facade.

The concrete parser still lives in ``protocol_parser.py`` for compatibility
with earlier sessions. This module is the Session 11 responsibility boundary
for schema constants and parser helpers.
"""

from protocol_parser import (  # noqa: F401
    CANONICAL_PANELS,
    POLICY_SECTIONS,
    VALID_ANSWER_MODES,
    VALID_SELECTION_MODES,
    _NONE_BODY_RE,
    _parse_links_block,
    _parse_metadata_block,
    _parse_protocol_links,
    _parse_protocol_text,
    extract_policy_header,
    parse_decision_tree,
    parse_protocol_file,
)


__all__ = [
    "CANONICAL_PANELS",
    "POLICY_SECTIONS",
    "VALID_ANSWER_MODES",
    "VALID_SELECTION_MODES",
    "_NONE_BODY_RE",
    "_parse_links_block",
    "_parse_metadata_block",
    "_parse_protocol_links",
    "_parse_protocol_text",
    "extract_policy_header",
    "parse_decision_tree",
    "parse_protocol_file",
]
