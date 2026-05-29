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

try:
    from alias_sync import run_sync as _alias_sync
    ALIAS_SYNC_AVAILABLE = True
except ImportError:
    ALIAS_SYNC_AVAILABLE = False

try:
    from selection_engine import (
        run_selection,
        extract_slots_from_query,
        render_selected_output,
    )
    SELECTION_ENGINE_AVAILABLE = True
except ImportError:
    SELECTION_ENGINE_AVAILABLE = False
    print("[startup] WARNING: selection_engine.py not found — deterministic selection disabled")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY")

openai_client   = OpenAI(api_key=OPENAI_API_KEY)

from config import (
    SAFETY_FOOTER,
    START_MESSAGE,
    EMBEDDING_MODEL   as _CFG_EMBEDDING_MODEL,
    CHAT_MODEL        as _CFG_CHAT_MODEL,
    MAX_HISTORY_TURNS as _CFG_MAX_HISTORY_TURNS,
    CACHE_DB,
    LOG_FILE,
)

# Kept as module-level names for backward compatibility
EMBEDDING_MODEL   = _CFG_EMBEDDING_MODEL
CHAT_MODEL        = _CFG_CHAT_MODEL
MAX_HISTORY_TURNS = _CFG_MAX_HISTORY_TURNS
BOT_VERSION       = os.getenv("BOT_VERSION", "session-10")
LOCAL_DEBUG_MODE  = os.getenv("LOCAL_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

PROTOCOL_CHUNKS         = []
ALIASES                 = {}
ALIAS_INDEX             = {}
BLOCKED_ALIASES         = set()
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


def _load_admin_ids():
    """Return Telegram user IDs allowed to run admin-only commands."""
    raw = os.getenv("ADMIN_USER_IDS", "").strip()
    if not raw:
        return set()
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
        else:
            print(f"[admin] WARNING: ignoring non-numeric entry: {part!r}")
    return ids


ALLOWED_USER_IDS: set = set()  # populated at startup
ADMIN_USER_IDS: set = set()    # populated at startup


def _is_allowed(user_id: int) -> bool:
    """Return True if allowlist is empty (open) or user_id is in it."""
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS


def _is_admin(user_id: int) -> bool:
    """Return True when the user is explicitly listed as a bot admin."""
    return user_id in ADMIN_USER_IDS


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
        print("!! ALLOWED USERS NOT DEFINED !!")
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


def _safe_user_message_for_log(user_message):
    """Avoid PHI/raw prompt logging unless explicit local debug mode is enabled."""
    if LOCAL_DEBUG_MODE:
        return user_message
    return {
        "redacted": True,
        "length": len(user_message or ""),
        "sha256_12": hashlib.sha256((user_message or "").encode("utf-8")).hexdigest()[:12],
    }


def _safe_prompt_preview_for_stdout(user_message):
    if LOCAL_DEBUG_MODE:
        text = (user_message or "").replace("\n", " ").strip()
        return text[:117] + "..." if len(text) > 120 else text
    return "<redacted>"


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
            "user_message":  _safe_user_message_for_log(user_message),
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

    prompt_preview = _safe_prompt_preview_for_stdout(user_message)
    print(f"[Q] {chat_hash} | {prompt_preview!r} → {rec_str}", flush=True)
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


def get_source_label_for_file(file_path, text):
    metadata_label = extract_source_label_from_text(text)
    if metadata_label:
        return metadata_label
    normalized = normalize_path(file_path)
    if normalized in PROTOCOL_FILE_TO_LABEL:
        return PROTOCOL_FILE_TO_LABEL[normalized]
    return derive_source_label(file_path)


# ---------------------------------------------------------------------------
# Alias recognition
# ---------------------------------------------------------------------------

def load_aliases(path="protocols/aliases.json"):
    global ALIASES, ALIAS_INDEX, BLOCKED_ALIASES, PROTOCOL_FILE_TO_LABEL
    if not os.path.exists(path):
        print("No aliases.json found. Alias recognition disabled.")
        ALIASES = {}
        ALIAS_INDEX = {}
        BLOCKED_ALIASES = set()
        PROTOCOL_FILE_TO_LABEL = {}
        return
    with open(path, "r", encoding="utf-8") as f:
        ALIASES = json.load(f)
    ALIAS_INDEX, BLOCKED_ALIASES, PROTOCOL_FILE_TO_LABEL = _build_alias_index(ALIASES)
    print(f"Loaded {len(ALIAS_INDEX)} aliases")
    if not RAPIDFUZZ_AVAILABLE:
        print("rapidfuzz not installed — fuzzy matching disabled, exact matching only.")


def _build_alias_index(alias_data):
    alias_index = {}
    blocked_aliases = {
        term.lower()
        for term in alias_data.get("blocked_aliases", [])
        if isinstance(term, str) and term.strip()
    }
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
    return alias_index, blocked_aliases, protocol_file_to_label


def _alias_term_matches(term, text):
    return re.search(r"\b" + re.escape(term) + r"\b", text) is not None


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
    for blocked_alias in sorted(BLOCKED_ALIASES, key=len, reverse=True):
        if _alias_term_matches(blocked_alias, text):
            return question, None

    if not ALIAS_INDEX:
        return question, None

    # ── 1. Substring exact — longest alias first to avoid short-alias collisions
    for alias in sorted(ALIAS_INDEX.keys(), key=len, reverse=True):
        data = ALIAS_INDEX[alias]
        matched = _alias_term_matches(alias, text)
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
# Protocol parser — extracted to protocol_parser.py
# All names re-exported here for backward compatibility.
# ---------------------------------------------------------------------------

from protocol_parser import (
    CANONICAL_PANELS,
    POLICY_SECTIONS,
    VALID_ANSWER_MODES,
    VALID_SELECTION_MODES,
    _PANEL_HEADER_RE,
    _NONE_BODY_RE,
    parse_protocol_file,
    _parse_protocol_text,
    _parse_metadata_block,
    _parse_links_block,
    _parse_protocol_links,
    parse_decision_tree,
    extract_policy_header,
)

# ---------------------------------------------------------------------------
# Response post-processing — extracted to postprocess.py; re-imported here
# for backward compatibility.
# ---------------------------------------------------------------------------

from postprocess import (
    _clean_body,
    apply_footer,
    clean_response,
    finalize_answer,
)

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
            # Session 8: extended routing state
            "active_protocol_id":               None,
            "protocol_type":                    None,
            "last_user_intent":                 None,
            "collected_slots":                  {},
            "pending_question":                 None,
            "last_recommended_antibiotics":     [],
            "dosing_allowed":                   None,
            "linked_dosing_protocol_available": None,
            "context_source":                   None,  # "fresh_alias" | "carried_context"
        }
    state = CONVERSATION_STATE[chat_id]
    state.setdefault("tree", None)
    state.setdefault("pending_topic_switch", None)
    state.setdefault("pending_links", None)
    # Backfill session-8 fields for states created before this version
    state.setdefault("active_protocol_id", None)
    state.setdefault("protocol_type", None)
    state.setdefault("last_user_intent", None)
    state.setdefault("collected_slots", {})
    state.setdefault("pending_question", None)
    state.setdefault("last_recommended_antibiotics", [])
    state.setdefault("dosing_allowed", None)
    state.setdefault("linked_dosing_protocol_available", None)
    state.setdefault("context_source", None)
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
    # Clear session-8 per-flow routing state
    state["last_user_intent"] = None
    state["collected_slots"] = {}
    state["pending_question"] = None
    state["last_recommended_antibiotics"] = []
    state["context_source"] = None


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



# ---------------------------------------------------------------------------
# Intent classification and routing helpers (Session 8)
# ---------------------------------------------------------------------------

_DOSING_INTENT_RE = re.compile(
    r"\b(dose|dosing|adag|dózis|dozis|mennyi|how much|adagolás|"
    r"pump|infusion|infúzió|GFR|eGFR|CrCl|CRRT|IHD|renal|vesefunkció)\b",
    re.IGNORECASE,
)

_SELECTION_INTENT_RE = re.compile(
    r"\b(hospitali\w+|intubat\w*|ICU|dischargeable|hazaengedhet|critical|"
    r"nosocomial|nosokomialis|viral|outpatient|járóbeteg|pathway|"
    r"mit adjak|what.*give|what.*treat|which.*antibiotic|melyik)\b",
    re.IGNORECASE,
)

_INFO_INTENT_RE = re.compile(
    r"\b(toxicity|toxicitás|monitoring|monitorozás|TDM|adverse|mellékhatás|"
    r"side effect|interaction|interakció|contraindic|ellenjavallat|"
    r"administration|preparation|beadás|készítés)\b",
    re.IGNORECASE,
)

# A "bare dosing request" has dosing keywords but no drug name resolved,
# OR the currently active protocol does not do dosing itself.
_BARE_DOSING_RE = re.compile(
    r"^\s*(dose|dosing|adag|dózis|dozis|mennyi|how much|"
    r"adagolás|what dose|what.?s the dose|mi az adag)\s*[\?!.]*\s*$",
    re.IGNORECASE,
)

# Drug names extracted from response text — multi-word names first (longest match).
# Populated at startup from the links in loaded protocols; see _build_drug_name_set().
_KNOWN_DRUG_NAMES: list[str] = []


def _build_drug_name_set():
    """Collect drug names from all loaded protocol LINKS so we can detect
    them in response text. Called once after load_protocols()."""
    global _KNOWN_DRUG_NAMES
    names: set[str] = set()
    for parsed in PROTOCOL_PARSED_BY_FILE.values():
        for lentry in (parsed.get("links") or {}).values():
            item = lentry.get("when_selected_item", "")
            if item:
                names.add(item.lower())
    # Longest first so multi-word names match before single-word fragments
    _KNOWN_DRUG_NAMES = sorted(names, key=len, reverse=True)


def classify_intent(question: str) -> str:
    """Classify the user's turn intent.

    Returns one of:
      dosing_request | selection_request | info_request | link_request |
      reset | unknown
    """
    if is_explicit_reset_phrase(question):
        return "reset"
    if _DOSING_INTENT_RE.search(question):
        return "dosing_request"
    if _SELECTION_INTENT_RE.search(question):
        return "selection_request"
    if _INFO_INTENT_RE.search(question):
        return "info_request"
    return "unknown"


def _extract_drug_mentions(text: str, drug_names: list[str]) -> list[str]:
    """Return drug names (from drug_names) that appear in text (case-insensitive)."""
    text_l = text.lower()
    found = []
    for name in drug_names:
        if re.search(r"\b" + re.escape(name) + r"\b", text_l):
            if name not in found:
                found.append(name)
    return found


def _get_protocol_links(protocol_file: str) -> dict:
    """Return the LINKS dict for a protocol file, or {}."""
    if not protocol_file:
        return {}
    parsed = PROTOCOL_PARSED_BY_FILE.get(normalize_path(protocol_file))
    return parsed.get("links", {}) if parsed else {}


def _dosing_protocol_available(link_entry: dict) -> bool:
    """True if the linked dosing protocol file actually exists on disk."""
    tf = link_entry.get("target_file", "")
    if not tf:
        return False
    return os.path.exists(tf)


def _handle_dosing_shortcut(state: dict, question: str, recognized) -> str | None:
    """Handle a bare 'dose?' or dosing follow-up.

    Rules:
    - Not a bare dosing request → return None (fall through to standard flow).
    - No last_recommended_antibiotics → return None (let standard flow handle).
    - >1 recommended antibiotic → ask which one.
    - Exactly 1: check linked dosing protocol.
        - Exists → return None (let standard flow proceed with the drug context).
        - Missing → return target_missing_behavior message.
    """
    if not _BARE_DOSING_RE.match(question):
        return None

    last = state.get("last_recommended_antibiotics", [])
    if not last:
        return None

    lang = _user_language(question)

    if len(last) > 1:
        names = " / ".join(last)
        return (
            f"Melyik szer dózisa? Utoljára ajánlott: {names}"
            if lang == "hu"
            else f"Dose of which drug? Last recommendation included: {names}"
        )

    # Exactly one drug
    drug = last[0]
    active_file = (state.get("active_recognized") or {}).get("protocol_file", "")
    links = _get_protocol_links(active_file)

    # Find link entry whose when_selected_item matches the drug
    matching_entry = None
    for lentry in links.values():
        if lentry.get("when_selected_item", "").lower() == drug.lower():
            matching_entry = lentry
            break

    if matching_entry is None:
        # Protocol has no dosing link for this drug
        return (
            f"A {drug} dózisát ez a protokoll nem tartalmazza."
            if lang == "hu"
            else f"Dosing for {drug} is not covered by this protocol."
        )

    if _dosing_protocol_available(matching_entry):
        # Protocol file exists — fall through to standard flow
        # (which will carry the drug context and retrieve the right protocol)
        return None

    # File missing — use the target_missing_behavior
    fallback = matching_entry.get(
        "target_missing_behavior",
        f"{drug.capitalize()} dosing is not specified in the uploaded protocols."
    )
    return fallback


def _needs_organism_disambiguation(state: dict, recognized) -> bool:
    """True when the recognized protocol is a microbiology interpretation
    type but the active context is absent or a different type, AND the
    matched confidence is high/exact (i.e. this looks like a deliberate
    organism mention, not a sidebar in a different conversation)."""
    if not recognized:
        return False
    rec_type = recognized.get("protocol_type", "")
    if rec_type != "microbiology_interpretation_protocol":
        return False
    # Check active context
    active = state.get("active_recognized")
    if not active:
        return True  # No context → ambiguous
    active_type = active.get("protocol_type", "")
    if active_type == "microbiology_interpretation_protocol":
        return False  # Already in microbiology context → no disambiguation needed
    # Active context is a different type (e.g. CAP pathway) → disambiguate
    return True


def _handle_organism_disambiguation(state: dict, question: str, recognized) -> str | None:
    """Return a disambiguation prompt if the query looks like a lone organism
    name with ambiguous context, or None to fall through."""
    if not _needs_organism_disambiguation(state, recognized):
        return None

    lang = _user_language(question)
    organism = recognized.get("display", question.strip())
    return (
        f"\"{organism}\" — BioFire/PCR eredmény értelmezésre kérdezed, "
        f"vagy keresni akarod a megfelelő antibiotikumot a kórokozóhoz?\n"
        f"(Is this a BioFire/PCR result to interpret, "
        f"or do you want the appropriate antibiotic for this organism?)"
        if lang == "hu"
        else
        f"\"{organism}\" — is this a BioFire/PCR result you want interpreted, "
        f"or are you looking for the appropriate antibiotic to cover this organism?"
    )


def _update_routing_state(state: dict, recognized, context_source: str):
    """Sync protocol-level routing fields from recognized metadata."""
    if not recognized:
        return
    state["active_protocol_id"] = recognized.get("protocol_id") or recognized.get("display")
    # Look up protocol_type from parsed protocol
    pf = recognized.get("protocol_file", "")
    parsed = PROTOCOL_PARSED_BY_FILE.get(normalize_path(pf)) if pf else None
    if parsed:
        meta = parsed.get("metadata", {})
        state["protocol_type"] = meta.get("protocol_type", "")
        state["dosing_allowed"] = meta.get("allows_dosing", "yes").lower() == "yes"
        # Check if any linked dosing protocol files actually exist
        links = parsed.get("links", {})
        state["linked_dosing_protocol_available"] = any(
            _dosing_protocol_available(le) for le in links.values()
        )
    state["context_source"] = context_source


def _update_recommended_antibiotics(state: dict, response_text: str, recognized):
    """After a response, record which drugs were recommended.

    Looks for drug names from the active protocol's LINKS entries in the
    response text. More reliable than open-ended NLP extraction.
    """
    pf = (recognized or {}).get("protocol_file", "")
    if not pf:
        pf = (state.get("active_recognized") or {}).get("protocol_file", "")
    links = _get_protocol_links(pf)
    drug_names = [
        le["when_selected_item"].lower()
        for le in links.values()
        if le.get("when_selected_item")
    ]
    if not drug_names:
        return
    found = _extract_drug_mentions(response_text, drug_names)
    if found:
        state["last_recommended_antibiotics"] = found


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
# Deterministic selection engine integration (Session 9)
# ---------------------------------------------------------------------------

def _try_deterministic_selection(state, recognized, question, lang):
    """Run deterministic selection before RAG. Returns rendered body or None."""
    if not SELECTION_ENGINE_AVAILABLE:
        return None
    if not recognized:
        return None
    protocol_file = recognized.get("protocol_file", "")
    if not protocol_file:
        return None
    parsed = PROTOCOL_PARSED_BY_FILE.get(normalize_path(protocol_file))
    if not parsed:
        return None
    meta = parsed.get("metadata", {})
    mode = meta.get("selection_mode", "none")
    if mode in ("none", "decision_tree", ""):
        return None
    existing = dict(state.get("collected_slots", {}))
    slots = extract_slots_from_query(question, parsed_protocol=parsed, existing_slots=existing)
    state["collected_slots"] = slots
    result = run_selection(parsed, slots, lang=lang)
    if result.no_match:
        return None
    if result.missing_slots and not result.default_used:
        ask_text = result.ask_missing
        if not ask_text:
            missing_str = ", ".join(result.missing_slots)
            ask_text = (f"Hiányzó adatok: {missing_str}. Kérlek küldd el."
                        if lang == "hu" else f"Missing: {missing_str}. Please provide.")
        if result.default_used:
            da = _pick_default_answer_text(parsed, lang)
            if da:
                ask_text = ask_text + "\n\n" + da
        return ask_text
    if result.missing_slots and result.default_used:
        da = _pick_default_answer_text(parsed, lang)
        missing_str = ", ".join(result.missing_slots)
        return (f"Hiányzó adatok: {missing_str}.\n\n{da}" if lang == "hu"
                else f"Missing: {missing_str}.\n\n{da}")
    rendered = render_selected_output(parsed, result, lang=lang)
    if not rendered:
        return None
    selected_items = result.output_data.get("selected_items") or result.output_data.get("selected_item")
    if selected_items:
        if isinstance(selected_items, str):
            selected_items = [selected_items]
        valid = [s for s in selected_items if s not in (
            "antibiotics_not_required","supportive_therapy",
            "infectious_diseases_consultation","highest_required_tier_agent")]
        if valid:
            state["last_recommended_antibiotics"] = valid
    return rendered


def _pick_default_answer_text(parsed, lang):
    """Return HU or EN section from DEFAULT_ANSWER panel."""
    da = parsed.get("default_answer", "")
    if not da:
        return ""
    import re as _re
    section_re = _re.compile(r"^###\s+(HU|EN)\s*$", re.MULTILINE)
    matches = list(section_re.finditer(da))
    preferred = "HU" if lang == "hu" else "EN"
    fallback  = "EN" if lang == "hu" else "HU"
    for label in (preferred, fallback):
        for i, m in enumerate(matches):
            if m.group(1) == label:
                start = m.end()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(da)
                return da[start:end].strip()
    return da.strip()


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

    # -- Intent classification (Session 8) ---
    state["last_user_intent"] = classify_intent(question)

    # -- Context source tracking ---
    if recognized:
        _update_routing_state(state, recognized, "fresh_alias")

    # -- Organism-only disambiguation ---
    organism_prompt = _handle_organism_disambiguation(state, question, recognized)
    if organism_prompt is not None:
        source_label = recognized.get("source_label") if recognized else None
        answer = finalize_answer(organism_prompt, None, source_label)
        history = state["history"] + [
            {"role": "user",      "content": question},
            {"role": "assistant", "content": answer},
        ]
        state["history"] = history[-MAX_HISTORY_TURNS * 2:]
        _log_query(
            chat_id=chat_id, user_message=question, recognized=recognized,
            retrieved_chunks=[], raw_llm=organism_prompt, final_response=answer,
            duration_ms=round((time.monotonic() - t_start) * 1000),
        )
        return answer

    # -- Bare dosing shortcut ("dose?" after a recommendation) ---
    dosing_shortcut = _handle_dosing_shortcut(state, question, recognized)
    if dosing_shortcut is not None:
        active = state.get("active_recognized")
        source_label = (recognized or active or {}).get("source_label")
        _pf = (recognized or active or {}).get("protocol_file")
        _parsed_ds = PROTOCOL_PARSED_BY_FILE.get(normalize_path(_pf)) if _pf else None
        _footer_ds = _parsed_ds.get("default_footer") if _parsed_ds else None
        answer = finalize_answer(dosing_shortcut, _footer_ds, source_label)
        history = state["history"] + [
            {"role": "user",      "content": question},
            {"role": "assistant", "content": answer},
        ]
        state["history"] = history[-MAX_HISTORY_TURNS * 2:]
        _log_query(
            chat_id=chat_id, user_message=question, recognized=recognized,
            retrieved_chunks=[], raw_llm=dosing_shortcut, final_response=answer,
            duration_ms=round((time.monotonic() - t_start) * 1000),
        )
        return answer

    # -- Non-tree fresh-alias protocol switch ---
    if (recognized
            and state.get("active_recognized")
            and not state.get("tree")
            and recognized["protocol_file"] != state["active_recognized"].get("protocol_file")
            and recognized.get("confidence") in ("exact", "high")):
        old_label = _label_for_protocol(state["active_recognized"].get("protocol_file", ""))
        new_label = recognized.get("display") or _label_for_protocol(recognized.get("protocol_file", ""))
        print(f"[routing] non-tree switch: {old_label} -> {new_label}")
        state["active_recognized"] = recognized
        state["last_recommended_antibiotics"] = []
        _update_routing_state(state, recognized, "fresh_alias")

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

    # ----- Deterministic selection engine (Session 9) -----
    lang = _user_language(question)
    _active_for_det = recognized or state.get("active_recognized")
    det_body = _try_deterministic_selection(state, _active_for_det, question, lang)
    if det_body is not None:
        active = state.get("active_recognized")
        source_label = (active or recognized or {}).get("source_label")
        _pf = (active or recognized or {}).get("protocol_file")
        _parsed_det = PROTOCOL_PARSED_BY_FILE.get(normalize_path(_pf)) if _pf else None
        _footer_det = _parsed_det.get("default_footer") if _parsed_det else None
        answer = finalize_answer(det_body, _footer_det, source_label)
        history = state["history"] + [
            {"role": "user",      "content": question},
            {"role": "assistant", "content": answer},
        ]
        state["history"] = history[-(MAX_HISTORY_TURNS * 2):]
        _log_query(chat_id=chat_id, user_message=question,
                   recognized=recognized or state.get("active_recognized"),
                   retrieved_chunks=[], raw_llm=det_body, final_response=answer,
                   duration_ms=round((time.monotonic() - t_start) * 1000))
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

    # Track recommended antibiotics for "what dose?" follow-up (Session 8)
    _update_recommended_antibiotics(state, answer, recognized)

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

def _protocol_meta_for_file(protocol_file):
    if not protocol_file:
        return {}
    parsed = PROTOCOL_PARSED_BY_FILE.get(normalize_path(protocol_file))
    return parsed.get("metadata", {}) if parsed else {}


def _protocol_for_file(protocol_file):
    if not protocol_file:
        return None
    return PROTOCOL_PARSED_BY_FILE.get(normalize_path(protocol_file))


def _loaded_protocol_rows():
    rows = []
    seen = set()
    for path, parsed in PROTOCOL_PARSED_BY_FILE.items():
        if path in seen:
            continue
        seen.add(path)
        meta = parsed.get("metadata", {}) if parsed else {}
        if not meta.get("protocol_id"):
            continue
        rows.append({
            "protocol_id":   meta.get("protocol_id"),
            "source_label":  meta.get("source_label") or _label_for_protocol(path),
            "protocol_type": meta.get("protocol_type") or "n/a",
            "status":        meta.get("status") or "n/a",
            "version":       meta.get("version") or "n/a",
        })
    return sorted(rows, key=lambda r: (r["protocol_id"], r["source_label"]))


def get_protocol_library_version():
    env_version = os.getenv("PROTOCOL_LIBRARY_VERSION", "").strip()
    if env_version:
        return env_version
    versions = sorted({
        (parsed.get("metadata", {}) or {}).get("version", "").strip()
        for parsed in PROTOCOL_PARSED_BY_FILE.values()
        if (parsed.get("metadata", {}) or {}).get("version", "").strip()
    })
    if not versions:
        return "unavailable"
    if len(versions) == 1:
        return versions[0]
    return "mixed: " + ", ".join(versions)


def format_protocols_output():
    rows = _loaded_protocol_rows()
    if not rows:
        return "Loaded protocols: none"
    lines = ["Loaded protocols:"]
    for row in rows:
        lines.append(
            f"- {row['protocol_id']} | {row['source_label']} | "
            f"{row['protocol_type']} | {row['status']} | {row['version']}"
        )
    return "\n".join(lines)


def format_version_output():
    return (
        f"Bot version: {BOT_VERSION}\n"
        f"Protocol library version: {get_protocol_library_version()}"
    )


def _detect_chunk_section(text):
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("##"):
            return stripped.lstrip("#").strip() or "n/a"
    return "n/a"


def format_debug_output(retrieved_chunks):
    debug_text = "DEBUG - retrieved protocol chunks:\n\n"
    if not retrieved_chunks:
        return debug_text + "No chunks retrieved.\n"
    for i, chunk in enumerate(retrieved_chunks, start=1):
        debug_text += (
            f"{i}. File: {chunk.get('source', 'n/a')}\n"
            f"   Source label: {chunk.get('source_label', 'n/a')}\n"
            f"   Section: {chunk.get('section') or _detect_chunk_section(chunk.get('text', ''))}\n"
            f"   Similarity: {chunk.get('similarity', 0.0):.4f}\n\n"
        )
    return debug_text


def _format_recognized_summary(recognized):
    if not recognized:
        return "none"
    meta = _protocol_meta_for_file(recognized.get("protocol_file", ""))
    protocol_id = meta.get("protocol_id") or recognized.get("protocol_id") or recognized.get("display", "n/a")
    protocol_type = meta.get("protocol_type") or recognized.get("protocol_type") or "n/a"
    return (
        f"{protocol_id} ({recognized.get('source_label') or meta.get('source_label') or 'n/a'}, "
        f"type={protocol_type})"
    )


def _format_state_snapshot(state):
    tree = state.get("tree") or {}
    pending_links = state.get("pending_links") or []
    return (
        f"active={_format_recognized_summary(state.get('active_recognized'))}; "
        f"intent={state.get('last_user_intent') or 'n/a'}; "
        f"context_source={state.get('context_source') or 'n/a'}; "
        f"tree_node={tree.get('current_node') or 'none'}; "
        f"pending_links={len(pending_links)}"
    )


def _format_selected_protocol(recognized, reason):
    if not recognized:
        return f"Selected protocol: none\nReason: {reason}"
    meta = _protocol_meta_for_file(recognized.get("protocol_file", ""))
    return (
        f"Selected protocol: {meta.get('protocol_id') or recognized.get('display', 'n/a')}\n"
        f"Source label: {meta.get('source_label') or recognized.get('source_label', 'n/a')}\n"
        f"Protocol file: {recognized.get('protocol_file', 'n/a')}\n"
        f"Protocol type: {meta.get('protocol_type') or recognized.get('protocol_type') or 'n/a'}\n"
        f"Reason: {reason}"
    )


def _format_links(parsed):
    if not parsed:
        return "Linked protocols: none"
    links = parsed.get("links") or {}
    old_links = parsed.get("protocol_links") or {}
    if not links and not old_links:
        return "Linked protocols: none"
    lines = ["Linked protocols:"]
    for label, entry in sorted(links.items()):
        target_file = entry.get("target_file", "")
        available = bool(target_file and _protocol_for_file(target_file))
        target_id = entry.get("target_protocol_id") or label
        lines.append(f"- {label}: {target_id} -> {target_file or 'n/a'} (available={available})")
    for label, entry in sorted(old_links.items()):
        target_file = entry.get("file", "")
        available = bool(target_file and _protocol_for_file(target_file))
        lines.append(f"- {label}: {target_file or 'n/a'} (available={available})")
    return "\n".join(lines)


def _format_tree_node(state, parsed):
    active_tree = state.get("tree")
    if active_tree:
        node_id = active_tree.get("current_node")
        node = _get_node(parsed, node_id) if parsed else None
        node_type = node.get("type") if node else "unknown"
        return f"Tree node: active {node_id or 'n/a'} (type={node_type})"
    tree = parsed.get("decision_tree") if parsed else None
    if tree and tree.get("root"):
        return f"Tree node: inactive; selected protocol root={tree['root']}"
    return "Tree node: none"


def _format_missing_fields(selection_result, parsed):
    missing = []
    if selection_result is not None:
        missing.extend(selection_result.missing_slots or [])
    required_info = (parsed or {}).get("required_information") or ""
    if not missing and required_info.strip():
        missing.append("see REQUIRED_INFORMATION panel")
    if not missing:
        return "Missing fields: none detected"
    return "Missing fields: " + ", ".join(str(item) for item in missing)


def _inspect_deterministic_path(question, recognized, state):
    if not SELECTION_ENGINE_AVAILABLE or not recognized:
        return None, {}, "LLM-generated RAG path"
    parsed = _protocol_for_file(recognized.get("protocol_file", ""))
    if not parsed:
        return None, {}, "LLM-generated RAG path"
    meta = parsed.get("metadata", {})
    mode = meta.get("selection_mode", "none").lower()
    if mode == "decision_tree":
        return None, dict(state.get("collected_slots", {})), "deterministic decision-tree dispatcher"
    if mode in ("", "none"):
        return None, dict(state.get("collected_slots", {})), "LLM-generated RAG path"
    slots = extract_slots_from_query(
        question,
        parsed_protocol=parsed,
        existing_slots=dict(state.get("collected_slots", {})),
    )
    result = run_selection(parsed, slots, lang=_user_language(question))
    if result.no_match:
        return result, slots, "LLM-generated RAG path (deterministic engine no-match)"
    return result, slots, f"deterministic selection_engine ({result.mode_used})"


def build_debug_trace(debug_question, chat_id):
    normalized_question, fresh_recognized = normalize_question(debug_question)
    state = get_chat_state(chat_id)
    state_before = _format_state_snapshot(state)
    active = state.get("active_recognized")
    recognized = fresh_recognized

    if fresh_recognized:
        context_source = "fresh_alias"
        reason = (
            f"fresh alias '{fresh_recognized.get('matched_alias', 'n/a')}' "
            f"matched with {fresh_recognized.get('confidence', 'n/a')} confidence"
        )
    elif active:
        recognized = active
        context_source = "carried_context"
        reason = "no fresh alias; using active protocol carried from prior state"
        normalized_question = (
            debug_question
            + f"\n\n[Continuing context: {recognized.get('display', '')} / "
            + f"{recognized.get('canonical', recognized.get('display', ''))}]"
        )
    else:
        context_source = "none"
        reason = "no alias or active protocol; retrieval is semantic only"

    preferred_file = recognized.get("protocol_file") if recognized else None
    retrieved_chunks = search_protocols(
        normalized_question, top_k=5, preferred_file=preferred_file
    )
    parsed = _protocol_for_file(preferred_file)
    meta = parsed.get("metadata", {}) if parsed else {}
    selection_result, slots, output_source = _inspect_deterministic_path(
        debug_question, recognized, state
    )
    selected_key = getattr(selection_result, "output_key", None) if selection_result else None
    default_used = getattr(selection_result, "default_used", None) if selection_result else None

    lines = [
        "DEBUG - routing trace",
        "Authorization: allowed",
        f"Context source: {context_source}",
        f"Matched alias: {(fresh_recognized or {}).get('matched_alias') or 'none'}",
        f"Confidence: {(fresh_recognized or recognized or {}).get('confidence', 'n/a')}",
        f"Protocol type: {meta.get('protocol_type') or (recognized or {}).get('protocol_type') or 'n/a'}",
        f"Intent: {classify_intent(debug_question)}",
        f"Active state before: {state_before}",
        f"Active state after: {_format_state_snapshot(state)}",
        _format_selected_protocol(recognized, reason),
        f"Deterministic/LLM source: {output_source}",
        f"Selection output: {selected_key or ('default' if default_used else 'n/a')}",
        "Collected slots: " + (json.dumps(slots, ensure_ascii=False, sort_keys=True) if slots else "none"),
        _format_missing_fields(selection_result, parsed),
        "Blocked output: no authorization block",
        _format_tree_node(state, parsed),
        _format_links(parsed),
        "",
        format_debug_output(retrieved_chunks).rstrip(),
    ]
    return "\n".join(lines)


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


async def handle_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id if update.effective_user else None
    await update.message.reply_text(f"Telegram user id: {user_id}")


async def handle_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    CONVERSATION_STATE.pop(chat_id, None)
    await update.message.reply_text("Conversation history cleared.")


async def handle_protocols(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_user.id):
        await update.message.reply_text(
            "Ez a bot kórházi dolgozók számára érhető el. "
            "Ha jogosult vagy a hozzáférésre, kérj meghívót."
        )
        return
    for chunk in split_message(format_protocols_output()):
        await update.message.reply_text(chunk)


async def handle_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_user.id):
        await update.message.reply_text(
            "Ez a bot kórházi dolgozók számára érhető el. "
            "Ha jogosult vagy a hozzáférésre, kérj meghívót."
        )
        return
    await update.message.reply_text(format_version_output())


async def handle_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_user.id) or not _is_admin(update.effective_user.id):
        await update.message.reply_text("Authorization: blocked for admin command.")
        return
    await update.message.reply_text(
        "Reload deferred. TODO: implement an atomic admin-only reload that rebuilds "
        "aliases, parsed protocols, chunks, and embeddings without serving a half-loaded state."
    )


async def handle_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update.effective_user.id):
        await update.message.reply_text(
            "DEBUG - routing trace\n"
            "Authorization: blocked\n"
            "Blocked output: user is not authorized to run debug commands."
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

    answer = build_debug_trace(debug_question, chat_id)

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
    global _query_log, ALLOWED_USER_IDS, ADMIN_USER_IDS
    ALLOWED_USER_IDS = _load_allowlist()
    ADMIN_USER_IDS = _load_admin_ids()
    if ALLOWED_USER_IDS:
        print(f"[startup] Allowlist active: {len(ALLOWED_USER_IDS)} user(s) authorised.")
    if ADMIN_USER_IDS:
        print(f"[startup] Admin commands enabled for {len(ADMIN_USER_IDS)} user(s).")
    _query_log = setup_logging()
    logging.info("Bot starting up")
    print("Loading rule files...")
    load_rule_files()

    if ALIAS_SYNC_AVAILABLE:
        print("Syncing aliases...")
        _alias_sync()
    print("Loading aliases...")
    load_aliases("protocols/aliases.json")

    print("Loading protocols and generating embeddings (this may take a moment)...")
    load_protocols()
    _build_drug_name_set()

    # Run protocol linter — warning-only, does not block startup
    print("Running protocol linter...")
    try:
        from protocol_linter import run_linter, print_report
        _lint_result = run_linter(proto_dir="protocols")
        _lint_warnings = _lint_result.warnings()
        if _lint_warnings:
            print(f"[linter] {len(_lint_warnings)} warning(s) — run 'python -m protocol_linter' for full report")
            for _w in _lint_warnings[:5]:
                print(f"  [linter] {_w.protocol}: [{_w.code}] {_w.message}")
            if len(_lint_warnings) > 5:
                print(f"  [linter] ... and {len(_lint_warnings) - 5} more")
        else:
            print("[linter] All protocols clean.")
    except Exception as _lint_err:
        print(f"[linter] WARNING: linter failed to run: {_lint_err}")

    print("Starting Telegram bot...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CommandHandler("whoami", handle_whoami))
    app.add_handler(CommandHandler("protocols", handle_protocols))
    app.add_handler(CommandHandler("version", handle_version))
    app.add_handler(CommandHandler("reload", handle_reload))
    app.add_handler(CommandHandler("reset", handle_reset))
    app.add_handler(CommandHandler("clear", handle_reset))
    app.add_handler(CommandHandler("debug", handle_debug))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot is running.")
    print("Commands: /whoami, /protocols, /version, /reset, /clear, /debug <query>.")
    app.run_polling()


if __name__ == "__main__":
    main()
