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

    ts = __import__("datetime").datetime.utcnow().isoformat() + "Z"
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
            matched = alias in text
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


def clean_response(text, source_label):
    text = _BOLD_RE.sub(r'\1', text)            # 1. strip bold
    text = _SOURCE_LINE_RE.sub('', text)        # 2. remove model-generated source lines
    text = _FILE_PATH_RE.sub('', text)          # 3. remove stray file paths
    if _HAS_DOSING_RE.search(text):
        text = _NOT_SPEC_RE.sub('', text)       # 4. remove contradictory "not specified"
    text = _BLANK_RE.sub('\n\n', text).strip()  # 5. tidy blank lines
    if source_label:
        text = text + f'\n\nSource: {source_label}{SAFETY_FOOTER}'   # 6. append correct source
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


def get_chat_state(chat_id):
    if chat_id not in CONVERSATION_STATE:
        CONVERSATION_STATE[chat_id] = {"history": [], "active_recognized": None}
    return CONVERSATION_STATE[chat_id]


def ask_ai(question, chat_id):
    t_start = time.monotonic()
    state   = get_chat_state(chat_id)

    normalized_question, recognized = normalize_question(question)

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
    answer = clean_response(raw_answer, source_label)

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
