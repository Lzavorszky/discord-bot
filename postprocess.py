"""
postprocess.py — Response post-processing pipeline.

Cleans raw LLM output and applies the canonical answer format:

    <body>

    [per-protocol footer]

    [SAFETY_FOOTER]

    Source: <source_label>

Public API
----------
clean_response(text, source_label)          — body + safety footer + Source
apply_footer(body, footer)                  — append a footer block (de-duped)
finalize_answer(body, footer, source_label) — full pipeline (used by ask_ai)
"""

import re
from config import SAFETY_FOOTER


# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

_BOLD_RE = re.compile(r'\*\*(.+?)\*\*', re.DOTALL)

_SOURCE_LINE_RE = re.compile(
    r'[\n\r]?[ \t]*[-•]?[ \t]*'
    r'(?:Source|Forrás|Source file[s]?|Forrás fájl[ok]?)'
    r'[ \t]*[:\*]*[ \t]*[`"]?[^\n\r]*',
    re.IGNORECASE
)

_FILE_PATH_RE = re.compile(r'`?protocols/[^\s`\n\r,;]+`?', re.IGNORECASE)

_NOT_SPEC_RE = re.compile(
    r'[-•]?[ \t]*This is not specified in the uploaded protocol\.?[ \t]*[\n\r]?',
    re.IGNORECASE
)

_BLANK_RE      = re.compile(r'\n{3,}')
_HAS_DOSING_RE = re.compile(r'\d+\s*(mg|g|amp|ml|mmol|mcg)', re.IGNORECASE)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _clean_body(text):
    """Steps 1–5: strip markdown, kill LLM source lines/paths, tidy blanks.
    Does NOT append the Source line — call finalize_answer for the full pipeline."""
    text = _BOLD_RE.sub(r'\1', text)            # 1. strip bold
    text = _SOURCE_LINE_RE.sub('', text)        # 2. remove model-generated source lines
    text = _FILE_PATH_RE.sub('', text)          # 3. remove stray file paths
    if _HAS_DOSING_RE.search(text):
        text = _NOT_SPEC_RE.sub('', text)       # 4. remove contradictory "not specified"
    return _BLANK_RE.sub('\n\n', text).strip()  # 5. tidy blank lines


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def apply_footer(body, footer):
    """Append per-protocol footer between body and Source line.
    No-op when footer is None/empty or already an exact substring of body (de-dupe)."""
    if not footer:
        return body
    if footer in body:
        return body
    return body + f'\n\n{footer}'


def clean_response(text, source_label):
    """Backward-compat wrapper: clean body + Source line. No per-protocol footer."""
    text = _clean_body(text)
    text = apply_footer(text, SAFETY_FOOTER)
    if source_label:
        text = text + f'\n\nSource: {source_label}'
    return text


def finalize_answer(body, footer, source_label):
    """Full post-processing pipeline used by ask_ai:
    clean body → apply per-protocol footer → SAFETY_FOOTER → Source line."""
    text = _clean_body(body)
    text = apply_footer(text, footer)
    text = apply_footer(text, SAFETY_FOOTER)
    if source_label:
        text = text + f'\n\nSource: {source_label}'
    return text
