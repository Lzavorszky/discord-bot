"""
Hospital Protocol Telegram Bot
Answers clinical protocol questions using RAG (retrieval-augmented generation).

Flat protocol folder structure expected:
  protocols/*.txt          <- all protocol files
  protocols/aliases.json   <- synonym/alias map

Rule files in bot root:
  system_rules.txt
  answer_format_rules.txt
  answer_style_rules.txt
  safety_rules.txt

Environment variables required:
  TELEGRAM_TOKEN
  OPENAI_API_KEY
"""

import os
import glob
import json
import re
import hashlib
import sqlite3
import logging
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import numpy as np
from openai import OpenAI
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters

try:
    from rapidfuzz import process, fuzz
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY")

openai_client   = OpenAI(api_key=OPENAI_API_KEY)

EMBEDDING_MODEL = "text-embedding-3-small"
CHAT_MODEL      = "gpt-4o-mini"

MAX_HISTORY_TURNS = 10

SAFETY_FOOTER = "  ⚠️ de ellenőrizd!"

START_MESSAGE = (
    "Bár sokat tud, ez végső soron csak egy chatbot, aki néhány txt fájlt olvasgat.\n"
    "\n"
    "Első a józan ész.\n"
    "\n"
    "Parancsok:\n"
    "  /reset  — beszélgetési előzmények törlése\n"
    "  /debug <kérdés>  — mutatja, melyik protokoll részletek töltődtek be"
)

CACHE_DB = "embeddings_cache.db"
LOG_FILE  = "bot_queries.log"

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

PROTOCOL_CHUNKS         = []
ALIASES                 = {}
ALIAS_INDEX             = {}
PROTOCOL_FILE_TO_LABEL  = {}
# Always-included gating header per protocol file (ANSWER_POLICY,
# DEFAULT_QUESTION, REQUIRED_INFORMATION, PATHWAY_PRIORITY).
# Keyed by normalized file path.
PROTOCOL_POLICY_BY_FILE = {}

# Full parsed protocol per file, in the canonical-panel schema.
# Keyed by normalized file path. See parse_protocol_file() for shape.
# Populated alongside PROTOCOL_POLICY_BY_FILE in load_protocols().
PROTOCOL_PARSED_BY_FILE = {}

SYSTEM_RULES        = ""
ANSWER_FORMAT_RULES = ""
ANSWER_STYLE_RULES  = ""
SAFETY_RULES        = ""

# Per-chat conversation state:
# { chat_id: {"history": [...], "active_recognized": {...} or None} }
CONVERSATION_STATE = {}


# ---------------------------------------------------------------------------
# Allowlist
#
# Only Telegram user IDs in this set can use the bot.
# Set the ALLOWED_USER_IDS environment variable as a comma-separated list:
#   ALLOWED_USER_IDS=123456789,987654321
#
# To find a Telegram user ID: message @userinfobot on Telegram.
# If the env var is not set, the bot runs open (no restriction) — fine for
# local testing but set it before giving access to anyone else.
# ---------------------------------------------------------------------------

def _load_allowlist():
    """Return a set of allowed integer user IDs, or empty set if unrestricted."""
    raw = os.getenv("ALLOWED_USER_IDS", "").strip()
    if not raw:
        return set()
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
        else:
            print(f"[allowlist] WARNING: ignoring non-numeric entry: {part!r}")
    return ids


ALLOWED_USER_IDS: set = set()  # populated at startup


def _is_allowed(user_id: int) -> bool:
    """Return True if allowlist is empty (open) or user_id is in it."""
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS


# ---------------------------------------------------------------------------
# Startup checks
# Runs before anything else. Exits immediately with a clear error if
# something critical is missing — better to crash loudly at boot than
# silently return wrong answers at 2am.
# ---------------------------------------------------------------------------

def run_startup_checks():
    import sys
    errors = []
    warnings = []

    # 1. Required environment variables
    if not os.getenv("TELEGRAM_TOKEN"):
        errors.append("TELEGRAM_TOKEN environment variable is not set.")
    if not os.getenv("OPENAI_API_KEY"):
        errors.append("OPENAI_API_KEY environment variable is not set.")

    # 2. Rule files — bot will answer without personality/safety rules if missing
    for rule_file in ["system_rules.txt", "answer_format_rules.txt",
                       "answer_style_rules.txt", "safety_rules.txt"]:
        if not os.path.exists(rule_file):
            warnings.append(f"Rule file missing: {rule_file}")

    # 3. aliases.json — must exist and be valid JSON
    aliases_path = "protocols/aliases.json"
    if not os.path.exists(aliases_path):
        errors.append(
            f"{aliases_path} not found. "
            "This file must exist at protocols/aliases.json — "
            "do not keep a copy in the root folder."
        )
    else:
        try:
            with open(aliases_path, "r", encoding="utf-8") as f:
                alias_data = json.load(f)
        except json.JSONDecodeError as e:
            errors.append(f"{aliases_path} is not valid JSON: {e}")
            alias_data = {}

        # 4. Every protocol_file referenced in aliases.json must exist on disk
        for category in ["drugs", "conditions"]:
            for key, item in alias_data.get(category, {}).items():
                pf = item.get("protocol_file", "")
                if pf and not os.path.exists(pf):
                    errors.append(
                        f"aliases.json → {key}: protocol_file not found: {pf}"
                    )

    # 5. protocols/ folder must exist and contain at least one .txt file
    if not os.path.isdir("protocols"):
        errors.append("protocols/ folder not found.")
    else:
        txt_files = (
            glob.glob("protocols/*.txt") +
            glob.glob("protocols/**/*.txt", recursive=True)
        )
        real_files = [f for f in txt_files
                      if Path(f).name not in EXCLUDED_FROM_PROTOCOLS]
        if not real_files:
            errors.append("protocols/ folder contains no .txt protocol files.")
        else:
            print(f"[startup] Found {len(real_files)} protocol file(s): "
                  f"{[Path(f).name for f in real_files]}")

    # Report
    for w in warnings:
        print(f"[startup] WARNING: {w}")
    if errors:
        print()
        print("=" * 60)
        print("STARTUP FAILED — fix the following before running the bot:")
        for e in errors:
            print(f"  ✗  {e}")
        print("=" * 60)
        sys.exit(1)

    if not os.getenv("ALLOWED_USER_IDS", "").strip():
        print("[startup] WARNING: ALLOWED_USER_IDS is not set — bot is open to everyone.")
    print(f"[startup] All checks passed.")


# ---------------------------------------------------------------------------
# Structured logging
#
# Every query is written as a JSON line to bot_queries.log AND to stdout.
# JSON-lines format: one complete JSON object per line, easy to grep and parse.
# The file rotates at 5 MB, keeping 3 old copies (so max ~20 MB on disk).
# On Railway, stdout is captured in the Railway log viewer automatically.
#
# Each log entry contains:
#   ts              — ISO timestamp
#   chat_id_hash    — MD5 of the real chat_id (traceable but not identifiable)
#   user_message    — exactly what the user typed
#   recognized      — matched drug/condition and confidence, or null
#   retrieved       — list of {source_label, similarity} for retrieved chunks
#   raw_llm         — the LLM response before post-processing
#   final           — the response sent to the user
#   duration_ms     — total time from message received to response sent
# ---------------------------------------------------------------------------

def setup_logging():
    """Call once at startup. Full JSON logs go to bot_queries.log (file only).
    Short human-readable summaries go to stdout so Railway captures them cleanly."""
    import sys

    # Root logger for general bot messages — stdout, so Railway shows them without [err]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    # Separate structured logger for full JSON audit trail — FILE ONLY.
    # We do NOT add a StreamHandler here: Railway truncates long JSON lines when
    # copying/downloading logs, making them appear blank. Use _log_query_stdout()
    # for a short human-readable summary instead.
    query_logger = logging.getLogger("query")
    query_logger.setLevel(logging.INFO)
    query_logger.propagate = False  # don't double-log to root

    # Rotating file: max 5 MB, keep 3 backups
    fh = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(message)s"))  # raw JSON only
    query_logger.addHandler(fh)

    logging.info(f"Logging initialised → {LOG_FILE}")
    return query_logger


# Module-level query logger (populated by setup_logging())
_query_log = None


def _log_query(chat_id, user_message, recognized, retrieved_chunks,
               raw_llm, final_response, duration_ms):
    """Write full JSON to the audit log file AND a short readable summary to stdout.

    The JSON in the file is the full audit record (raw_llm + final included).
    The stdout summary is intentionally short so Railway log copy/download never
    truncates it — it's what you read when debugging.

    Summary format example:
      [Q] a3f2c1 | "Sumetrolim, Steno BSI, 60 kg, GFR 60" → TMP/SMX (exact)
      [R] TMP/SMX:0.89  TMP/SMX:0.85  TMP/SMX:0.81
      [A] Stenotrophomonas BSI → high-dose TMP/SMX. GFR 60: no renal adjustment...  [1843ms]
    """
    import sys

    # Timezone-aware UTC (datetime.utcnow() is deprecated in Python 3.12+).
    import datetime as _dt
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    chat_hash = hashlib.md5(str(chat_id).encode()).hexdigest()[:8]

    # ── Full JSON to file ──────────────────────────────────────────────────
    if _query_log is not None:
        entry = {
            "ts":            ts,
            "chat_id_hash":  chat_hash,
            "user_message":  user_message,
            "recognized":    {
                "display":       recognized["display"],
                "matched_alias": recognized.get("matched_alias", ""),
                "confidence":    recognized["confidence"],
                "protocol_file": recognized.get("protocol_file", ""),
            } if recognized else None,
            "retrieved": [
                {"source_label": c["source_label"], "similarity": round(c["similarity"], 4)}
                for c in retrieved_chunks
            ],
            "raw_llm":    raw_llm,
            "final":      final_response,
            "duration_ms": duration_ms,
        }
        _query_log.info(json.dumps(entry, ensure_ascii=False))

    # ── Short readable summary to stdout (survives Railway log copy) ───────
    rec_str = (
        f"{recognized['display']} ({recognized['confidence']})"
        if recognized else "NO MATCH"
    )
    chunks_str = "  ".join(
        f"{c['source_label']}:{round(c['similarity'], 2)}"
        for c in retrieved_chunks
    ) or "none"

    # Truncate the final answer at 120 chars for the summary line
    answer_preview = (final_response or "").replace("\n", " ").strip()
    if len(answer_preview) > 120:
        answer_preview = answer_preview[:117] + "..."

    print(f"[Q] {chat_hash} | {user_message!r} → {rec_str}", flush=True)
    print(f"[R] {chunks_str}", flush=True)
    print(f"[A] {answer_preview}  [{duration_ms}ms]", flush=True)


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def load_text_file(path):
    if not os.path.exists(path):
        print(f"Rule file not found: {path}")
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def load_rule_files():
    global SYSTEM_RULES, ANSWER_FORMAT_RULES, ANSWER_STYLE_RULES, SAFETY_RULES
    SYSTEM_RULES        = load_text_file("system_rules.txt")
    ANSWER_FORMAT_RULES = load_text_file("answer_format_rules.txt")
    ANSWER_STYLE_RULES  = load_text_file("answer_style_rules.txt")
    SAFETY_RULES        = load_text_file("safety_rules.txt")
    print("Loaded rule files")


def normalize_path(path):
    return str(Path(path)).replace("\\", "/").lower()


def derive_source_label(file_path):
    stem = Path(file_path).stem.lower()
    fallback_labels = {
        "tmpsmx":                          "TMP/SMX",
        "tmp_smx":                         "TMP/SMX",
        "ampsul":                          "ampicillin/sulbactam",
        "ampicillin_sulbactam":            "ampicillin/sulbactam",
        "amp_sul":                         "ampicillin/sulbactam",
        "meropenem":                       "meropenem",
        "cap":                             "CAP",
        "biofire":                         "BioFire",
        "pneumonia_pcr":                   "BioFire",
        "general_rules_antibiotic_dosing": "General antibiotic dosing rules",
    }
    return fallback_labels.get(stem, stem.replace("_", " ").title())


def extract_source_label_from_text(text):
    for line in text.splitlines()[:20]:
        match = re.match(r"^\s*source_label\s*:\s*(.+?)\s*$", line, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# Alias recognition
# ---------------------------------------------------------------------------

def load_aliases(path="protocols/aliases.json"):
    global ALIASES, ALIAS_INDEX, PROTOCOL_FILE_TO_LABEL
    if not os.path.exists(path):
        print("No aliases.json found. Alias recognition disabled.")
        ALIASES = {}
        ALIAS_INDEX = {}
        PROTOCOL_FILE_TO_LABEL = {}
        return
    with open(path, "r", encoding="utf-8") as f:
        ALIASES = json.load(f)
    ALIAS_INDEX, PROTOCOL_FILE_TO_LABEL = _build_alias_index(ALIASES)
    print(f"Loaded {len(ALIAS_INDEX)} aliases")
    if not RAPIDFUZZ_AVAILABLE:
        print("rapidfuzz not installed — fuzzy matching disabled, exact matching only.")


def _build_alias_index(alias_data):
    alias_index = {}
    protocol_file_to_label = {}
    for category in ["drugs", "conditions"]:
        for key, item in alias_data.get(category, {}).items():
            display       = item.get("display", key)
            canonical     = item.get("canonical", display)
            source_label  = item.get("source_label", display)
            protocol_file = item.get("protocol_file", "")
            data = {
                "key": key, "category": category,
                "display": display, "canonical": canonical,
                "source_label": source_label, "protocol_file": protocol_file,
            }
            if protocol_file:
                protocol_file_to_label[normalize_path(protocol_file)] = source_label
            terms = [display, canonical] + item.get("aliases", [])
            for term in terms:
                if term:
                    alias_index[term.lower()] = data
    return alias_index, protocol_file_to_label


def normalize_question(question):
    """
    Returns (normalized_question, recognized_metadata_or_None).

    Matching cascade (each step short-circuits on hit):
      1. Substring exact (longest alias first) — catches all spellings
         already known in aliases.json.
      2. Per-word fuzzy — split the query into words ≥5 chars and fuzz each
         word against the alias index. Isolates the keyword from sentence
         noise like "mit adjak?", which otherwise drags WRatio below the
         threshold for typical misspells.
      3. Whole-text partial_ratio — safety net for compound aliases like
         "ampicillin sulbactam" that are not a single token.

    Thresholds: HIGH ≥88, MED ≥80. Per-word fuzzy on an isolated word
    scores typical one-letter misspells around 90, so 88 is the sweet
    spot — high enough to reject random text, low enough to catch the
    misspells we care about.
    """
    text = question.lower().strip()
    if not ALIAS_INDEX:
        return question, None

    # ── 1. Substring exact — longest alias first to avoid short-alias collisions
    for alias in sorted(ALIAS_INDEX.keys(), key=len, reverse=True):
        data = ALIAS_INDEX[alias]
        if len(alias) <= 4:
            matched = re.search(r"\b" + re.escape(alias) + r"\b", text) is not None
        else:
            matched = re.search(r"\b" + re.escape(alias) + r"\b", text) is not None
        if matched:
            normalized_question = (
                question
                + f"\n\nRecognized term: {data['display']}"
                + f"\nCanonical term: {data['canonical']}"
            )
            return normalized_question, {
                **data, "matched_alias": alias,
                "confidence": "exact", "score": 100,
            }

    if not RAPIDFUZZ_AVAILABLE:
        return question, None

    # Only fuzz against aliases ≥5 chars. Short aliases like "cap", "mem",
    # "hap", "vap" produce noisy partial-string matches against unrelated
    # words (e.g. partial_ratio("vancomycin", "cap") flagged "vancomycin
    # dose" as CAP). Short aliases are already handled by step 1's exact
    # word-boundary substring match — bypassing them here removes a class
    # of false positives without losing any real match.
    alias_keys = [a for a in ALIAS_INDEX.keys() if len(a) >= 5]
    best = {"alias": None, "score": 0, "source": ""}

    # ── 2. Per-word fuzzy — only consider words long enough to be meaningful.
    # Short tokens like "mit", "a", "re", "is" are filler — fuzzing them
    # against the alias index produces noisy matches.
    for word in re.findall(r"\w+", text):
        if len(word) < 5:
            continue
        m = process.extractOne(word, alias_keys, scorer=fuzz.WRatio)
        if m and m[1] > best["score"]:
            best = {"alias": m[0], "score": m[1], "source": f"word:{word}"}

    # ── 3. Whole-text partial_ratio — multi-word aliases.
    m2 = process.extractOne(text, alias_keys, scorer=fuzz.partial_ratio)
    if m2 and m2[1] > best["score"]:
        best = {"alias": m2[0], "score": m2[1], "source": "partial_ratio"}

    if not best["alias"]:
        return question, None

    alias = best["alias"]
    score = best["score"]
    data = ALIAS_INDEX[alias]

    if score >= 88:
        normalized_question = (
            question
            + f"\n\nRecognized term: {data['display']}"
            + f"\nCanonical term: {data['canonical']}"
        )
        return normalized_question, {
            **data, "matched_alias": alias,
            "confidence": "high", "score": score,
        }
    if score >= 80:
        normalized_question = (
            question
            + f"\nPossible recognized term: {data['display']}"
            + f"\nCanonical term: {data['canonical']}"
        )
        return normalized_question, {
            **data, "matched_alias": alias,
            "confidence": "medium", "score": score,
        }
    return question, None


# ---------------------------------------------------------------------------
# Embeddings cache (SQLite)
#
# Avoids calling the OpenAI embeddings API on every restart.
# Logic: hash each protocol file's contents; if the hash is already in the
# cache DB, load stored embeddings instead of recomputing them.
# The cache file (embeddings_cache.db) lives in the bot root directory.
# On Railway hobby tier the filesystem is ephemeral (wiped on deploy), so
# the cache helps with crash-restarts but not fresh deploys. Add a Railway
# persistent volume later to make it survive deploys too.
# ---------------------------------------------------------------------------

def _compute_file_hash(file_path):
    """MD5 fingerprint of a file's contents. Changes if the file changes."""
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _init_cache_db():
    """Create the cache table if it doesn't exist yet."""
    con = sqlite3.connect(CACHE_DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS embedding_cache (
            file_hash    TEXT PRIMARY KEY,
            source       TEXT NOT NULL,
            source_label TEXT NOT NULL,
            chunks_json  TEXT NOT NULL,
            created_at   TEXT DEFAULT (datetime('now'))
        )
    """)
    con.commit()
    con.close()


def _load_from_cache(file_hash):
    """Return list of chunk dicts if hash found, else None."""
    con = sqlite3.connect(CACHE_DB)
    row = con.execute(
        "SELECT chunks_json FROM embedding_cache WHERE file_hash = ?",
        (file_hash,)
    ).fetchone()
    con.close()
    if row is None:
        return None
    raw = json.loads(row[0])
    # Restore numpy arrays from plain lists
    for chunk in raw:
        chunk["embedding"] = np.array(chunk["embedding"])
    return raw


def _save_to_cache(file_hash, chunks):
    """Persist chunks (with embeddings as plain lists) under file_hash."""
    serialisable = []
    for chunk in chunks:
        serialisable.append({
            "source":       chunk["source"],
            "source_label": chunk["source_label"],
            "text":         chunk["text"],
            "embedding":    chunk["embedding"].tolist(),
        })
    con = sqlite3.connect(CACHE_DB)
    con.execute(
        """INSERT OR REPLACE INTO embedding_cache
           (file_hash, source, source_label, chunks_json)
           VALUES (?, ?, ?, ?)""",
        (file_hash,
         chunks[0]["source"],
         chunks[0]["source_label"],
         json.dumps(serialisable))
    )
    con.commit()
    con.close()


# ---------------------------------------------------------------------------
# Protocol loading and retrieval
# ---------------------------------------------------------------------------

EXCLUDED_FROM_PROTOCOLS = {
    "system_rules.txt",
    "answer_format_rules.txt",
    "answer_style_rules.txt",
    "safety_rules.txt",
    "aliases.json",
}


# Section headers that define the gating rules for a protocol.
# These are ALWAYS prepended to the LLM context when the protocol is
# alias-recognized, regardless of what semantic search returned — otherwise
# treatment-pathway chunks crowd them out and the LLM dumps pathway info
# without first asking the required clarifying question.
POLICY_SECTIONS = {
    "ANSWER_POLICY",
    "DEFAULT_QUESTION",
    "REQUIRED_INFORMATION",
    "PATHWAY_PRIORITY",
}


# ---------------------------------------------------------------------------
# Canonical panel schema
#
# Every protocol file SHOULD declare these top-level `## SECTION_NAME`
# panels in this order. Missing panels are tolerated (treated as empty)
# for backward compatibility. Unknown `## ...` headers go into `free_form`
# and are still LLM-visible via RAG. Single-hash `# ...` headers are
# ignored as panel boundaries.
#
# Parsed (Python uses the value):
#   METADATA, DEFAULT_QUESTION, DECISION_TREE, DEFAULT_FOOTER
# LLM-only (text is injected via gating header or surfaced via RAG):
#   ALIASES, ANSWER_POLICY, REQUIRED_INFORMATION, PREFERRED_INFORMATION,
#   MODIFIER_INFORMATION, PATHWAY_PRIORITY, TREATMENT_PATHWAYS, SAFETY_NOTES
# ---------------------------------------------------------------------------

CANONICAL_PANELS = [
    "METADATA",
    "ALIASES",
    "ANSWER_POLICY",
    "REQUIRED_INFORMATION",
    "PREFERRED_INFORMATION",
    "MODIFIER_INFORMATION",
    "DEFAULT_QUESTION",
    "PATHWAY_PRIORITY",
    "DECISION_TREE",
    "TREATMENT_PATHWAYS",
    "SAFETY_NOTES",
    "DEFAULT_FOOTER",
    "PROTOCOL_LINKS",   # 13th panel: cross-protocol handoff declarations
]

# Matches a `## SECTION_NAME` header at the start of a line. Permissive
# enough to also catch existing non-canonical headers like
# `## CONTINUOUS VS BOLUS DOSING` (which go to free_form). The name must
# start with an uppercase letter or underscore.
_PANEL_HEADER_RE = re.compile(
    r"^##[ \t]+([A-Z_][A-Z0-9_ ]*?)[ \t]*$",
    re.MULTILINE,
)

# A panel body of just `(none)` (case-insensitive) is semantically empty.
# Authors should write `(none)` rather than omitting a panel, so the file
# itself documents what was deliberately left blank.
_NONE_BODY_RE = re.compile(r"^\s*\(none\)\s*$", re.IGNORECASE)


def extract_policy_header(text):
    """Return the concatenated text of all POLICY_SECTIONS found in `text`,
    or an empty string if none are present.

    Sections are delimited by lines starting with `## SECTION_NAME` and
    end at the next `## ` heading or end of file.
    """
    pattern = re.compile(r"^##\s+([A-Z_]+)\s*$", re.MULTILINE)
    matches = list(pattern.finditer(text))
    if not matches:
        return ""

    kept = []
    for i, m in enumerate(matches):
        name = m.group(1)
        if name not in POLICY_SECTIONS:
            continue
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        kept.append(text[start:end].strip())
    return "\n\n".join(kept)


# ---------------------------------------------------------------------------
# Canonical-schema parser
#
# parse_protocol_file(path) reads a `.txt` protocol and returns a dict with
# one entry per canonical panel, plus a `free_form` bucket for any
# unrecognised `## ...` section. The parser is non-fatal: missing panels
# become empty strings (or None for the three Python-parsed ones), and
# old-style files keep working — they just have empty canonical panels
# and most content in free_form.
#
# This function does NOT yet feed back into ask_ai; that wiring lands in
# the next steps (state model, tree dispatcher, footer). For now it just
# populates PROTOCOL_PARSED_BY_FILE so we can inspect what got parsed.
# ---------------------------------------------------------------------------

def parse_protocol_file(path):
    """Read a protocol .txt and return its canonical-panel dict.

    Returns a dict with these keys:
      metadata              dict[str, str]   parsed key:value lines from ## METADATA
      aliases               str              body of ## ALIASES (informational)
      answer_policy         str
      required_information  str
      preferred_information str
      modifier_information  str
      default_question      str | None       None if (none) or missing
      pathway_priority      str
      decision_tree         dict | None      see parse_decision_tree()
      treatment_pathways    str
      safety_notes          str
      default_footer        str | None       None if (none) or missing
      free_form             dict[str, str]   any other ## section, keyed by name
      path                  str              input path (for diagnostics)
      warnings              list[str]        non-fatal issues (e.g. panel order)
    """
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    return _parse_protocol_text(text, path=path)


def _parse_protocol_text(text, path="<inline>"):
    """Pure-string variant of parse_protocol_file (no disk IO)."""
    result = {
        "path":                  path,
        "metadata":              {},
        "aliases":               "",
        "answer_policy":         "",
        "required_information":  "",
        "preferred_information": "",
        "modifier_information":  "",
        "default_question":      None,
        "pathway_priority":      "",
        "decision_tree":         None,
        "treatment_pathways":    "",
        "safety_notes":          "",
        "default_footer":        None,
        "protocol_links":        {},   # {label: {file, ctx_keys}}
        "free_form":             {},
        "warnings":              [],
    }

    matches = list(_PANEL_HEADER_RE.finditer(text))
    if not matches:
        return result

    seen_canonical = []
    for i, m in enumerate(matches):
        raw_name = m.group(1).strip()
        # Normalize: spaces -> underscores so `CONTINUOUS VS BOLUS DOSING`
        # becomes `CONTINUOUS_VS_BOLUS_DOSING` for the free_form key.
        name = re.sub(r"[ \t]+", "_", raw_name).upper()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip()

        if _NONE_BODY_RE.match(body):
            body = ""

        if name in CANONICAL_PANELS:
            seen_canonical.append(name)
            if name == "METADATA":
                result["metadata"] = _parse_metadata_block(body)
            elif name == "DECISION_TREE":
                result["decision_tree"] = parse_decision_tree(body) if body else None
            elif name == "DEFAULT_QUESTION":
                result["default_question"] = body or None
            elif name == "DEFAULT_FOOTER":
                result["default_footer"] = body or None
            elif name == "PROTOCOL_LINKS":
                result["protocol_links"] = _parse_protocol_links(body) if body else {}
            else:
                result[name.lower()] = body
        else:
            result["free_form"][name] = body

    # Warn (non-fatal) if canonical panels appear out of canonical order.
    expected_order = [p for p in CANONICAL_PANELS if p in seen_canonical]
    if seen_canonical != expected_order:
        result["warnings"].append(
            f"{path}: canonical panels out of order. "
            f"got={seen_canonical} expected={expected_order}"
        )

    return result


def _parse_metadata_block(text):
    """Parse `key: value` lines from a ## METADATA panel.

    Blank lines and `# comment` lines are skipped. Keys are lowercased,
    values are stripped of surrounding whitespace.
    """
    meta = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        meta[key.strip().lower()] = val.strip()
    return meta


def _parse_protocol_links(text):
    """Parse a ## PROTOCOL_LINKS panel body.

    Each non-blank, non-comment line has the form::

        label -> protocols/foo.txt [via: key1, key2]

    Returns a dict keyed by label::

        {
          "ceftriaxone": {"file": "protocols/ceftriaxone.txt", "ctx_keys": ["renal_gfr"]},
          "clarithromycin": {"file": "protocols/clarithromycin.txt", "ctx_keys": []},
        }
    """
    _LINK_RE = re.compile(
        r"^(\S+)\s*->\s*(\S+)(?:\s+via:\s*(.+))?$"
    )
    result = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _LINK_RE.match(line)
        if not m:
            continue
        label    = m.group(1)
        file_    = m.group(2)
        ctx_keys = [k.strip() for k in m.group(3).split(",")] if m.group(3) else []
        result[label] = {"file": file_, "ctx_keys": ctx_keys}
    return result


# ---------------------------------------------------------------------------
# Decision-tree parser
#
# A `## DECISION_TREE` panel body looks like:
#
#   ROOT: <node_id>
#
#   NODE: <node_id>
#     TYPE: question | collect | answer
#     ASK_HU: <text>              (or `ASK_HU: |` for a multi-line block)
#     ASK_EN: <text>
#     ANSWER_HU: ... / ANSWER_EN: ... / ANSWER_REF: <pathway_id>
#     BRANCHES:                   (question nodes)
#       <label> -> <target_node_id>
#     COLLECT:                    (collect nodes)
#       - name: <key>
#         type: number | one_of | text
#         values: [a, b, c]       (one_of only)
#         unit: <string>          (optional)
#     NEXT: <node_id>             (collect nodes)
#     THEN: end                   (answer nodes)
#     HINT: <free text or | block>
#
# `NODE:` lines (no indent) delimit nodes. A node block runs until the
# next NODE: or the end of the section. ROOT: defaults to the first node
# if omitted.
# ---------------------------------------------------------------------------

_TREE_NODE_RE = re.compile(r"^NODE:[ \t]*(\w+)[ \t]*$", re.MULTILINE)
_TREE_ROOT_RE = re.compile(r"^ROOT:[ \t]*(\w+)[ \t]*$", re.MULTILINE)
_TREE_KEY_RE = re.compile(r"^([ \t]*)([A-Z_][A-Z_0-9]*):[ \t]*(.*?)[ \t]*$")
_TREE_BRANCH_RE = re.compile(r"^[ \t]*(\S+)[ \t]*->[ \t]*(\S+)[ \t]*$")


def parse_decision_tree(text):
    """Parse the body of a ## DECISION_TREE panel.

    Returns {"root": <node_id>, "nodes": {id: <node dict>}} or None if
    the section is empty / contains no NODE: declarations.
    """
    if not text:
        return None

    node_matches = list(_TREE_NODE_RE.finditer(text))
    if not node_matches:
        return None

    root_match = _TREE_ROOT_RE.search(text)
    root = root_match.group(1) if root_match else node_matches[0].group(1)

    nodes = {}
    for i, m in enumerate(node_matches):
        node_id = m.group(1)
        body_start = m.end()
        body_end = node_matches[i + 1].start() if i + 1 < len(node_matches) else len(text)
        nodes[node_id] = _parse_tree_node(node_id, text[body_start:body_end])

    return {"root": root, "nodes": nodes}


_TREE_NODE_KEYS = {
    "type", "ask_hu", "ask_en",
    "answer_hu", "answer_en", "answer_ref",
    "next", "then", "hint", "link",
}


def _parse_tree_node(node_id, body):
    """Parse one NODE: block body into a node dict."""
    node = {
        "id":         node_id,
        "type":       None,
        "ask_hu":     None,
        "ask_en":     None,
        "answer_hu":  None,
        "answer_en":  None,
        "answer_ref": None,
        "next":       None,
        "then":       None,
        "hint":       None,
        "link":       [],   # list of PROTOCOL_LINKS labels offered after this answer
        "branches":   {},   # label -> target node id
        "collect":    [],   # list of {name, type, values?, unit?}
    }

    lines = body.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        m = _TREE_KEY_RE.match(line)
        if not m:
            i += 1
            continue

        indent = len(m.group(1))
        key = m.group(2).lower()
        value = m.group(3)

        if key == "branches":
            i += 1
            while i < len(lines):
                sub = lines[i]
                if not sub.strip():
                    i += 1
                    continue
                sub_indent = len(sub) - len(sub.lstrip())
                if sub_indent <= indent:
                    break
                bm = _TREE_BRANCH_RE.match(sub)
                if bm:
                    node["branches"][bm.group(1)] = bm.group(2)
                i += 1
            continue

        if key == "collect":
            i += 1
            item = None
            while i < len(lines):
                sub = lines[i]
                if not sub.strip():
                    i += 1
                    continue
                sub_indent = len(sub) - len(sub.lstrip())
                if sub_indent <= indent:
                    break
                stripped = sub.strip()
                if stripped.startswith("- "):
                    if item is not None:
                        node["collect"].append(item)
                    item = {}
                    rest = stripped[2:]
                    if ":" in rest:
                        ck, cv = rest.split(":", 1)
                        item[ck.strip().lower()] = cv.strip()
                else:
                    if ":" in stripped:
                        ck, cv = stripped.split(":", 1)
                        if item is None:
                            item = {}
                        item[ck.strip().lower()] = cv.strip()
                i += 1
            if item is not None:
                node["collect"].append(item)
            continue

        if value == "|":
            # Block scalar: subsequent lines indented deeper than `indent`
            # form the value. Preserve their relative indentation by
            # stripping a single common prefix.
            i += 1
            block_lines = []
            block_indent = None
            while i < len(lines):
                sub = lines[i]
                if not sub.strip():
                    block_lines.append("")
                    i += 1
                    continue
                sub_indent = len(sub) - len(sub.lstrip())
                if sub_indent <= indent:
                    break
                if block_indent is None:
                    block_indent = sub_indent
                block_lines.append(
                    sub[block_indent:] if len(sub) >= block_indent else sub.lstrip()
                )
                i += 1
            block_text = "\n".join(block_lines).rstrip()
            if key in _TREE_NODE_KEYS:
                node[key] = block_text
            continue

        # Simple scalar — LINK: is comma-split into a list
        if key == "link":
            node["link"] = [s.strip() for s in value.split(",") if s.strip()]
        elif key in _TREE_NODE_KEYS:
            node[key] = value.strip()
        i += 1

    return node


def chunk_text(text, source, source_label, max_chars=900):
    sections = text.split("\n\n")
    chunks = []
    current = ""
    for section in sections:
        if len(current) + len(section) < max_chars:
            current += section + "\n\n"
        else:
            if current.strip():
                chunks.append({"source": source, "source_label": source_label, "text": current.strip()})
            current = section + "\n\n"
    if current.strip():
        chunks.append({"source": source, "source_label": source_label, "text": current.strip()})
    return chunks


def get_embedding(text):
    response = openai_client.embeddings.create(model=EMBEDDING_MODEL, input=text)
    return np.array(response.data[0].embedding)


def get_source_label_for_file(file_path, text):
    metadata_label = extract_source_label_from_text(text)
    if metadata_label:
        return metadata_label
    normalized = normalize_path(file_path)
    if normalized in PROTOCOL_FILE_TO_LABEL:
        return PROTOCOL_FILE_TO_LABEL[normalized]
    return derive_source_label(file_path)


def load_protocols():
    global PROTOCOL_CHUNKS
    _init_cache_db()

    # Support both flat and nested protocol folder.
    # Normalize to absolute paths before deduplicating so that
    # protocols/*.txt and protocols/**/*.txt never produce duplicate
    # entries for the same file on any OS/filesystem.
    raw = (
        glob.glob("protocols/*.txt") +
        glob.glob("protocols/**/*.txt", recursive=True)
    )
    files = list({os.path.abspath(f): f for f in raw}.values())

    loaded = cached = fresh = 0
    for file_path in sorted(files):
        if Path(file_path).name in EXCLUDED_FROM_PROTOCOLS:
            print(f"Skipping excluded file: {file_path}")
            continue

        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()

        # Always (re)extract policy header — cheap, no API call, and we want
        # it picked up even when chunks come from the embeddings cache.
        policy_header = extract_policy_header(text)
        if policy_header:
            PROTOCOL_POLICY_BY_FILE[normalize_path(file_path)] = policy_header

        # Also parse the full canonical-panel schema so later steps can read
        # decision_tree, default_footer, default_question, etc. Safe for any
        # file: missing panels become empty/None and old-style content goes
        # into free_form. Not yet consumed by ask_ai — that's the next step.
        parsed = _parse_protocol_text(text, path=file_path)
        PROTOCOL_PARSED_BY_FILE[normalize_path(file_path)] = parsed
        for w in parsed.get("warnings", []):
            print(f"[startup] WARNING: {w}")

        file_hash = _compute_file_hash(file_path)
        cached_chunks = _load_from_cache(file_hash)

        if cached_chunks is not None:
            # File unchanged — use stored embeddings, no API call
            PROTOCOL_CHUNKS.extend(cached_chunks)
            print(f"  [cache] {file_path} ({len(cached_chunks)} chunks)")
            cached += 1
        else:
            # File is new or changed — embed and store
            source_label = get_source_label_for_file(file_path, text)
            chunks = chunk_text(text=text, source=file_path, source_label=source_label)
            for chunk in chunks:
                chunk["embedding"] = get_embedding(chunk["text"])
            _save_to_cache(file_hash, chunks)
            PROTOCOL_CHUNKS.extend(chunks)
            print(f"  [fresh] {file_path} ({len(chunks)} chunks, embeddings computed)")
            fresh += 1

        loaded += 1

    print(f"Total: {len(PROTOCOL_CHUNKS)} chunks from {loaded} files "
          f"({cached} from cache, {fresh} freshly embedded)")


def search_protocols(question, top_k=3, preferred_file=None, guaranteed_slots=2):
    """
    Semantic search. When preferred_file is set, guarantee at least
    `guaranteed_slots` results come from that file so the active protocol
    is not crowded out by unrelated files during multi-turn conversations.
    """
    question_embedding = get_embedding(question)
    preferred_file_norm = normalize_path(preferred_file) if preferred_file else None

    preferred_chunks = []
    other_chunks = []

    for chunk in PROTOCOL_CHUNKS:
        similarity = float(np.dot(question_embedding, chunk["embedding"]))
        entry = {
            "source":       chunk["source"],
            "source_label": chunk["source_label"],
            "text":         chunk["text"],
            "similarity":   similarity,
        }
        if preferred_file_norm and normalize_path(chunk["source"]) == preferred_file_norm:
            preferred_chunks.append(entry)
        else:
            other_chunks.append(entry)

    preferred_chunks.sort(key=lambda x: x["similarity"], reverse=True)
    other_chunks.sort(key=lambda x: x["similarity"], reverse=True)

    if preferred_file_norm and preferred_chunks:
        slots_for_preferred = min(guaranteed_slots, len(preferred_chunks), top_k)
        slots_for_others    = top_k - slots_for_preferred
        return preferred_chunks[:slots_for_preferred] + other_chunks[:slots_for_others]

    all_chunks = preferred_chunks + other_chunks
    all_chunks.sort(key=lambda x: x["similarity"], reverse=True)
    return all_chunks[:top_k]


# ---------------------------------------------------------------------------
# Response post-processing
# Enforces formatting rules the LLM does not reliably follow.
# ---------------------------------------------------------------------------

_BOLD_RE        = re.compile(r'\*\*(.+?)\*\*', re.DOTALL)
_SOURCE_LINE_RE = re.compile(
    r'[\n\r]?[ \t]*[-•]?[ \t]*'
    r'(?:Source|Forrás|Source file[s]?|Forrás fájl[ok]?)'
    r'[ \t]*[:\*]*[ \t]*[`"]?[^\n\r]*',
    re.IGNORECASE
)
_FILE_PATH_RE   = re.compile(r'`?protocols/[^\s`\n\r,;]+`?', re.IGNORECASE)
_NOT_SPEC_RE    = re.compile(
    r'[-•]?[ \t]*This is not specified in the uploaded protocol\.?[ \t]*[\n\r]?',
    re.IGNORECASE
)
_BLANK_RE       = re.compile(r'\n{3,}')
_HAS_DOSING_RE  = re.compile(r'\d+\s*(mg|g|amp|ml|mmol|mcg)', re.IGNORECASE)


def _clean_body(text):
    """Steps 1–5: strip markdown, kill LLM source lines/paths, tidy blanks.
    Does NOT append the Source line — call finalize_answer for the full pipeline."""
    text = _BOLD_RE.sub(r'\1', text)            # 1. strip bold
    text = _SOURCE_LINE_RE.sub('', text)        # 2. remove model-generated source lines
    text = _FILE_PATH_RE.sub('', text)          # 3. remove stray file paths
    if _HAS_DOSING_RE.search(text):
        text = _NOT_SPEC_RE.sub('', text)       # 4. remove contradictory "not specified"
    return _BLANK_RE.sub('\n\n', text).strip()  # 5. tidy blank lines


def clean_response(text, source_label):
    """Backward-compat wrapper: clean body + Source line. No per-protocol footer."""
    text = _clean_body(text)
    if source_label:
        text = text + f'\n\nSource: {source_label}{SAFETY_FOOTER}'
    return text


def apply_footer(body, footer):
    """Append per-protocol footer between body and Source line.
    No-op when footer is None/empty or already an exact substring of body (de-dupe)."""
    if not footer:
        return body
    if footer in body:
        return body
    return body + f'\n\n{footer}'


def finalize_answer(body, footer, source_label):
    """Full post-processing pipeline used by ask_ai:
    clean body → apply per-protocol footer → append Source line."""
    text = _clean_body(body)
    text = apply_footer(text, footer)
    if source_label:
        text = text + f'\n\nSource: {source_label}{SAFETY_FOOTER}'
    return text


# ---------------------------------------------------------------------------
# AI answer generation
# ---------------------------------------------------------------------------

SOURCE_INSTRUCTION = (
    "DO NOT write a Source line in your response. "
    "The source is appended automatically after your answer. "
    "Do not write 'Source:', 'Forrás:', 'Source file:', or any file path."
)

POLICY_INSTRUCTION = (
    "If the context contains a 'PROTOCOL GATING RULES' block, it is binding. "
    "Follow ANSWER_POLICY exactly: when REQUIRED_INFORMATION is missing, ask "
    "the DEFAULT_QUESTION verbatim and do NOT list treatment pathways, do NOT "
    "suggest antibiotics, do NOT explain options yet. Wait for the user's "
    "answer, then apply PATHWAY_PRIORITY."
)


def build_recognition_context(recognized):
    if not recognized:
        return ""
    return (
        f"RECOGNIZED QUERY TERM:\n"
        f"User term matched: {recognized['matched_alias']}\n"
        f"Normalized to:     {recognized['display']}\n"
        f"Canonical name:    {recognized['canonical']}\n"
        f"Source label:      {recognized['source_label']}\n"
        f"Confidence:        {recognized['confidence']}"
    )


def build_system_prompt(recognized, context):
    return "\n\n".join(filter(None, [
        SYSTEM_RULES,
        ANSWER_FORMAT_RULES,
        ANSWER_STYLE_RULES,
        SAFETY_RULES,
        SOURCE_INSTRUCTION,
        POLICY_INSTRUCTION,
        build_recognition_context(recognized),
        f"PROTOCOL EXCERPTS:\n{context}",
    ]))


# ---------------------------------------------------------------------------
# Conversation state lifecycle
#
# State shape per chat_id:
#   history               list[dict]   — chat messages, trimmed to MAX_HISTORY_TURNS
#   active_recognized     dict | None  — last alias-recognized protocol metadata
#   tree                  dict | None  — active decision-tree walk, if any
#   pending_topic_switch  dict | None  — "user is mid-tree, but a new protocol
#                                        was just recognized; we asked them to
#                                        confirm the switch and are waiting"
#
# tree dict shape:
#   protocol_file  str         — which file's tree we're walking
#   current_node   str         — node id within that tree
#   collected      dict        — typed values gathered by collect nodes
#   started_at     str (ISO Z) — first node entered
#   last_node_at   str (ISO Z) — most recent node transition (used for timeout)
#
# pending_topic_switch dict shape:
#   from_protocol  str   — current tree's protocol_file
#   to_protocol    str   — new protocol's path (the one the user just mentioned)
#   to_recognized  dict  — the new recognized metadata, so we can act on "yes"
#   proposed_at    str   — ISO timestamp
#
# Reset triggers (handled here + in the dispatcher):
#   - /reset or /clear        → CONVERSATION_STATE.pop (full clear)
#   - idle > TREE_IDLE_TIMEOUT → tree state silently cleared on next message
#   - explicit reset phrase    → tree state cleared, ack to user
#   - topic-switch confirmed   → tree replaced (handled by the dispatcher)
# ---------------------------------------------------------------------------

# Minutes of silence after which an in-progress tree is presumed stale.
TREE_IDLE_TIMEOUT_SECONDS = 30 * 60

# Matches a message that is *only* a reset phrase (possibly with trailing
# punctuation). We require the phrase to be the whole message — otherwise
# clinical sentences containing "új" or "másik" would false-trigger.
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


def _now_iso():
    """UTC timestamp in ISO 8601 with Z suffix. Matches the format used in
    _log_query so timestamps line up across logs and state."""
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(ts):
    """Inverse of _now_iso. Returns a timezone-aware datetime, or None."""
    import datetime as _dt
    if not ts:
        return None
    try:
        return _dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def get_chat_state(chat_id):
    """Return (creating if necessary) the per-chat state dict.

    Always exposes the full key set, including the newer `tree` and
    `pending_topic_switch` fields, so callers never need to .get() with a
    default. setdefault() calls backfill any state that pre-dated the
    schema (matters once we move to Redis/SQLite persistence later).
    """
    if chat_id not in CONVERSATION_STATE:
        CONVERSATION_STATE[chat_id] = {
            "history":              [],
            "active_recognized":    None,
            "tree":                 None,
            "pending_topic_switch": None,
            "pending_links":        None,
        }
    state = CONVERSATION_STATE[chat_id]
    state.setdefault("tree", None)
    state.setdefault("pending_topic_switch", None)
    state.setdefault("pending_links", None)
    return state


def init_tree_state(state, parsed_protocol, recognized):
    """Start a new tree walk. Idempotent only in the sense that it
    overwrites any previous tree — callers should call reset_tree_state
    first if they want clean transitions."""
    tree_def = parsed_protocol.get("decision_tree") if parsed_protocol else None
    if not tree_def or not tree_def.get("root"):
        return
    now = _now_iso()
    state["tree"] = {
        "protocol_file": recognized.get("protocol_file", ""),
        "current_node":  tree_def["root"],
        "collected":     {},
        "started_at":    now,
        "last_node_at":  now,
    }


def advance_tree_state(state, next_node_id, collected_updates=None):
    """Move the current tree to next_node_id, optionally merging in
    newly-collected values, and refresh last_node_at."""
    if not state.get("tree"):
        return
    state["tree"]["current_node"] = next_node_id
    if collected_updates:
        state["tree"]["collected"].update(collected_updates)
    state["tree"]["last_node_at"] = _now_iso()


def reset_tree_state(state):
    """Clear the tree walk + any pending topic-switch prompt. Leaves
    history, active_recognized, and pending_links alone (pending_links
    survives a tree reset so the offer is still visible after the tree ends)."""
    state["tree"] = None
    state["pending_topic_switch"] = None


def is_tree_idle_timeout(state):
    """True if a tree is active and its last_node_at is older than the
    TREE_IDLE_TIMEOUT_SECONDS threshold."""
    import datetime as _dt
    tree = state.get("tree")
    if not tree:
        return False
    last = _parse_iso(tree.get("last_node_at"))
    if not last:
        return False
    age = (_dt.datetime.now(_dt.timezone.utc) - last).total_seconds()
    return age > TREE_IDLE_TIMEOUT_SECONDS


def is_explicit_reset_phrase(text):
    """True if the user's whole message is a reset phrase like 'új beteg'."""
    return bool(EXPLICIT_RESET_RE.match(text or ""))


def maybe_auto_reset_tree(state):
    """Apply silent reset triggers. Returns True if a reset happened.

    Currently only the idle-timeout trigger is silent. Explicit-phrase
    resets are handled by the dispatcher so we can ack the user.
    """
    if is_tree_idle_timeout(state):
        reset_tree_state(state)
        return True
    return False


# ---------------------------------------------------------------------------
# Tree dispatcher
#
# The dispatcher short-circuits the standard RAG+LLM flow whenever a
# decision-tree protocol is active. It handles three things:
#   1. Pending topic-switch confirmation (user is mid-tree, mentioned a
#      different protocol, we asked "switch? yes/no" and are awaiting reply)
#   2. New-tree initialisation (user just mentioned an alias whose
#      protocol has a ## DECISION_TREE — emit the root question)
#   3. Tree walking (user replied to the current node; classify, advance,
#      emit next ask or terminal answer)
#
# Question nodes use a small gpt-4o-mini classifier call to map the user's
# free-text reply to one of the node's branch labels. Collect nodes use a
# JSON-output extractor call to parse typed values. Answer nodes emit a
# fixed string (with `{collected_key}` interpolation) or resolve
# ANSWER_REF against the protocol's TREATMENT_PATHWAYS panel.
#
# Returns None to mean "I didn't handle this turn — fall through to the
# standard RAG+LLM flow." Any string return value is the final body (no
# source line, no footer — ask_ai adds those).
# ---------------------------------------------------------------------------

_HU_LETTERS = set("áéíóöőúüűÁÉÍÓÖŐÚÜŰ")

# Unambiguously-Hungarian short words that lack accented letters and
# therefore wouldn't trip the _HU_LETTERS check. Clinicians often reply
# with bare ASCII "igen" / "nem" — we need to call those HU.
_HU_ASCII_WORDS = {
    "igen", "nem", "kell", "nincs", "rendben", "persze",
    "dozis", "kerek", "koszonom", "oke",
    "beteg", "kezeles", "kerdes",
}

_YES_RE = re.compile(
    r"^\s*(igen|yes|y|ok|oké|persze|sure|jó|valt|váltsd|switch)[\s\.\?!]*$",
    re.IGNORECASE,
)
_NO_RE = re.compile(
    r"^\s*(nem|no|n|nope|stay|maradj|maradok|maradjunk|don't|dont)[\s\.\?!]*$",
    re.IGNORECASE,
)
_BOTH_RE = re.compile(
    r"^\s*(mindkett[oő]|both|mind2|2|igen.+mind|all)[\s\.\?!]*$",
    re.IGNORECASE,
)

_TREE_REF_HEADER_RE = re.compile(r"^###[ \t]+(\S+)[ \t]*$", re.MULTILINE)


def _user_language(text):
    """HU/EN detection. Returns 'hu' if either (a) any á/é/í/ó/ö/ő/ú/ü/ű
    is present, or (b) any unambiguously-Hungarian short word is present.
    Otherwise 'en'. Matches the answer_style_rules 'dominant language'
    heuristic well enough for clinician shorthand including bare 'igen'/'nem'."""
    if not text:
        return "en"
    if any(ch in _HU_LETTERS for ch in text):
        return "hu"
    tokens = set(re.findall(r"\w+", text.lower()))
    if tokens & _HU_ASCII_WORDS:
        return "hu"
    return "en"




def _pick_lang(hu_text, en_text, lang):
    """Pick HU or EN with fallback to whichever is non-empty."""
    if lang == "hu" and hu_text:
        return hu_text
    if lang == "en" and en_text:
        return en_text
    return hu_text or en_text or ""


def _label_for_protocol(path):
    """Friendly source label for `path`, falling back to file stem."""
    if not path:
        return "?"
    norm = normalize_path(path)
    if norm in PROTOCOL_FILE_TO_LABEL:
        return PROTOCOL_FILE_TO_LABEL[norm]
    return derive_source_label(path)


def _resolve_ref(treatment_pathways_text, ref):
    """Look up a `### ref_name` subsection inside the TREATMENT_PATHWAYS
    panel and return its body (or empty string if not found)."""
    if not treatment_pathways_text or not ref:
        return ""
    matches = list(_TREE_REF_HEADER_RE.finditer(treatment_pathways_text))
    for i, m in enumerate(matches):
        if m.group(1) == ref:
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(treatment_pathways_text)
            return treatment_pathways_text[start:end].strip()
    return ""


def _interpolate(text, collected):
    """Replace {key} placeholders in `text` with values from collected.
    Missing or None values render as '?'."""
    if not text:
        return text
    for k, v in (collected or {}).items():
        text = text.replace("{" + k + "}", str(v) if v is not None else "?")
    return text


def _resolve_answer(node, parsed, collected, lang):
    """Compose the body for an answer node."""
    hu = node.get("answer_hu")
    en = node.get("answer_en")
    ref = node.get("answer_ref")
    if hu or en:
        body = _pick_lang(hu, en, lang)
    elif ref:
        body = _resolve_ref(parsed.get("treatment_pathways", ""), ref)
    else:
        body = ""
    return _interpolate(body, collected)


def _classify_branch(node, user_message, lang):
    """Mini gpt-4o-mini call: pick one of node['branches'] labels matching
    the user's reply. Returns the label or None on failure."""
    if not node.get("branches"):
        return None
    ask = _pick_lang(node.get("ask_hu"), node.get("ask_en"), lang)
    labels = list(node["branches"].keys())
    hint = node.get("hint") or ""

    sys_prompt = (
        "You classify a clinician's reply into exactly one label from a "
        "fixed set. Return ONLY the label string, no other words, no "
        "punctuation, no explanation."
    )
    user_prompt = (
        f"Question previously asked: {ask}\n"
        f"Clinician's reply: {user_message}\n\n"
        "Pick exactly ONE of these labels (return verbatim):\n"
        + "\n".join(f"  {lbl}" for lbl in labels)
        + (f"\n\nGuidance:\n{hint}" if hint else "")
    )

    try:
        resp = openai_client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            max_tokens=20,
            temperature=0,
        )
        raw = (resp.choices[0].message.content or "").strip()
        cleaned = re.sub(r"[^A-Za-z0-9_]", "", raw).lower()
        for lbl in labels:
            if lbl.lower() == cleaned:
                return lbl
        # Loose contains-match as second pass (catches "confirmed." → confirmed)
        for lbl in labels:
            if lbl.lower() in cleaned or cleaned in lbl.lower():
                return lbl
        return None
    except Exception as e:
        print(f"[dispatcher] branch classifier error: {e}")
        return None


def _extract_collected(node, user_message, lang):
    """Mini gpt-4o-mini call with JSON output: extract typed values for a
    collect node. Returns dict {name: value} (values may be None if
    missing), or None on hard failure."""
    if not node.get("collect"):
        return {}
    ask = _pick_lang(node.get("ask_hu"), node.get("ask_en"), lang)
    schema_lines = []
    for item in node["collect"]:
        name = item.get("name", "?")
        t = item.get("type", "text")
        if t == "one_of":
            schema_lines.append(f'  "{name}": one of {item.get("values", "")}')
        elif t == "number":
            unit = item.get("unit", "")
            schema_lines.append(f'  "{name}": number' + (f" ({unit})" if unit else ""))
        else:
            schema_lines.append(f'  "{name}": string')

    sys_prompt = (
        "Extract clinical values from the user's reply into a JSON object. "
        "Return JSON only (no prose, no markdown fences). Use null for any "
        "value the user did not provide."
    )
    user_prompt = (
        f"Question asked: {ask}\n"
        f"User reply: {user_message}\n\n"
        "Extract into a JSON object with these keys:\n"
        + "\n".join(schema_lines)
    )

    try:
        resp = openai_client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            max_tokens=200,
            temperature=0,
            response_format={"type": "json_object"},
        )
        raw = (resp.choices[0].message.content or "").strip()
        data = json.loads(raw)
        # Only keep keys we asked for
        wanted = {item.get("name") for item in node["collect"]}
        return {k: v for k, v in data.items() if k in wanted}
    except Exception as e:
        print(f"[dispatcher] value extractor error: {e}")
        return None


def _get_node(parsed, node_id):
    """Lookup helper: return node dict or None."""
    if not parsed:
        return None
    tree = parsed.get("decision_tree")
    if not tree:
        return None
    return tree.get("nodes", {}).get(node_id)


def _emit_node_ask(state, user_message):
    """Return the current node's ASK text without classifying. Used when a
    tree was just initialised or after a confirmed topic switch — i.e.
    when the user's message was the trigger, not an answer to a question.
    For terminal answer nodes (degenerate tree), emit and reset."""
    tree = state.get("tree")
    if not tree:
        return None
    parsed = PROTOCOL_PARSED_BY_FILE.get(normalize_path(tree["protocol_file"]))
    node = _get_node(parsed, tree["current_node"])
    if not node:
        reset_tree_state(state)
        return None
    lang = _user_language(user_message)
    if node["type"] == "answer":
        body = _resolve_answer(node, parsed, tree["collected"], lang)
        body = _maybe_attach_links(state, node, parsed, body, lang)
        if node.get("then") == "end":
            reset_tree_state(state)
        return body
    return _pick_lang(node.get("ask_hu"), node.get("ask_en"), lang)


def _emit_or_recurse(state, user_message, lang):
    """After advancing the current_node pointer, emit either the new
    node's ASK or — if it's a terminal answer — the answer itself.
    Saves the user from having to send another message just to read a
    terminal answer."""
    tree = state.get("tree")
    if not tree:
        return None
    parsed = PROTOCOL_PARSED_BY_FILE.get(normalize_path(tree["protocol_file"]))
    node = _get_node(parsed, tree["current_node"])
    if not node:
        reset_tree_state(state)
        return None
    if node["type"] == "answer":
        body = _resolve_answer(node, parsed, tree["collected"], lang)
        body = _maybe_attach_links(state, node, parsed, body, lang)
        if node.get("then") == "end":
            reset_tree_state(state)
        return body
    return _pick_lang(node.get("ask_hu"), node.get("ask_en"), lang)


def _walk_current_node(state, user_message):
    """User's reply is an answer to the current node. Classify or extract,
    advance, and emit the next step. Returns the response body string."""
    tree = state["tree"]
    parsed = PROTOCOL_PARSED_BY_FILE.get(normalize_path(tree["protocol_file"]))
    node = _get_node(parsed, tree["current_node"])
    if not node:
        reset_tree_state(state)
        return None

    lang = _user_language(user_message)

    if node["type"] == "question":
        label = _classify_branch(node, user_message, lang)
        if label is None or label not in node["branches"]:
            ask = _pick_lang(node.get("ask_hu"), node.get("ask_en"), lang)
            prefix = "Nem értem. " if lang == "hu" else "Sorry, didn't catch that. "
            return prefix + ask
        target = node["branches"][label]
        if target == "end":
            reset_tree_state(state)
            return None
        if not _get_node(parsed, target):
            print(f"[dispatcher] unknown branch target {target!r} from {tree['current_node']}")
            reset_tree_state(state)
            return None
        advance_tree_state(state, target)
        return _emit_or_recurse(state, user_message, lang)

    if node["type"] == "collect":
        values = _extract_collected(node, user_message, lang)
        if values is None:
            ask = _pick_lang(node.get("ask_hu"), node.get("ask_en"), lang)
            return ("Nem tudtam értelmezni. " if lang == "hu" else "Couldn't parse that. ") + ask
        # Missing any required value? Re-ask.
        missing = [item.get("name") for item in node["collect"]
                   if values.get(item.get("name")) in (None, "", [])]
        if missing:
            ask = _pick_lang(node.get("ask_hu"), node.get("ask_en"), lang)
            need = ", ".join(missing)
            prefix = (f"Kell még: {need}. " if lang == "hu"
                      else f"Still need: {need}. ")
            return prefix + ask
        next_id = node.get("next")
        if not next_id or next_id == "end":
            advance_tree_state(state, tree["current_node"], collected_updates=values)
            reset_tree_state(state)
            return None
        if not _get_node(parsed, next_id):
            print(f"[dispatcher] unknown NEXT target {next_id!r} from {tree['current_node']}")
            reset_tree_state(state)
            return None
        advance_tree_state(state, next_id, collected_updates=values)
        return _emit_or_recurse(state, user_message, lang)

    if node["type"] == "answer":
        body = _resolve_answer(node, parsed, tree["collected"], lang)
        body = _maybe_attach_links(state, node, parsed, body, lang)
        if node.get("then") == "end":
            reset_tree_state(state)
        return body

    print(f"[dispatcher] unknown node type {node.get('type')!r} at {tree['current_node']}")
    reset_tree_state(state)
    return None


def _propose_topic_switch(state, tree, new_recognized):
    """User mid-tree mentioned a different protocol. Stash the proposed
    switch and emit a bilingual yes/no prompt."""
    from_label = _label_for_protocol(tree["protocol_file"])
    to_label = (new_recognized.get("display")
                or _label_for_protocol(new_recognized.get("protocol_file", "")))
    state["pending_topic_switch"] = {
        "from_protocol": tree["protocol_file"],
        "to_protocol":   new_recognized.get("protocol_file", ""),
        "to_recognized": new_recognized,
        "proposed_at":   _now_iso(),
    }
    return (
        f"Új téma — {from_label} helyett {to_label}? "
        f"Az aktuális folyamatot kitöröljem? igen / nem\n"
        f"(New topic — switch from {from_label} to {to_label}? "
        f"Discard the current flow? yes / no)"
    )


def _handle_pending_topic_switch(state, pending, user_message):
    """Resolve a pending switch. yes → reset old tree, init new. no →
    keep current tree, re-emit current node. anything else → re-ask."""
    lang = _user_language(user_message)
    if _YES_RE.match(user_message):
        new_recognized = pending["to_recognized"]
        reset_tree_state(state)
        state["active_recognized"] = new_recognized
        parsed = PROTOCOL_PARSED_BY_FILE.get(
            normalize_path(new_recognized.get("protocol_file", ""))
        )
        if parsed and parsed.get("decision_tree"):
            init_tree_state(state, parsed, new_recognized)
            return _emit_node_ask(state, user_message)
        # No tree on the new protocol — ack and let standard flow take next turn
        label = new_recognized.get("display") or "?"
        return (f"OK, váltottam — kérdezz a {label}-ról."
                if lang == "hu"
                else f"OK, switched — ask about {label}.")
    if _NO_RE.match(user_message):
        state["pending_topic_switch"] = None
        tree = state.get("tree")
        if tree:
            parsed = PROTOCOL_PARSED_BY_FILE.get(normalize_path(tree["protocol_file"]))
            node = _get_node(parsed, tree["current_node"])
            if node and node.get("type") in ("question", "collect"):
                ask = _pick_lang(node.get("ask_hu"), node.get("ask_en"), lang)
                prefix = "OK, maradunk. " if lang == "hu" else "OK, sticking with this. "
                return prefix + ask
        return "OK, maradunk." if lang == "hu" else "OK, sticking with this."
    return ("Nem értem. Új téma? igen / nem"
            if lang == "hu"
            else "Didn't catch that. New topic? yes / no")


# ---------------------------------------------------------------------------
# Cross-protocol handoff helpers
# ---------------------------------------------------------------------------

def _render_link_offer(labels, lang):
    """Build the one-line offer appended after an answer with LINK: entries.

    Single link:   "Kell dózis? → ceftriaxone"
    Multiple:      "Kell dózis? → ceftriaxone / clarithromycin / mindkettő"
    """
    arrow = " / ".join(labels)
    if len(labels) > 1:
        both = "mindkettő" if lang == "hu" else "both"
        arrow += f" / {both}"
    prefix = "Kell dózis? → " if lang == "hu" else "Need dosing? → "
    return prefix + arrow


def _maybe_attach_links(state, node, parsed, body, lang):
    """If an answer node declares LINK: entries, resolve them against the
    protocol's PROTOCOL_LINKS panel, snapshot the forwarded context
    (before tree reset clears it), set state['pending_links'], and append
    the offer line to the body.

    Returns the (possibly extended) body string.
    """
    link_labels = node.get("link") or []
    if not link_labels:
        return body

    proto_links = (parsed or {}).get("protocol_links") or {}

    # Snapshot forwarded context NOW before reset_tree_state clears the tree.
    tree_collected = (state.get("tree") or {}).get("collected", {})

    valid_entries = []
    for label in link_labels:
        entry_def = proto_links.get(label)
        if not entry_def:
            continue
        ctx_keys  = entry_def.get("ctx_keys", [])
        forwarded = {k: tree_collected[k] for k in ctx_keys if k in tree_collected}
        valid_entries.append({
            "label":     label,
            "file":      entry_def["file"],
            "ctx_keys":  ctx_keys,
            "forwarded": forwarded,
        })

    if not valid_entries:
        return body

    state["pending_links"] = valid_entries
    offer = _render_link_offer([e["label"] for e in valid_entries], lang)
    return body.rstrip() + "\n\n" + offer


def _is_link_batchable(entry):
    """True if the linked protocol has no REQUIRED_INFORMATION and no
    decision tree — meaning it can be resolved in a single RAG+LLM call
    without asking the user for more information."""
    parsed = PROTOCOL_PARSED_BY_FILE.get(normalize_path(entry["file"]))
    if not parsed:
        return False
    req     = (parsed.get("required_information") or "").strip()
    has_tree = bool(parsed.get("decision_tree"))
    return not req and not has_tree


def _rag_answer_for_link(entry, forwarded, recognized, state, lang):
    """Make a one-shot RAG+LLM call for a linked protocol that has no tree.

    `forwarded` is the pre-captured {key: value} dict from the parent tree.
    Returns raw LLM text (caller should run finalize_answer on it).
    """
    file_path = entry["file"]
    label     = entry["label"]

    question = ("Adagolás: " if lang == "hu" else "Dosing: ") + label
    if forwarded:
        pairs     = ", ".join(f"{k}={v}" for k, v in forwarded.items())
        question += f" ({pairs})"

    chunks = search_protocols(question, top_k=3, preferred_file=file_path)
    context = "\n\n---\n\n".join(
        f"Source: {c['source_label']}\n{c['text']}" for c in chunks
    )
    policy_header = PROTOCOL_POLICY_BY_FILE.get(normalize_path(file_path), "")
    if policy_header:
        sl      = recognized.get("source_label", "")
        context = (
            f"PROTOCOL GATING RULES (must be followed before any treatment info)\n"
            f"Source: {sl}\n{policy_header}\n\n---\n\n"
            + context
        )

    system_prompt = build_system_prompt(recognized, context)
    history       = state.get("history", [])
    response = openai_client.chat.completions.create(
        model=CHAT_MODEL,
        messages=[{"role": "system", "content": system_prompt}]
                 + history
                 + [{"role": "user", "content": question}],
    )
    return response.choices[0].message.content


def _activate_link_entry(state, entry, user_message, lang):
    """Activate a single linked protocol.

    If the target has a tree: init it, inject forwarded context, emit root.
    If no tree: do a one-shot RAG+LLM call and return the answer.
    """
    file_path = entry["file"]
    parsed    = PROTOCOL_PARSED_BY_FILE.get(normalize_path(file_path))
    if not parsed:
        return ("A protokoll nem elérhető." if lang == "hu"
                else "Protocol not available.")

    meta = parsed.get("metadata", {})
    recognized = {
        "protocol_file": file_path,
        "source_label":  meta.get("source_label", entry["label"]),
        "display":       meta.get("source_label", entry["label"]),
        "canonical":     meta.get("source_label", entry["label"]),
    }
    forwarded = entry.get("forwarded", {})

    state["active_recognized"] = recognized
    reset_tree_state(state)

    if parsed.get("decision_tree"):
        init_tree_state(state, parsed, recognized)
        if forwarded and state.get("tree"):
            state["tree"]["collected"].update(forwarded)
        return _emit_node_ask(state, user_message)

    # No tree — immediate RAG+LLM answer
    raw = _rag_answer_for_link(entry, forwarded, recognized, state, lang)
    if raw:
        footer       = parsed.get("default_footer")
        source_label = recognized.get("source_label")
        return finalize_answer(raw, footer, source_label)
    return ("A protokoll nem elérhető." if lang == "hu"
            else "Protocol not available.")


def _resolve_links_batch(state, entries, user_message, lang):
    """E5-B: resolve batchable links immediately, activate first gapped one.

    Batchable = no REQUIRED_INFORMATION and no tree.
    Gapped    = has a tree or REQUIRED_INFORMATION (needs interactive walk).
    """
    batchable = [e for e in entries if _is_link_batchable(e)]
    gapped    = [e for e in entries if not _is_link_batchable(e)]

    parts = []

    if batchable:
        # Build combined context from all batchable protocols
        all_chunks = []
        for entry in batchable:
            all_chunks.extend(
                search_protocols(entry["label"], top_k=2, preferred_file=entry["file"])
            )
        context = "\n\n---\n\n".join(
            f"Source: {c['source_label']}\n{c['text']}" for c in all_chunks
        )
        drug_list = ", ".join(e["label"] for e in batchable)
        # Merge all forwarded contexts
        merged_forwarded = {}
        for e in batchable:
            merged_forwarded.update(e.get("forwarded", {}))
        question = ("Adagolás: " if lang == "hu" else "Dosing: ") + drug_list
        if merged_forwarded:
            pairs     = ", ".join(f"{k}={v}" for k, v in merged_forwarded.items())
            question += f" ({pairs})"
        system_prompt = build_system_prompt(None, context)
        response = openai_client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[{"role": "system", "content": system_prompt}]
                     + state.get("history", [])
                     + [{"role": "user", "content": question}],
        )
        parts.append(response.choices[0].message.content)

    if gapped:
        # Activate the first gapped protocol; note any remaining ones
        first = gapped[0]
        activation = _activate_link_entry(state, first, user_message, lang)
        if len(gapped) > 1:
            remaining  = ", ".join(e["label"] for e in gapped[1:])
            note       = ("\n\n(Ezután: " if lang == "hu" else "\n\n(Next: ") + remaining + ")"
            activation = activation.rstrip() + note
        parts.append(activation)

    return "\n\n".join(p for p in parts if p) or None


def _handle_pending_links(state, pending_links, user_message):
    """Resolve a pending link-offer. Called with top priority in dispatch_tree
    when state['pending_links'] is set.

    Handles:
      - "mindkettő"/"both" → batch or sequential
      - label name in message → activate that protocol
      - no/nem → clear offer, fall through
      - anything else → re-ask
    """
    lang   = _user_language(user_message)
    labels = [e["label"] for e in pending_links]

    # "both/mindkettő" path (E5-B)
    if len(pending_links) > 1 and _BOTH_RE.match(user_message.strip()):
        state["pending_links"] = None
        return _resolve_links_batch(state, pending_links, user_message, lang)

    # Single-label pick (case-insensitive substring match)
    msg_lower = user_message.lower().strip()
    for entry in pending_links:
        if entry["label"].lower() in msg_lower:
            state["pending_links"] = None
            return _activate_link_entry(state, entry, user_message, lang)

    # Decline
    if _NO_RE.match(user_message.strip()):
        state["pending_links"] = None
        return None   # fall through to standard RAG flow

    # Unrecognised — re-ask
    offer  = _render_link_offer(labels, lang)
    prefix = "Nem értem. " if lang == "hu" else "Didn't catch that. "
    return prefix + offer


def dispatch_tree(state, recognized, raw_question, normalized_question):
    """Main entry. Returns a finished response body to skip the standard
    LLM flow, or None to fall through. May mutate state (tree pointer,
    pending_topic_switch, active_recognized)."""
    # 1. Pending topic-switch confirmation has top priority.
    pending = state.get("pending_topic_switch")
    if pending:
        return _handle_pending_topic_switch(state, pending, raw_question)

    # 1a. Pending link-offer (cross-protocol handoff) has second priority.
    pending_links = state.get("pending_links")
    if pending_links:
        return _handle_pending_links(state, pending_links, raw_question)

    tree = state.get("tree")

    # 2. No tree active — maybe init one if the recognized protocol has a tree.
    if not tree:
        if not recognized:
            return None
        parsed = PROTOCOL_PARSED_BY_FILE.get(
            normalize_path(recognized.get("protocol_file", ""))
        )
        if not parsed or not parsed.get("decision_tree"):
            return None
        # Update active_recognized so the source label resolves correctly.
        state["active_recognized"] = recognized
        init_tree_state(state, parsed, recognized)
        return _emit_node_ask(state, raw_question)

    # 3. Tree active. Did the user mention a *different* protocol?
    if (recognized
            and recognized.get("protocol_file")
            and recognized["protocol_file"] != tree.get("protocol_file")):
        return _propose_topic_switch(state, tree, recognized)

    # 4. Walk the current node.
    return _walk_current_node(state, raw_question)


# ---------------------------------------------------------------------------
# AI answer generation (cont.)
# ---------------------------------------------------------------------------

def ask_ai(question, chat_id):
    t_start = time.monotonic()
    state   = get_chat_state(chat_id)

    # Silent reset of trees that have been idle too long.
    if maybe_auto_reset_tree(state):
        print(f"[dispatcher] tree idle-reset for chat {chat_id}")

    # Explicit reset phrase short-circuit ("új beteg" / "new case" / etc.).
    if is_explicit_reset_phrase(question):
        reset_tree_state(state)
        ack = ("OK, kitöröltem az aktuális folyamatot. Mit segítsek?"
               if _user_language(question) == "hu"
               else "OK, cleared the current flow. How can I help?")
        history = state["history"] + [
            {"role": "user",      "content": question},
            {"role": "assistant", "content": ack},
        ]
        max_messages = MAX_HISTORY_TURNS * 2
        if len(history) > max_messages:
            history = history[-max_messages:]
        state["history"] = history
        _log_query(
            chat_id=chat_id, user_message=question, recognized=None,
            retrieved_chunks=[], raw_llm=ack, final_response=ack,
            duration_ms=round((time.monotonic() - t_start) * 1000),
        )
        return ack

    normalized_question, recognized = normalize_question(question)

    # Tree dispatcher. May return a finished body to short-circuit the
    # RAG + main LLM call, or None to fall through to the standard flow.
    dispatch_body = dispatch_tree(state, recognized, question, normalized_question)
    if dispatch_body is not None:
        active = state.get("active_recognized")
        source_label = None
        if active:
            source_label = active.get("source_label")
        elif recognized:
            source_label = recognized.get("source_label")
        _pf = (active or recognized or {}).get("protocol_file")
        _parsed = PROTOCOL_PARSED_BY_FILE.get(normalize_path(_pf)) if _pf else None
        _footer = _parsed.get("default_footer") if _parsed else None
        answer = finalize_answer(dispatch_body, _footer, source_label)
        history = state["history"] + [
            {"role": "user",      "content": question},
            {"role": "assistant", "content": answer},
        ]
        max_messages = MAX_HISTORY_TURNS * 2
        if len(history) > max_messages:
            history = history[-max_messages:]
        state["history"] = history
        _log_query(
            chat_id=chat_id, user_message=question, recognized=recognized,
            retrieved_chunks=[], raw_llm=dispatch_body, final_response=answer,
            duration_ms=round((time.monotonic() - t_start) * 1000),
        )
        return answer

    # ----- Standard flow (carry-forward + RAG + LLM, unchanged from today) -----

    if recognized:
        # New drug/condition explicitly mentioned — update active context
        state["active_recognized"] = recognized
    else:
        # No alias in this message — reuse active context from conversation
        recognized = state["active_recognized"]
        if recognized:
            normalized_question = (
                question
                + f"\n\n[Continuing context: {recognized['display']} / {recognized['canonical']}]"
            )

    preferred_file = recognized.get("protocol_file") if recognized else None

    retrieved_chunks = search_protocols(
        normalized_question, top_k=3, preferred_file=preferred_file
    )

    # Expose only human-readable source_label to the model, never file paths
    context = "\n\n---\n\n".join(
        f"Source: {c['source_label']}\n{c['text']}"
        for c in retrieved_chunks
    )

    # Always prepend the matched protocol's gating header (ANSWER_POLICY,
    # DEFAULT_QUESTION, REQUIRED_INFORMATION, PATHWAY_PRIORITY).
    # Semantic search alone picks treatment-pathway chunks that crowd these
    # out, so the LLM never sees "ask patient status first" — and dumps
    # pathway info instead. Injecting the policy header guarantees the gate
    # is visible to the model.
    if preferred_file:
        policy_header = PROTOCOL_POLICY_BY_FILE.get(normalize_path(preferred_file), "")
        if policy_header:
            label = recognized.get("source_label", "") if recognized else ""
            context = (
                f"PROTOCOL GATING RULES (must be followed before any treatment info)\n"
                f"Source: {label}\n{policy_header}\n\n---\n\n"
                + context
            )

    system_prompt = build_system_prompt(recognized, context)

    history  = state["history"]
    messages = history + [{"role": "user", "content": question}]

    response = openai_client.chat.completions.create(
        model=CHAT_MODEL,
        messages=[{"role": "system", "content": system_prompt}] + messages,
    )

    raw_answer = response.choices[0].message.content

    source_label = recognized.get("source_label") if recognized else None
    _pf = recognized.get("protocol_file") if recognized else None
    _parsed = PROTOCOL_PARSED_BY_FILE.get(normalize_path(_pf)) if _pf else None
    _footer = _parsed.get("default_footer") if _parsed else None
    answer = finalize_answer(raw_answer, _footer, source_label)

    # Update conversation history, trim to MAX_HISTORY_TURNS pairs
    history = history + [
        {"role": "user",      "content": question},
        {"role": "assistant", "content": answer},
    ]
    max_messages = MAX_HISTORY_TURNS * 2
    if len(history) > max_messages:
        history = history[-max_messages:]
    state["history"] = history

    # Structured audit log — written after response is ready
    _log_query(
        chat_id       = chat_id,
        user_message  = question,
        recognized    = recognized,
        retrieved_chunks = retrieved_chunks,
        raw_llm       = raw_answer,
        final_response = answer,
        duration_ms   = round((time.monotonic() - t_start) * 1000),
    )

    return answer


# ---------------------------------------------------------------------------
# Debug output
# ---------------------------------------------------------------------------

def format_debug_output(retrieved_chunks):
    debug_text = "DEBUG — retrieved protocol chunks:\n\n"
    for i, chunk in enumerate(retrieved_chunks, start=1):
        preview = chunk["text"][:500].replace("\n", " ")
        debug_text += (
            f"{i}. Source label: {chunk['source_label']}\n"
            f"   Source file:  {chunk['source']}\n"
            f"   Similarity:   {chunk['similarity']:.4f}\n"
            f"   Preview: {preview}...\n\n"
        )
    return debug_text


# ---------------------------------------------------------------------------
# Telegram message splitting
# ---------------------------------------------------------------------------

def split_message(text, max_length=4000):
    """Split long messages at newlines to stay within Telegram's 4096-char limit."""
    chunks = []
    while len(text) > max_length:
        split_at = text.rfind("\n", 0, max_length)
        if split_at == -1:
            split_at = max_length
        chunks.append(text[:split_at])
        text = text[split_at:].strip()
    if text:
        chunks.append(text)
    return chunks


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_MESSAGE)


async def handle_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    CONVERSATION_STATE.pop(chat_id, None)
    await update.message.reply_text("Conversation history cleared.")


async def handle_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_user.id):
        await update.message.reply_text(
            "Ez a bot kórházi dolgozók számára érhető el. "
            "Ha jogosult vagy a hozzáférésre, kérj meghívót."
        )
        return
    chat_id = update.effective_chat.id
    # context.args contains words after /debug
    debug_question = " ".join(context.args).strip() if context.args else ""

    if not debug_question:
        await update.message.reply_text(
            "Please provide a question after /debug\nExample: /debug meropenem septic shock"
        )
        return

    normalized_question, recognized = normalize_question(debug_question)
    state  = get_chat_state(chat_id)
    active = state["active_recognized"]
    if not recognized and active:
        recognized = active

    preferred_file = recognized.get("protocol_file") if recognized else None
    retrieved_chunks = search_protocols(
        normalized_question, top_k=5, preferred_file=preferred_file
    )

    answer = ""
    if recognized:
        answer += (
            "DEBUG — recognized term:\n"
            f"Matched alias:  {recognized.get('matched_alias', '(carried from prior turn)')}\n"
            f"Normalized to:  {recognized['display']}\n"
            f"Source label:   {recognized['source_label']}\n"
            f"Protocol file:  {recognized.get('protocol_file', 'n/a')}\n"
            f"Confidence:     {recognized['confidence']}\n\n"
        )
    answer += format_debug_output(retrieved_chunks)

    for chunk in split_message(answer):
        await update.message.reply_text(chunk)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    question = update.message.text.strip()
    if not question:
        return

    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        await update.message.reply_text(
            "Ez a bot kórházi dolgozók számára érhető el. "
            "Ha jogosult vagy a hozzáférésre, kérj meghívót."
        )
        return

    chat_id = update.effective_chat.id
    await update.message.chat.send_action(action="typing")

    answer = ask_ai(question, chat_id)

    for chunk in split_message(answer):
        await update.message.reply_text(chunk)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    run_startup_checks()
    global _query_log, ALLOWED_USER_IDS
    ALLOWED_USER_IDS = _load_allowlist()
    if ALLOWED_USER_IDS:
        print(f"[startup] Allowlist active: {len(ALLOWED_USER_IDS)} user(s) authorised.")
    _query_log = setup_logging()
    logging.info("Bot starting up")
    print("Loading rule files...")
    load_rule_files()

    print("Loading aliases...")
    load_aliases("protocols/aliases.json")

    print("Loading protocols and generating embeddings (this may take a moment)...")
    load_protocols()

    print("Starting Telegram bot...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("reset", handle_reset))
    app.add_handler(CommandHandler("clear", handle_reset))
    app.add_handler(CommandHandler("debug", handle_debug))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot is running.")
    print("Commands: /reset or /clear to clear history, /debug <query> to inspect retrieval.")
    app.run_polling()


if __name__ == "__main__":
    main()
