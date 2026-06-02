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
import unicodedata
from logging.handlers import RotatingFileHandler
from pathlib import Path

import aliases as alias_helpers
import authorization as authorization_helpers
import logging_audit as audit_helpers
import retrieval as retrieval_helpers
import state as state_helpers
import numpy as np
from openai import OpenAI
from routing import AnswerEnvelope, TurnContext
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
    get_runtime_options,
)

# Kept as module-level names for backward compatibility
EMBEDDING_MODEL   = _CFG_EMBEDDING_MODEL
CHAT_MODEL        = _CFG_CHAT_MODEL
MAX_HISTORY_TURNS = _CFG_MAX_HISTORY_TURNS
BOT_VERSION       = os.getenv("BOT_VERSION", "session-10")
LOCAL_DEBUG_MODE  = os.getenv("LOCAL_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}
RUNTIME_OPTIONS   = get_runtime_options()
ACCESS_MODE       = RUNTIME_OPTIONS.get("access_mode", "closed")
FULL_CONVERSATION_LOG = False
SAFE_RUNTIME_FAILURE_MESSAGE = (
    "I could not safely complete that request because an external service failed. "
    "Please retry in a moment, and use local clinical review if the decision is urgent."
)
_CACHE_DISABLED = False


def _env_flag(name):
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _local_debug_enabled():
    return _env_flag("LOCAL_DEBUG")


def _runtime_log_user_messages_enabled():
    return bool(RUNTIME_OPTIONS.get("log_user_messages"))


def _full_conversation_log_enabled():
    return (
        _local_debug_enabled()
        or _env_flag("FULL_CONVERSATION_LOG")
        or _runtime_log_user_messages_enabled()
    )


def _effective_access_mode():
    if _local_debug_enabled() and not (
        RUNTIME_OPTIONS.get("_access_mode_explicit")
    ):
        return "open"
    return str(RUNTIME_OPTIONS.get("access_mode") or "closed").strip().lower()


def _refresh_runtime_settings():
    global RUNTIME_OPTIONS, ACCESS_MODE, FULL_CONVERSATION_LOG
    RUNTIME_OPTIONS = get_runtime_options()
    ACCESS_MODE = _effective_access_mode()
    FULL_CONVERSATION_LOG = _full_conversation_log_enabled()
    return RUNTIME_OPTIONS


_refresh_runtime_settings()

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

PROTOCOL_CHUNKS         = []
ALIASES                 = {}
ALIAS_INDEX             = {}
BLOCKED_ALIASES         = set()
UNSUPPORTED_SYNDROMES   = {}
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
state_helpers.bind_state(CONVERSATION_STATE)


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
    return authorization_helpers._load_allowlist(RUNTIME_OPTIONS)


def _load_admin_ids():
    return authorization_helpers._load_admin_ids(RUNTIME_OPTIONS)


ALLOWED_USER_IDS: set = set()  # populated at startup
ADMIN_USER_IDS: set = set()    # populated at startup


def _is_allowed(user_id: int) -> bool:
    if _effective_access_mode() == "open":
        return True
    if not ALLOWED_USER_IDS:
        return False
    return authorization_helpers.is_allowed(user_id, ALLOWED_USER_IDS)


def _is_admin(user_id: int) -> bool:
    return authorization_helpers.is_admin(user_id, ADMIN_USER_IDS)


def _should_run_alias_sync_on_startup():
    return ALIAS_SYNC_AVAILABLE and _local_debug_enabled() and _env_flag("ALIAS_SYNC_ON_STARTUP")


def _maybe_run_alias_sync_on_startup():
    if _should_run_alias_sync_on_startup():
        print("Syncing aliases...")
        _alias_sync()
        return True
    if ALIAS_SYNC_AVAILABLE:
        print("[startup] Skipping alias_sync at startup; run it explicitly before deploy.")
    return False


# ---------------------------------------------------------------------------
# Startup checks
# Runs before anything else. Exits immediately with a clear error if
# something critical is missing — better to crash loudly at boot than
# silently return wrong answers at 2am.
# ---------------------------------------------------------------------------

def run_startup_checks():
    import sys
    _refresh_runtime_settings()
    errors = []
    warnings = []
    runtime_allowed = set(RUNTIME_OPTIONS.get("allowed_user_ids") or [])
    access_mode = _effective_access_mode()
    if access_mode not in {"open", "closed"}:
        errors.append(
            f"Invalid access_mode {access_mode!r}. Use 'open' for testing or 'closed' for production."
        )

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
        real_files = list({
            normalize_path(os.path.abspath(f)): f
            for f in txt_files
            if Path(f).name not in EXCLUDED_FROM_PROTOCOLS
        }.values())
        if not real_files:
            errors.append("protocols/ folder contains no .txt protocol files.")
        else:
            print(f"[startup] Found {len(real_files)} protocol file(s): "
                  f"{[Path(f).name for f in real_files]}")

        try:
            from protocol_linter import run_linter
            lint_result = run_linter(proto_dir="protocols")
            lint_errors = lint_result.errors()
            lint_warnings = lint_result.warnings()
            for issue in lint_errors:
                errors.append(
                    f"Protocol linter blocking error: {issue.protocol}: "
                    f"[{issue.code}] {issue.message}"
                )
            if lint_warnings:
                warnings.append(
                    f"Protocol linter reported {len(lint_warnings)} warning(s); "
                    "run 'python -m protocol_linter' for details."
                )
        except Exception as exc:
            errors.append(f"Protocol linter failed during startup validation: {exc}")

    # 6. Production must fail closed. Local debug/open mode may run unrestricted.
    if access_mode == "closed" and not runtime_allowed:
        if _local_debug_enabled():
            warnings.append("!! ALLOWED USERS NOT DEFINED !!")
        else:
            errors.append(
                "ALLOWED_USER_IDS environment variable is not set. "
                "Set it before running outside LOCAL_DEBUG, or set access_mode=open for local testing."
            )
    elif access_mode == "open" and not runtime_allowed:
        warnings.append("!! ALLOWED USERS NOT DEFINED !!")

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

    print(f"[startup] All checks passed.")


# ---------------------------------------------------------------------------
# Structured logging
#
# Every query is written as a JSON line to bot_queries.log.
# JSON-lines format: one complete JSON object per line, easy to grep and parse.
# The file rotates at 5 MB, keeping 3 old copies (so max ~20 MB on disk).
# On Railway, stdout is captured in the Railway log viewer automatically. Set
# LOCAL_DEBUG=1 or FULL_CONVERSATION_LOG=1 to also print a full reconstructable
# JSON turn to stdout while debugging alone.
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
    return audit_helpers.setup_logging(LOG_FILE)



# Module-level query logger (populated by setup_logging())
_query_log = None


def _safe_user_message_for_log(user_message):
    return audit_helpers._safe_user_message_for_log(user_message, FULL_CONVERSATION_LOG)



def _safe_prompt_preview_for_stdout(user_message):
    return audit_helpers._safe_prompt_preview_for_stdout(user_message, FULL_CONVERSATION_LOG)



def _reconstructable_turn_for_stdout(entry):
    return audit_helpers._reconstructable_turn_for_stdout(entry, FULL_CONVERSATION_LOG)



def _log_query(chat_id, user_message, recognized, retrieved_chunks,
               raw_llm, final_response, duration_ms, trace=None):
    return audit_helpers._log_query(
        _query_log,
        chat_id,
        user_message,
        recognized,
        retrieved_chunks,
        raw_llm,
        final_response,
        duration_ms,
        trace=trace,
        full_conversation_log=FULL_CONVERSATION_LOG,
    )



def _runtime_error_payload(component, error, chat_id=None, user_message=None, duration_ms=None):
    import datetime as _dt
    payload = {
        "event": "runtime_error",
        "component": component,
        "ts": _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "error_type": type(error).__name__,
        "error_message": str(error),
    }
    if chat_id is not None:
        payload["chat_id_hash"] = hashlib.md5(str(chat_id).encode()).hexdigest()[:8]
    if user_message is not None:
        payload["user_message"] = _safe_user_message_for_log(user_message)
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    return payload


def _log_runtime_error(component, error, chat_id=None, user_message=None, duration_ms=None):
    payload = _runtime_error_payload(component, error, chat_id, user_message, duration_ms)
    line = json.dumps(payload, ensure_ascii=False)
    logging.error(line, exc_info=True)
    if _query_log is not None:
        _query_log.info(line)


def _log_safe_runtime_failure(t_start, chat_id, question, error, component):
    duration_ms = round((time.monotonic() - t_start) * 1000)
    trace = {
        "runtime_error": True,
        "component": component,
        "error_type": type(error).__name__,
        "safe_failure": True,
        "deterministic_or_llm": "runtime_failure",
        "llm_called": False,
    }
    _log_runtime_error(component, error, chat_id, question, duration_ms)
    try:
        _log_query(
            chat_id=chat_id,
            user_message=question,
            recognized=None,
            retrieved_chunks=[],
            raw_llm="",
            final_response=SAFE_RUNTIME_FAILURE_MESSAGE,
            duration_ms=duration_ms,
            trace=trace,
        )
    except Exception as log_error:
        logging.error(
            json.dumps(
                _runtime_error_payload("query_log_failure", log_error, chat_id, question),
                ensure_ascii=False,
            ),
            exc_info=True,
        )


def _format_trace_for_stdout(trace):
    return audit_helpers._format_trace_for_stdout(trace)



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
    return retrieval_helpers.normalize_path(path)



def derive_source_label(file_path):
    return retrieval_helpers.derive_source_label(file_path)



def extract_source_label_from_text(text):
    return retrieval_helpers.extract_source_label_from_text(text)



def get_source_label_for_file(file_path, text):
    return retrieval_helpers.get_source_label_for_file(
        file_path,
        text,
        protocol_file_to_label=PROTOCOL_FILE_TO_LABEL,
    )



# ---------------------------------------------------------------------------
# Alias recognition
# ---------------------------------------------------------------------------

def load_aliases(path="protocols/aliases.json"):
    global ALIASES, ALIAS_INDEX, BLOCKED_ALIASES, UNSUPPORTED_SYNDROMES, PROTOCOL_FILE_TO_LABEL
    alias_helpers.load_aliases(path)
    ALIASES = alias_helpers.ALIASES
    ALIAS_INDEX = alias_helpers.ALIAS_INDEX
    BLOCKED_ALIASES = alias_helpers.BLOCKED_ALIASES
    UNSUPPORTED_SYNDROMES = alias_helpers.UNSUPPORTED_SYNDROMES
    PROTOCOL_FILE_TO_LABEL = alias_helpers.PROTOCOL_FILE_TO_LABEL



def _build_alias_index(alias_data):
    return alias_helpers._build_alias_index(alias_data, normalize_path_fn=normalize_path)



def _alias_term_matches(term, text):
    return alias_helpers._alias_term_matches(term, text)



def normalize_question(question):
    return alias_helpers.normalize_question(
        question,
        alias_index=ALIAS_INDEX,
        blocked_aliases=BLOCKED_ALIASES,
        unsupported_policies=UNSUPPORTED_SYNDROMES,
        rapidfuzz_available=RAPIDFUZZ_AVAILABLE,
        process_module=process if RAPIDFUZZ_AVAILABLE else None,
        fuzz_module=fuzz if RAPIDFUZZ_AVAILABLE else None,
    )


def _detect_unsupported_policy(question):
    return alias_helpers._detect_unsupported_policy(
        question,
        unsupported_policies=UNSUPPORTED_SYNDROMES,
        blocked_aliases=BLOCKED_ALIASES,
        rapidfuzz_available=RAPIDFUZZ_AVAILABLE,
        process_module=process if RAPIDFUZZ_AVAILABLE else None,
        fuzz_module=fuzz if RAPIDFUZZ_AVAILABLE else None,
    )



def _detect_unsupported_syndrome(question):
    return alias_helpers._detect_unsupported_syndrome(
        question,
        unsupported_policies=UNSUPPORTED_SYNDROMES,
        blocked_aliases=BLOCKED_ALIASES,
    )



_OBVIOUS_NONCLINICAL_RE = re.compile(
    r"^\s*(?:"
    r"hi|hello|hey|good morning|good afternoon|good evening|"
    r"how are you\??|thanks?|thank you|ok|okay|"
    r"what is the capital of .+|capital of .+|"
    r"tell me a joke|what'?s the weather\??"
    r")\s*$",
    re.IGNORECASE,
)


def _is_obvious_nonclinical_message(question):
    return alias_helpers._is_obvious_nonclinical_message(question)



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
    return retrieval_helpers._compute_file_hash(file_path)



def _init_cache_db():
    global _CACHE_DISABLED
    ok, _CACHE_DISABLED = retrieval_helpers.init_cache_db(CACHE_DB, _CACHE_DISABLED)
    return ok



def _load_from_cache(file_hash):
    return retrieval_helpers.load_from_cache(file_hash, CACHE_DB, _CACHE_DISABLED)



def _save_to_cache(file_hash, chunks):
    return retrieval_helpers.save_to_cache(file_hash, chunks, CACHE_DB, _CACHE_DISABLED)



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
    return retrieval_helpers.chunk_text(text, source, source_label, max_chars)



def get_embedding(text):
    return retrieval_helpers.get_embedding(text, openai_client, EMBEDDING_MODEL)



def load_protocols():
    global _CACHE_DISABLED, PROTOCOL_CHUNKS
    _CACHE_DISABLED = retrieval_helpers.load_protocols(
        protocol_chunks=PROTOCOL_CHUNKS,
        policy_by_file=PROTOCOL_POLICY_BY_FILE,
        parsed_by_file=PROTOCOL_PARSED_BY_FILE,
        protocol_file_to_label=PROTOCOL_FILE_TO_LABEL,
        cache_db=CACHE_DB,
        cache_disabled=_CACHE_DISABLED,
        embedding_client=openai_client,
        embedding_model=EMBEDDING_MODEL,
    )



def search_protocols(question, top_k=3, preferred_file=None, guaranteed_slots=2):
    return retrieval_helpers.search_protocols(
        question,
        top_k=top_k,
        preferred_file=preferred_file,
        guaranteed_slots=guaranteed_slots,
        protocol_chunks=PROTOCOL_CHUNKS,
        embedding_client=openai_client,
        embedding_model=EMBEDDING_MODEL,
    )


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
    return state_helpers._now_iso()



def _parse_iso(ts):
    return state_helpers._parse_iso(ts)



def get_chat_state(chat_id):
    state_helpers.bind_state(CONVERSATION_STATE)
    return state_helpers.get_chat_state(chat_id)



def init_tree_state(state, parsed_protocol, recognized):
    return state_helpers.init_tree_state(state, parsed_protocol, recognized)



def advance_tree_state(state, next_node_id, collected_updates=None):
    return state_helpers.advance_tree_state(state, next_node_id, collected_updates)



def reset_tree_state(state):
    return state_helpers.reset_tree_state(state)


def reset_patient_state(state):
    return state_helpers.reset_patient_state(state)



def _slot_namespace_for_recognized(recognized):
    return state_helpers._slot_namespace_for_recognized(recognized)



def _get_protocol_slots(state, recognized):
    return state_helpers._get_protocol_slots(state, recognized)



def _set_protocol_slots(state, recognized, slots):
    return state_helpers._set_protocol_slots(state, recognized, slots)


def _mirror_active_protocol_slots(state, recognized=None):
    return state_helpers.mirror_active_protocol_slots(state, recognized)


def _transfer_protocol_slots(
    state,
    source_recognized,
    target_recognized,
    transfer_slots,
    *,
    target_slot_names=None,
    extra_slots=None,
):
    return state_helpers.transfer_protocol_slots(
        state,
        source_recognized,
        target_recognized,
        transfer_slots,
        target_slot_names=target_slot_names,
        extra_slots=extra_slots,
    )



def _recognized_protocol_summary(recognized):
    if not recognized:
        return None
    meta = _protocol_meta_for_file(recognized.get("protocol_file", ""))
    return {
        "protocol_id": meta.get("protocol_id") or recognized.get("protocol_id") or recognized.get("display"),
        "protocol_file": recognized.get("protocol_file", ""),
        "source_label": meta.get("source_label") or recognized.get("source_label"),
        "protocol_type": meta.get("protocol_type") or recognized.get("protocol_type"),
    }


_CORRECTION_RE = re.compile(
    r"\b(?:not|nem|actually|instead|rather|correction|correct|but|hanem|helyett)\b",
    re.IGNORECASE,
)


def _is_correction_intent(question):
    return bool(_CORRECTION_RE.search(question or ""))


_ADMIN_DEBUG_NOTE_RE = re.compile(
    r"^\s*(?:"
    r"/debug\b|"
    r"debug\s*:|debug\s+(?:note|not)\s*:|"
    r"dedebug\s+(?:note|not)\s*:|deubg\s*:|"
    r"admin\s*:|note\s+to\s+self\s*:|audit\s*:|log\s*:|todo\s*:)"
    ,
    re.IGNORECASE,
)


def _is_admin_debug_note(question):
    return bool(_ADMIN_DEBUG_NOTE_RE.search(question or ""))


_KNOWN_SLOT_ALIASES = {
    "gfr": [
        "gfr", "egfr", "crcl", "renal", "kidney", "kidney function",
        "renal function", "vesefunkcio", "vesefunkció", "ml/min",
    ],
    "egfr": ["egfr"],
    "body_weight_kg": [
        "weight", "body weight", "kg", "suly", "súly", "testsuly", "testsúly",
    ],
    "adjusted_body_weight": ["adjusted body weight", "abw", "adjusted weight"],
    "pathogen_list": [
        "pathogen", "pathogens", "organism", "organisms", "bacteria",
        "bacterium", "detected pathogen", "result", "biofire result",
    ],
    "resistance_gene_list": [
        "resistance", "resistance gene", "resistance marker", "gene", "genes",
        "marker", "markers", "ctx-m", "ctxm", "esbl", "carbapenemase",
        "meca", "mec-a",
    ],
    "indication": ["indication", "diagnosis", "infection", "reason"],
    "patient_status": [
        "status", "patient status", "intubated", "hospitalized",
        "hospitalised", "dischargeable", "outpatient", "ambulant",
    ],
    "intubated": ["intubated", "ventilated", "mechanically ventilated"],
    "crrt": ["crrt"],
    "ihd": ["ihd", "hemodialysis", "haemodialysis", "dialysis"],
    "viral_test_result": ["viral", "viral test", "influenza", "flu"],
    "vancomycin_level": ["vancomycin level", "vanco level", "level", "tdm"],
    "mic": ["mic"],
}


def _slot_display_name(slot_name):
    return {
        "body_weight_kg": "weight",
        "gfr": "GFR",
        "egfr": "eGFR",
        "pathogen_list": "pathogens",
        "resistance_gene_list": "resistance genes",
        "patient_status": "patient status",
        "viral_test_result": "viral test result",
    }.get(slot_name, slot_name.replace("_", " "))


def _protocol_slot_names(parsed):
    names = set()
    if parsed:
        names.update((parsed.get("slot_schema") or {}).keys())
        for block_name in ("input_slots", "required_information"):
            block = parsed.get(block_name) or ""
            for raw in str(block).splitlines():
                line = raw.strip()
                if not line.startswith("-"):
                    continue
                item = line.lstrip("-").strip().split()[0].strip(":,;")
                if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", item):
                    names.add(item.lower())
    if not names:
        names.update(_KNOWN_SLOT_ALIASES.keys())
    return names


def _slot_aliases(slot_name):
    aliases = set(_KNOWN_SLOT_ALIASES.get(slot_name, []))
    aliases.add(slot_name)
    aliases.add(slot_name.replace("_", " "))
    if slot_name.endswith("_kg"):
        aliases.add(slot_name[:-3].replace("_", " "))
    return sorted(aliases, key=len, reverse=True)


def _text_mentions_slot(text, slot_name):
    lower = (text or "").lower()
    for alias in _slot_aliases(slot_name):
        if re.search(r"\b" + re.escape(alias.lower()) + r"\b", lower):
            return True
    return False


def _numeric_protocol_slots(parsed, existing):
    schema = (parsed or {}).get("slot_schema") or {}
    names = set()
    for slot_name, spec in schema.items():
        if str(spec.get("type", "")).lower() == "number":
            names.add(slot_name)
    for slot_name, value in (existing or {}).items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            names.add(slot_name)
    return names


def _number_tokens(text):
    return [float(m.group(1)) for m in re.finditer(r"\b(\d+(?:\.\d+)?)\b", text or "")]


def _numbers_before_change_marker(text):
    marker = re.search(r"\b(?:but|hanem|instead|rather)\b", text or "", re.IGNORECASE)
    left = (text or "")[:marker.start()] if marker else (text or "")
    return _number_tokens(left)


def _approximately_equal(a, b):
    try:
        return abs(float(a) - float(b)) < 0.0001
    except (TypeError, ValueError):
        return False


def _correction_rhs_text(text):
    match = re.search(r"\b(?:but|hanem|instead|rather)\b", text or "", re.IGNORECASE)
    if match:
        rhs = (text or "")[match.end():].strip(" ,.;:")
        if rhs:
            return rhs
    return text or ""


def _apply_slot_update_conflicts(slots, updates):
    if "patient_status" in updates:
        if updates.get("patient_status") == "intubated":
            slots["intubated"] = True
        else:
            slots.pop("intubated", None)
    if updates.get("crrt") is True:
        slots.pop("ihd", None)
    if updates.get("ihd") is True:
        slots.pop("crrt", None)


def _format_correction_clarification(parsed, existing):
    candidates = []
    protocol_slots = _protocol_slot_names(parsed)
    for slot_name in sorted(protocol_slots):
        if slot_name in existing or slot_name in (parsed.get("slot_schema") if parsed else {}):
            candidates.append(_slot_display_name(slot_name))
    if not candidates:
        candidates = ["weight", "GFR", "pathogens", "indication"]
    choices = ", ".join(dict.fromkeys(candidates))
    return f"Which slot should I correct? Please specify one target, for example: {choices}."


def _infer_numeric_correction_target(parsed, existing, text, new_value):
    candidates = set()
    numeric_slots = _numeric_protocol_slots(parsed, existing)
    if re.search(r"\bkg\b", text or "", re.IGNORECASE):
        candidates.add("body_weight_kg")
    if re.search(r"\b(?:ml/min|gfr|egfr|crcl|renal|kidney)\b", text or "", re.IGNORECASE):
        candidates.add("gfr")
    for slot_name in numeric_slots:
        if _text_mentions_slot(text, slot_name):
            candidates.add(slot_name)
    old_values = _numbers_before_change_marker(text)
    if old_values:
        for slot_name in numeric_slots:
            if any(_approximately_equal(existing.get(slot_name), old) for old in old_values):
                candidates.add(slot_name)
    candidates = {slot for slot in candidates if slot in _protocol_slot_names(parsed) or slot in existing}
    if len(candidates) == 1:
        return next(iter(candidates))
    if len(candidates) > 1:
        return None
    if len(numeric_slots) == 1:
        return next(iter(numeric_slots))
    return None


def _selection_trace_value(selection_result, key, default=None):
    if isinstance(selection_result, dict):
        return selection_result.get(key, default)
    if selection_result is None:
        return default
    return getattr(selection_result, key, default)


def _trace_retrieved_chunks(retrieved_chunks):
    traced = []
    for chunk in retrieved_chunks or []:
        item = {
            "source_label": chunk.get("source_label"),
            "source": chunk.get("source"),
        }
        if "similarity" in chunk:
            try:
                item["similarity"] = round(float(chunk.get("similarity")), 4)
            except (TypeError, ValueError):
                item["similarity"] = chunk.get("similarity")
        traced.append(item)
    return traced


def _turn_context_for_trace(turn_context):
    if not turn_context:
        return {}
    return {
        "raw_user_text": _safe_user_message_for_log(turn_context.raw_user_text),
        "chat_id_hash": hashlib.md5(str(turn_context.chat_id).encode()).hexdigest()[:8],
        "active_before": _recognized_protocol_summary(turn_context.active_before),
        "fresh_recognized": _recognized_protocol_summary(turn_context.fresh_recognized),
        "selected_recognized": _recognized_protocol_summary(turn_context.selected_recognized),
        "unsupported_syndrome": turn_context.unsupported_syndrome,
        "unsupported_key": turn_context.unsupported_syndrome,
        "unsupported_matched_term": turn_context.unsupported_matched_term,
        "unsupported_message": turn_context.unsupported_message,
        "intent": turn_context.intent,
        "correction_intent": bool(turn_context.correction_intent),
        "clear_intent": bool(turn_context.clear_intent),
        "normalized_question_present": bool(turn_context.normalized_question),
        "protocol_slots_before": dict(turn_context.protocol_slots_before or {}),
        "protocol_slots_after": dict(turn_context.protocol_slots_after or {}),
        "confirmation_pending": bool(turn_context.confirmation_pending),
        "confirmation_required": bool(turn_context.confirmation_required),
    }


def _new_turn_context(
    *,
    raw_user_text,
    chat_id,
    state,
    active_before,
    fresh_recognized=None,
    selected_recognized=None,
    unsupported_syndrome=None,
    unsupported_matched_term=None,
    unsupported_message=None,
    intent=None,
    normalized_question=None,
    confirmation_pending=False,
    confirmation_required=False,
):
    selected = selected_recognized or fresh_recognized
    return TurnContext(
        raw_user_text=raw_user_text,
        chat_id=chat_id,
        active_before=dict(active_before or {}),
        fresh_recognized=fresh_recognized,
        selected_recognized=selected,
        unsupported_syndrome=unsupported_syndrome,
        unsupported_matched_term=unsupported_matched_term,
        unsupported_message=unsupported_message,
        intent=intent or classify_intent(raw_user_text),
        correction_intent=_is_correction_intent(raw_user_text),
        clear_intent=_is_slot_clear_phrase(raw_user_text),
        normalized_question=normalized_question or raw_user_text,
        protocol_slots_before=_get_protocol_slots(state, selected) if selected else {},
        protocol_slots_after=_get_protocol_slots(state, selected) if selected else {},
        confirmation_pending=bool(confirmation_pending),
        confirmation_required=bool(confirmation_required),
    )


def _update_turn_after_selection(turn_context, state, selected_recognized=None, confirmation_required=None):
    if not turn_context:
        return
    if selected_recognized is not None:
        turn_context.selected_recognized = selected_recognized
    selected = turn_context.selected_recognized
    turn_context.protocol_slots_after = _get_protocol_slots(state, selected) if selected else {}
    if confirmation_required is not None:
        turn_context.confirmation_required = bool(confirmation_required)


def _make_answer_trace(
    *,
    state,
    recognized=None,
    active_before=None,
    deterministic_or_llm="unknown",
    llm_called=False,
    selection_result=None,
    slots=None,
    unsupported_syndrome=None,
    unsupported_matched_term=None,
    unsupported_message=None,
    unsupported_action=None,
    blocked_reason=None,
    confirmation_required=False,
    turn_context=None,
    final_body=None,
    final_answer=None,
    retrieved_chunks=None,
):
    active_after = state.get("active_recognized") if state else None
    selected = recognized or active_after
    meta = _protocol_meta_for_file((selected or {}).get("protocol_file", ""))
    output_key = _selection_trace_value(selection_result, "output_key")
    mode_used = _selection_trace_value(selection_result, "mode_used")
    default_used = _selection_trace_value(selection_result, "default_used")
    missing_slots = _selection_trace_value(selection_result, "missing_slots", []) or []
    trace = {
        "selected_protocol_id": meta.get("protocol_id") or (selected or {}).get("protocol_id") or (selected or {}).get("display"),
        "selected_protocol_file": (selected or {}).get("protocol_file"),
        "source_label": meta.get("source_label") or (selected or {}).get("source_label"),
        "protocol_type": meta.get("protocol_type") or (selected or {}).get("protocol_type"),
        "active_before": _recognized_protocol_summary(active_before),
        "active_after": _recognized_protocol_summary(active_after),
        "matched_alias": (recognized or {}).get("matched_alias"),
        "confidence": (recognized or {}).get("confidence"),
        "selection_output_key": output_key or ("default" if default_used else None),
        "selection_mode": mode_used,
        "missing_slots": missing_slots,
        "slots": dict(slots or {}),
        "deterministic_or_llm": deterministic_or_llm,
        "llm_called": bool(llm_called),
        "unsupported_syndrome": unsupported_syndrome,
        "unsupported_key": unsupported_syndrome,
        "unsupported_matched_term": unsupported_matched_term,
        "unsupported_message": unsupported_message,
        "unsupported_action": unsupported_action,
        "blocked_reason": blocked_reason,
        "confirmation_required": bool(confirmation_required),
    }
    if turn_context:
        trace["turn_context"] = _turn_context_for_trace(turn_context)
        trace["confirmation_pending"] = bool(turn_context.confirmation_pending)
        trace["confirmation_required"] = bool(turn_context.confirmation_required or confirmation_required)
    if final_body is not None:
        trace["final_body"] = final_body
    if final_answer is not None:
        trace["final_answer"] = final_answer
    if retrieved_chunks is not None:
        trace["retrieved_chunks"] = _trace_retrieved_chunks(retrieved_chunks)
    trace["selected_output_key"] = trace["selection_output_key"]
    return trace


def _make_answer_envelope(
    *,
    state,
    turn_context=None,
    recognized=None,
    active_before=None,
    final_body="",
    final_answer="",
    retrieved_chunks=None,
    deterministic_or_llm="unknown",
    llm_called=False,
    selection_result=None,
    slots=None,
    unsupported_syndrome=None,
    unsupported_matched_term=None,
    unsupported_message=None,
    unsupported_action=None,
    blocked_reason=None,
    confirmation_required=False,
):
    retrieved_chunks = list(retrieved_chunks or [])
    selected = recognized or (state.get("active_recognized") if state else None)
    meta = _protocol_meta_for_file((selected or {}).get("protocol_file", ""))
    output_key = _selection_trace_value(selection_result, "output_key")
    mode_used = _selection_trace_value(selection_result, "mode_used")
    default_used = _selection_trace_value(selection_result, "default_used")
    selected_output_key = output_key or ("default" if default_used else None)
    if turn_context:
        _update_turn_after_selection(turn_context, state, selected, confirmation_required)
    trace = _make_answer_trace(
        state=state,
        recognized=selected,
        active_before=active_before,
        deterministic_or_llm=deterministic_or_llm,
        llm_called=llm_called,
        selection_result=selection_result,
        slots=slots,
        unsupported_syndrome=unsupported_syndrome,
        unsupported_matched_term=unsupported_matched_term,
        unsupported_message=unsupported_message,
        unsupported_action=unsupported_action,
        blocked_reason=blocked_reason,
        confirmation_required=confirmation_required,
        turn_context=turn_context,
        final_body=final_body,
        final_answer=final_answer,
        retrieved_chunks=retrieved_chunks,
    )
    return AnswerEnvelope(
        final_body=final_body,
        final_answer=final_answer,
        selected_protocol_id=trace.get("selected_protocol_id"),
        selected_protocol_file=(selected or {}).get("protocol_file"),
        selected_source=meta.get("source_label") or (selected or {}).get("source_label"),
        selected_output_key=selected_output_key,
        selection_mode=mode_used,
        deterministic_or_llm=deterministic_or_llm,
        llm_called=bool(llm_called),
        retrieved_chunks=retrieved_chunks,
        blocked_reason=blocked_reason,
        unsupported_action=unsupported_action,
        unsupported_syndrome=unsupported_syndrome,
        unsupported_matched_term=unsupported_matched_term,
        unsupported_message=unsupported_message,
        trace=trace,
    )


def _remember_answer(state, question, answer):
    history = state["history"] + [
        {"role": "user", "content": question},
        {"role": "assistant", "content": answer},
    ]
    state["history"] = history[-(MAX_HISTORY_TURNS * 2):]


def _log_answer_envelope(t_start, chat_id, question, recognized, envelope):
    _log_query(
        chat_id=chat_id,
        user_message=question,
        recognized=recognized,
        retrieved_chunks=envelope.retrieved_chunks,
        raw_llm=envelope.final_body,
        final_response=envelope.final_answer,
        duration_ms=round((time.monotonic() - t_start) * 1000),
        trace=envelope.trace,
    )


def is_tree_idle_timeout(state):
    return state_helpers.is_tree_idle_timeout(state)



def is_explicit_reset_phrase(text):
    return state_helpers.is_explicit_reset_phrase(text)



def maybe_auto_reset_tree(state):
    return state_helpers.maybe_auto_reset_tree(state)



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


def _recognized_for_link_target(link_entry: dict) -> dict | None:
    target_file = link_entry.get("target_file", "")
    if not target_file:
        return None
    parsed = PROTOCOL_PARSED_BY_FILE.get(normalize_path(target_file))
    if not parsed:
        return None
    meta = parsed.get("metadata", {})
    label = meta.get("source_label") or link_entry.get("when_selected_item") or link_entry.get("target_protocol_id")
    return {
        "protocol_id": meta.get("protocol_id") or link_entry.get("target_protocol_id"),
        "protocol_file": target_file,
        "protocol_type": meta.get("protocol_type"),
        "source_label": label,
        "display": label,
        "canonical": meta.get("canonical_name") or label,
        "confidence": "high",
        "category": "drugs" if meta.get("protocol_type") == "drug_dosing_protocol" else "conditions",
    }


def _activate_new_schema_link_target(state: dict, source_recognized: dict, link_entry: dict) -> dict | None:
    target_recognized = _recognized_for_link_target(link_entry)
    if not target_recognized:
        return None
    target_file = target_recognized.get("protocol_file", "")
    target_parsed = PROTOCOL_PARSED_BY_FILE.get(normalize_path(target_file))
    target_slots = _protocol_slot_names(target_parsed)
    extra = {}
    selected_item = link_entry.get("when_selected_item")
    if selected_item:
        extra["selected_antimicrobial"] = selected_item
    _transfer_protocol_slots(
        state,
        source_recognized,
        target_recognized,
        link_entry.get("transfer_slots", []),
        target_slot_names=target_slots,
        extra_slots=extra,
    )
    state["active_recognized"] = target_recognized
    state["last_recommended_antibiotics"] = []
    _update_routing_state(state, target_recognized, "link_transfer")
    _mirror_active_protocol_slots(state, target_recognized)
    return target_recognized


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
        _activate_new_schema_link_target(
            state,
            state.get("active_recognized") or {},
            matching_entry,
        )
        return None
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
    calculator_ids = {
        "body_size_calculators",
        "echo_cardiac_output",
        "echo_ava",
        "echo_ero_rvol",
    }
    protocol_id = meta.get("protocol_id", "")
    if mode in ("none", "decision_tree", "") and protocol_id not in calculator_ids:
        return None
    existing = _get_protocol_slots(state, recognized)
    correction = _apply_generic_correction_or_clear(parsed, existing, question)
    if correction.get("ambiguous"):
        _set_protocol_slots(state, recognized, correction.get("slots", existing))
        state["_last_selection_trace"] = {
            "output_key": None,
            "mode_used": "correction_intent",
            "default_used": False,
            "missing_slots": [],
            "no_match": False,
            "slots": dict(correction.get("slots", existing)),
        }
        return correction.get("message")
    existing = correction.get("slots", existing)
    question_for_extract = correction.get("question_for_extract", question)
    slots = extract_slots_from_query(question_for_extract, parsed_protocol=parsed, existing_slots=existing)
    _set_protocol_slots(state, recognized, slots)
    result = run_selection(parsed, slots, lang=lang)
    pending_bounds = _pending_bounds_from_selection(recognized, slots, result, question)
    if pending_bounds:
        state["pending_out_of_bounds_confirmation"] = pending_bounds
    state["_last_selection_trace"] = {
        "output_key": result.output_key,
        "mode_used": result.mode_used,
        "default_used": result.default_used,
        "missing_slots": list(result.missing_slots or []),
        "no_match": result.no_match,
        "slots": dict(slots),
    }
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


_CLEAR_SLOT_RE = re.compile(
    r"\b(?:delete|clear|remove|forget|reset)\s+(?:previous\s+)?"
    r"(?:pathogens?|organisms?|genes?|results?|slots?|data|facts?|"
    r"gfr|egfr|crcl|renal|kidney|weight|body\s+weight|indication|status)\b",
    re.IGNORECASE,
)


def _is_slot_clear_phrase(question):
    return bool(_CLEAR_SLOT_RE.search(question or ""))


def _clear_slots_for_protocol(parsed, slots, question):
    cleared = dict(slots or {})
    lower = (question or "").lower()
    if any(word in lower for word in ["slot", "data", "fact"]):
        return {}
    targets = _clear_slot_targets(parsed, question)
    for target in targets:
        cleared.pop(target, None)
    return cleared


def _clear_slot_targets(parsed, question):
    text = question or ""
    lower = text.lower()
    protocol_slots = _protocol_slot_names(parsed)
    targets = set()
    if any(word in lower for word in ["pathogen", "organism"]):
        targets.add("pathogen_list")
        if "resistance_gene_list" in protocol_slots:
            targets.add("resistance_gene_list")
    if any(word in lower for word in ["gene", "resistance", "marker"]):
        targets.add("resistance_gene_list")
    if "result" in lower:
        if "pathogen_list" in protocol_slots or "resistance_gene_list" in protocol_slots:
            targets.update(["pathogen_list", "resistance_gene_list"])
        else:
            return set(protocol_slots)
    if any(word in lower for word in ["renal", "kidney", "gfr", "egfr", "crcl"]):
        targets.update(["gfr", "egfr", "crrt", "ihd"])
    if any(word in lower for word in ["weight", "suly", "súly", "kg"]):
        targets.update(["body_weight_kg", "adjusted_body_weight"])
    for slot_name in protocol_slots:
        if _text_mentions_slot(text, slot_name):
            targets.add(slot_name)
    return {slot for slot in targets if slot in protocol_slots}


def _apply_generic_correction_or_clear(parsed, existing, question):
    slots = dict(existing or {})
    if _is_slot_clear_phrase(question):
        cleared = _clear_slots_for_protocol(parsed, slots, question)
        return {"handled": True, "slots": cleared, "question_for_extract": ""}

    if not _is_correction_intent(question):
        return {"handled": False, "slots": slots, "question_for_extract": question}

    fragment = _correction_rhs_text(question)
    updates = extract_slots_from_query(fragment, parsed_protocol=parsed, existing_slots={})
    if updates:
        updated = dict(slots)
        for key, value in updates.items():
            updated[key] = value
        _apply_slot_update_conflicts(updated, updates)
        return {"handled": True, "slots": updated, "question_for_extract": ""}

    numbers = _number_tokens(fragment)
    if numbers:
        new_value = numbers[-1]
        target = _infer_numeric_correction_target(parsed, slots, question, new_value)
        if target:
            updated = dict(slots)
            updated[target] = new_value
            return {"handled": True, "slots": updated, "question_for_extract": ""}
        return {
            "handled": True,
            "ambiguous": True,
            "message": _format_correction_clarification(parsed, slots),
            "slots": slots,
            "question_for_extract": "",
        }

    return {
        "handled": True,
        "ambiguous": True,
        "message": _format_correction_clarification(parsed, slots),
        "slots": slots,
        "question_for_extract": "",
    }


_YES_RE = re.compile(r"^\s*(?:yes|y|igen|ja|apply|use it)\s*[\.\?!]*\s*$", re.IGNORECASE)
_NO_RE = re.compile(r"^\s*(?:no|n|nem|nope|cancel|do not)\s*[\.\?!]*\s*$", re.IGNORECASE)
_YES_PREFIX_RE = re.compile(r"^\s*(?:yes|y|igen|ja)\b", re.IGNORECASE)
_NO_PREFIX_RE = re.compile(r"^\s*(?:no|n|nem|nope)\b", re.IGNORECASE)


def _pending_bounds_from_selection(recognized, slots, result, question):
    if _selection_trace_value(result, "output_key") != "SLOT_OUT_OF_CLINICAL_BOUNDS":
        return None
    render_vars = _selection_trace_value(result, "render_vars", {}) or {}
    slot_name = render_vars.get("out_of_bounds_slot")
    if not slot_name:
        output_data = _selection_trace_value(result, "output_data", {}) or {}
        slot_name = output_data.get("slot")
    if not slot_name:
        return None
    return {
        "type": "slot_out_of_bounds",
        "recognized": dict(recognized or {}),
        "slot": slot_name,
        "value": slots.get(slot_name),
        "slots": dict(slots or {}),
        "question": question,
    }


def _extract_pending_bounds_correction_question(pending, question, parsed):
    updates = extract_slots_from_query(question, parsed_protocol=parsed, existing_slots={})
    slot_name = pending.get("slot")
    if slot_name in updates:
        return question
    numbers = _number_tokens(question)
    if len(numbers) == 1 and slot_name:
        return f"{slot_name} {numbers[0]}"
    return None


def _resolve_pending_out_of_bounds_confirmation(state, question):
    pending = state.get("pending_out_of_bounds_confirmation")
    if not pending:
        return None
    recognized = pending.get("recognized") or state.get("active_recognized")
    parsed = PROTOCOL_PARSED_BY_FILE.get(normalize_path((recognized or {}).get("protocol_file", "")))
    correction_question = _extract_pending_bounds_correction_question(pending, question, parsed)
    pending_value = pending.get("value")
    slot_name = pending.get("slot")
    numbers = _number_tokens(question)
    numeric_differs = bool(
        numbers
        and pending_value is not None
        and not _approximately_equal(numbers[-1], pending_value)
    )

    if _YES_PREFIX_RE.match(question) and not numeric_differs:
        state["pending_out_of_bounds_confirmation"] = None
        _set_protocol_slots(state, recognized, pending.get("slots", {}))
        slot_label = _slot_display_name(slot_name)
        body = (
            f"Confirmed {slot_label} {_fmt_number_for_message(pending_value)} is outside the expected "
            "clinical bounds for this protocol. I still cannot provide automatic dosing from this value. "
            "Please correct the value or use individualized ID/pharmacy review."
        )
        return {
            "answer_body": body,
            "recognized": recognized,
            "blocked_reason": "out_of_bounds_confirmed",
            "slots": dict(pending.get("slots", {})),
        }

    if _NO_PREFIX_RE.match(question) and not correction_question:
        state["pending_out_of_bounds_confirmation"] = None
        body = f"OK. Please send the corrected {_slot_display_name(slot_name)} before dosing."
        return {
            "answer_body": body,
            "recognized": recognized,
            "blocked_reason": "out_of_bounds_correction_requested",
            "slots": dict(pending.get("slots", {})),
        }

    if correction_question:
        state["pending_out_of_bounds_confirmation"] = None
        return {
            "question": correction_question,
            "recognized": recognized,
        }

    return None


def _fmt_number_for_message(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else str(number)


_PERIOP_FOLLOWUP_RE = re.compile(
    r"\b(?:"
    r"dabigatran\w*|pradaxa|apixaban\w*|eliquis|edoxaban\w*|lixiana|"
    r"rivaroxaban\w*|xarelto|fondaparinux|arixtra|warfarin|marfarin|"
    r"acenocoumarol|syncumar|heparin|lmwh|enoxaparin|clexane|dalteparin|"
    r"fragmin|nadroparin|fraxiparin|clopidogrel|prasugrel|ticagrelor|"
    r"aspirin|asa|acetylsalicylic|ticlopidine|cangrelor|abciximab|"
    r"tirofiban|eptifibatide|triflusal|cilostazol|dipyridamole|"
    r"metformin|insulin|sglt-?2|glp-?1|dpp-?4|sulfonylurea|"
    r"steroid|szteroid|methylprednison\w*|methylprednisolon\w*|"
    r"hydrocortison\w*|hydrocortisone|glucocorticoid|glukokortikoid|"
    r"dexamethason\w*|dexamethasone|prednisolon\w*|prednisolone|"
    r"fludrocortison\w*|fludrocortisone|equivalent|equivalence|"
    r"conversion|convert|ekvivalens|ekvivalencia|atvaltas|konverzio|"
    r"epidural|spinal|neuraxial|regional\s+an(?:a|e)esth|catheter|"
    r"kateter|kanul|spinalis|gerinc|regionalis"
    r")\b",
    re.IGNORECASE,
)


def _active_protocol_id(state):
    active = state.get("active_recognized") or {}
    protocol_file = active.get("protocol_file", "")
    parsed = PROTOCOL_PARSED_BY_FILE.get(normalize_path(protocol_file)) if protocol_file else None
    meta = parsed.get("metadata", {}) if parsed else {}
    return meta.get("protocol_id") or active.get("protocol_id") or active.get("key")


_PERIOP_ENTRY_RE = re.compile(
    r"^([^\n:#][^:\n]{2,120}):\n"
    r"(.*?)(?=\n\n[^\n:#][^:\n]{2,120}:\n|\n\n###|\Z)",
    re.MULTILINE | re.DOTALL,
)

_PERIOP_SKIP_TERMS = {
    "periooperative_context_required",
    "perioperative_context_required",
    "target glucose in the perioperative period",
    "other endocrine medications",
}

_PERIOP_MODIFIER_FOLLOWUP_RE = re.compile(
    r"\b(?:epidural|spinal|neuraxial|regional\s+an(?:a|e)esth|catheter|"
    r"puncture|kateter|kanul|spinalis|gerinc|regionalis|hasi|abdominal)\b",
    re.IGNORECASE,
)

_PERIOP_STEROID_QUERY_RE = re.compile(
    r"\b(?:steroid|szteroid|stress\s*dose|stressz\s*dozis|"
    r"methylprednison\w*|methylprednisolon\w*|hydrocortison\w*|"
    r"hydrocortisone|glucocorticoid|glukokortikoid)\b",
    re.IGNORECASE,
)

_PERIOP_STEROID_FOLLOWUP_RE = re.compile(
    r"\b(?:small|minor|medium|major|large|kis|kozepes|közepes|nagy|"
    r"surgery|operation|mutet|műtét|hernia|serv|sérv|lc|pppd|"
    r"dexamethasone|prednisolone|fludrocortisone|equivalent|ekvivalens)\b",
    re.IGNORECASE,
)

_STEROID_EQUIVALENCE_QUERY_RE = re.compile(
    r"\b(?:equivalent|equivalence|equivalency|conversion|convert|"
    r"ekvivalens|ekvivalencia|atvaltas|konverzio)\b",
    re.IGNORECASE,
)

_STEROID_EQUIVALENCE_DATA = {
    "methylprednisone": {
        "display": "methylprednisone",
        "reference_mg": 8.0,
        "activity": "5:0.5",
        "duration": "12-36 h",
        "aliases": ["methylprednisone", "methylprednisolone", "methylprednison", "methylprednisolon"],
    },
    "dexamethasone": {
        "display": "dexamethasone",
        "reference_mg": 1.5,
        "activity": "30:0",
        "duration": "36-54 h",
        "aliases": ["dexamethasone", "dexamethason"],
    },
    "hydrocortisone": {
        "display": "hydrocortisone",
        "reference_mg": 40.0,
        "activity": "1:1",
        "duration": "8-12 h",
        "aliases": ["hydrocortisone", "hydrocortison"],
    },
    "prednisolone": {
        "display": "prednisolone",
        "reference_mg": 10.0,
        "activity": "4:0.8",
        "duration": "12-36 h",
        "aliases": ["prednisolone", "prednisolon"],
    },
    "fludrocortisone": {
        "display": "fludrocortisone",
        "reference_mg": 4.0,
        "activity": "10:250",
        "duration": "24 h",
        "aliases": ["fludrocortisone", "fludrocortison"],
    },
}

_STEROID_ALIAS_TO_KEY = {
    alias: key
    for key, row in _STEROID_EQUIVALENCE_DATA.items()
    for alias in row["aliases"]
}

_STEROID_DOSE_MG_RE = re.compile(r"(?<!\d)(\d+(?:[\.,]\d+)?)\s*mg\b", re.IGNORECASE)

_UNSUPPORTED_STEROID_EQUIVALENCE_RE = re.compile(
    r"\b(?:prednisone|betamethasone|triamcinolone|cortisone|"
    r"budesonide|fluticasone|mometasone|beclomethasone)\b",
    re.IGNORECASE,
)


def _ascii_fold(text):
    folded = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in folded if not unicodedata.combining(ch)).lower()


def _periop_query_tokens(question):
    return re.findall(r"[a-z0-9]+", _ascii_fold(question))


def _periop_entry_terms(header):
    terms = []
    for raw in re.split(r";|,", header or ""):
        term = raw.strip()
        if not term:
            continue
        folded = _ascii_fold(term)
        compact = re.sub(r"[^a-z0-9]+", " ", folded).strip()
        if len(re.sub(r"[^a-z0-9]+", "", compact)) < 4:
            continue
        if compact in _PERIOP_SKIP_TERMS:
            continue
        terms.append((term, compact))
    return terms


def _periop_term_matches(question, term):
    folded_question = _ascii_fold(question)
    if " " in term:
        return term in folded_question
    return any(token.startswith(term) for token in _periop_query_tokens(question))


def _parse_periop_entries(parsed):
    info = parsed.get("info_blocks", "") if parsed else ""
    entries = []
    current_section = ""
    for line in info.splitlines():
        if line.startswith("### "):
            current_section = line[4:].strip()
        # The regex below handles complete entries from the whole INFO_BLOCKS
        # panel; section is used only to mark antithrombotic entries after match.
    for match in _PERIOP_ENTRY_RE.finditer(info):
        header = match.group(1).strip()
        body = match.group(2).strip()
        if not header or not body:
            continue
        if header.lower().startswith("perioperative_context_required"):
            continue
        section_start = info.rfind("\n### ", 0, match.start())
        if section_start >= 0:
            section_end = info.find("\n", section_start + 1)
            if section_end < 0:
                section_end = len(info)
            section_line = info[section_start + 5: section_end]
            current_section = section_line.strip()
        else:
            current_section = ""
        entries.append({"header": header, "body": body, "section": current_section})
    return entries


def _render_periop_entry(entry):
    return f"{entry['header']}:\n{entry['body']}"


def _extract_info_block_section(parsed, section_name):
    info = parsed.get("info_blocks", "") if parsed else ""
    pattern = re.compile(
        r"^###\s+" + re.escape(section_name) + r"\s*$\n(.*?)(?=^###\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(info)
    return match.group(1).strip() if match else None


def _recognized_for_protocol_id(protocol_id):
    for norm_file, parsed in PROTOCOL_PARSED_BY_FILE.items():
        meta = parsed.get("metadata", {}) if parsed else {}
        if meta.get("protocol_id") == protocol_id:
            protocol_file = parsed.get("path") or norm_file
            return {
                "protocol_file": protocol_file,
                "protocol_id": protocol_id,
                "key": protocol_id,
                "display": meta.get("canonical_name") or meta.get("protocol_name") or protocol_id,
                "canonical": meta.get("canonical_name") or protocol_id,
                "source_label": meta.get("source_label") or protocol_id,
                "matched_alias": protocol_id,
                "confidence": "exact",
                "category": "conditions",
            }
    return None


def _find_steroid_equivalence_agent(question):
    folded = _ascii_fold(question or "")
    for alias in sorted(_STEROID_ALIAS_TO_KEY, key=len, reverse=True):
        if re.search(r"\b" + re.escape(alias) + r"\b", folded):
            return _STEROID_ALIAS_TO_KEY[alias]
    return None


def _find_steroid_equivalence_dose(question):
    match = _STEROID_DOSE_MG_RE.search(question or "")
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def _fmt_steroid_mg(value):
    rounded = round(float(value), 3)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:.3f}".rstrip("0").rstrip(".")


def _render_steroid_equivalence(agent_key, dose_mg, parsed):
    source = _STEROID_EQUIVALENCE_DATA[agent_key]
    factor = dose_mg / source["reference_mg"]
    lines = [
        f"Steroid equivalence for {_fmt_steroid_mg(dose_mg)} mg {source['display']}:",
        "",
        "| Steroid | Equivalent dose |",
        "|---|---:|",
    ]
    for row in _STEROID_EQUIVALENCE_DATA.values():
        equivalent = factor * row["reference_mg"]
        lines.append(f"| {row['display']} | {_fmt_steroid_mg(equivalent)} mg |")

    return "\n".join(lines)


def _try_steroid_equivalence_shortcut(state, recognized, question):
    selected = recognized or state.get("active_recognized")
    protocol_id = None
    if selected:
        protocol_file = selected.get("protocol_file", "")
        parsed = PROTOCOL_PARSED_BY_FILE.get(normalize_path(protocol_file)) if protocol_file else None
        meta = parsed.get("metadata", {}) if parsed else {}
        protocol_id = meta.get("protocol_id") or selected.get("protocol_id") or selected.get("key")

    text = question or ""
    wants_equivalence = bool(_STEROID_EQUIVALENCE_QUERY_RE.search(text))
    if protocol_id == "steroid_equivalence":
        should_apply = True
    elif protocol_id in {"periop_gyogyszerek", "periop_steroids"} and wants_equivalence:
        should_apply = True
    else:
        should_apply = False
    if not should_apply:
        return None

    equivalence_recognized = _recognized_for_protocol_id("steroid_equivalence")
    if not equivalence_recognized:
        return None
    equivalence_parsed = PROTOCOL_PARSED_BY_FILE.get(
        normalize_path(equivalence_recognized.get("protocol_file", ""))
    )

    agent_key = _find_steroid_equivalence_agent(text)
    dose_mg = _find_steroid_equivalence_dose(text)

    state["active_recognized"] = equivalence_recognized
    _update_routing_state(state, equivalence_recognized, "steroid_equivalence_shortcut")

    if not agent_key:
        if _UNSUPPORTED_STEROID_EQUIVALENCE_RE.search(text):
            return (
                "I can calculate equivalence only for methylprednisone, dexamethasone, "
                "hydrocortisone, prednisolone, and fludrocortisone."
            )
        return "Please provide the steroid and dose in mg, for example: methylprednisone 8 mg."
    if dose_mg is None:
        return f"Please provide the { _STEROID_EQUIVALENCE_DATA[agent_key]['display'] } dose in mg."

    return _render_steroid_equivalence(agent_key, dose_mg, equivalence_parsed)


def _try_periop_steroid_shortcut(state, protocol_id, question):
    if protocol_id == "periop_gyogyszerek" and not _PERIOP_STEROID_QUERY_RE.search(question or ""):
        return None
    if protocol_id not in {"periop_gyogyszerek", "periop_steroids"}:
        return None

    steroid_recognized = _recognized_for_protocol_id("periop_steroids")
    if not steroid_recognized:
        return None
    steroid_parsed = PROTOCOL_PARSED_BY_FILE.get(normalize_path(steroid_recognized.get("protocol_file", "")))
    body = _extract_info_block_section(steroid_parsed, "steroid_guide_full")
    if not body:
        return None
    state["active_recognized"] = steroid_recognized
    return body


def _try_periop_info_shortcut(state, recognized, question):
    selected = recognized or state.get("active_recognized")
    if not selected:
        return None
    protocol_file = selected.get("protocol_file", "")
    parsed = PROTOCOL_PARSED_BY_FILE.get(normalize_path(protocol_file)) if protocol_file else None
    meta = parsed.get("metadata", {}) if parsed else {}
    protocol_id = meta.get("protocol_id") or selected.get("protocol_id") or selected.get("key")
    steroid_body = _try_periop_steroid_shortcut(state, protocol_id, question)
    if steroid_body is not None:
        return steroid_body
    if protocol_id != "periop_gyogyszerek":
        return None

    entries = _parse_periop_entries(parsed)
    for entry in entries:
        for _display, term in _periop_entry_terms(entry["header"]):
            if _periop_term_matches(question, term):
                state["last_periop_entry"] = dict(entry)
                return _render_periop_entry(entry)

    last_entry = state.get("last_periop_entry")
    if last_entry and _PERIOP_MODIFIER_FOLLOWUP_RE.search(question or ""):
        return _render_periop_entry(last_entry)

    return None


def _looks_like_active_protocol_followup(question, state):
    if not state.get("active_recognized"):
        return False
    text = question or ""
    active_protocol_id = _active_protocol_id(state)
    if active_protocol_id == "periop_gyogyszerek" and _PERIOP_FOLLOWUP_RE.search(text):
        return True
    if active_protocol_id == "periop_steroids" and (
        _PERIOP_STEROID_QUERY_RE.search(text) or _PERIOP_STEROID_FOLLOWUP_RE.search(text)
    ):
        return True
    if active_protocol_id == "steroid_equivalence" and (
        _STEROID_EQUIVALENCE_QUERY_RE.search(text)
        or _find_steroid_equivalence_agent(text)
        or _find_steroid_equivalence_dose(text) is not None
    ):
        return True
    if _is_slot_clear_phrase(text):
        return True
    if classify_intent(text) != "unknown":
        return True
    active = state.get("active_recognized") or {}
    protocol_file = active.get("protocol_file", "")
    parsed = PROTOCOL_PARSED_BY_FILE.get(normalize_path(protocol_file)) if protocol_file else None
    if parsed and extract_slots_from_query(text, parsed_protocol=parsed):
        return True
    if extract_slots_from_query(text):
        return True
    if re.search(r"\b(?:actually|instead|rather|but|not|nem|hanem|helyett)\b", text, re.IGNORECASE):
        return True
    return False


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

def _ask_ai_impl(question, chat_id):
    t_start = time.monotonic()
    state   = get_chat_state(chat_id)
    raw_user_text = question
    active_before = dict(state.get("active_recognized") or {})
    state.pop("_last_selection_trace", None)

    # Silent reset of trees that have been idle too long.
    if maybe_auto_reset_tree(state):
        print(f"[dispatcher] tree idle-reset for chat {chat_id}")

    # Explicit reset phrase short-circuit ("új beteg" / "new case" / etc.).
    if is_explicit_reset_phrase(question):
        reset_patient_state(state)
        ack = ("OK, kitöröltem az aktuális folyamatot. Mit segítsek?"
               if _user_language(question) == "hu"
               else "OK, cleared the current flow. How can I help?")
        turn = _new_turn_context(
            raw_user_text=raw_user_text, chat_id=chat_id, state=state,
            active_before=active_before, selected_recognized=None,
            intent="reset",
        )
        envelope = _make_answer_envelope(
            state=state, turn_context=turn, recognized=None,
            active_before=active_before, final_body=ack, final_answer=ack,
            deterministic_or_llm="deterministic_reset", llm_called=False,
            blocked_reason="explicit_reset",
        )
        _remember_answer(state, question, ack)
        _log_answer_envelope(t_start, chat_id, question, None, envelope)
        return ack

    if _is_admin_debug_note(question):
        body = "Admin/debug note ignored; no patient facts or protocol slots were changed."
        turn = _new_turn_context(
            raw_user_text=raw_user_text, chat_id=chat_id, state=state,
            active_before=active_before, selected_recognized=None,
            intent="debug_note",
        )
        answer = finalize_answer(body, None, None)
        envelope = _make_answer_envelope(
            state=state, turn_context=turn, recognized=None,
            active_before=active_before, final_body=body, final_answer=answer,
            deterministic_or_llm="deterministic_policy", llm_called=False,
            blocked_reason="admin_debug_note",
        )
        _log_answer_envelope(t_start, chat_id, question, None, envelope)
        return answer

    forced_recognized = None
    pending_bounds_action = _resolve_pending_out_of_bounds_confirmation(state, question)
    if pending_bounds_action:
        if pending_bounds_action.get("answer_body") is not None:
            body = pending_bounds_action["answer_body"]
            selected = pending_bounds_action.get("recognized")
            source_label = (selected or {}).get("source_label")
            answer = finalize_answer(body, None, source_label)
            turn = _new_turn_context(
                raw_user_text=raw_user_text,
                chat_id=chat_id,
                state=state,
                active_before=active_before,
                selected_recognized=selected,
                confirmation_pending=True,
                confirmation_required=False,
                intent="out_of_bounds_confirmation",
            )
            envelope = _make_answer_envelope(
                state=state,
                turn_context=turn,
                recognized=selected,
                active_before=active_before,
                final_body=body,
                final_answer=answer,
                deterministic_or_llm="deterministic_confirmation",
                llm_called=False,
                slots=pending_bounds_action.get("slots", {}),
                blocked_reason=pending_bounds_action.get("blocked_reason"),
            )
            _remember_answer(state, raw_user_text, answer)
            _log_answer_envelope(t_start, chat_id, raw_user_text, selected, envelope)
            return answer
        question = pending_bounds_action.get("question", question)
        forced_recognized = pending_bounds_action.get("recognized")

    pending_context = state.get("pending_context_confirmation")
    if pending_context:
        if _YES_RE.match(question):
            forced_recognized = pending_context.get("recognized")
            if pending_context.get("type") == "fuzzy_alias" and forced_recognized:
                forced_recognized = dict(forced_recognized)
                forced_recognized["confirmed_from_confidence"] = forced_recognized.get("confidence")
                forced_recognized["confidence"] = "high"
                forced_recognized["confirmed_by_user"] = True
            question = pending_context.get("question") or question
            state["pending_context_confirmation"] = None
        elif _NO_RE.match(question):
            state["pending_context_confirmation"] = None
            ack = (
                "OK, I will not apply that message to the active protocol. "
                "Please name the protocol or drug you want to use."
            )
            turn = _new_turn_context(
                raw_user_text=raw_user_text, chat_id=chat_id, state=state,
                active_before=active_before, selected_recognized=None,
                confirmation_pending=True,
            )
            envelope = _make_answer_envelope(
                state=state, turn_context=turn, recognized=None,
                active_before=active_before, final_body=ack, final_answer=ack,
                deterministic_or_llm="deterministic_confirmation", llm_called=False,
                blocked_reason="context_confirmation_no",
            )
            _remember_answer(state, question, ack)
            _log_answer_envelope(t_start, chat_id, question, None, envelope)
            return ack
        else:
            state["pending_context_confirmation"] = None

    unsupported_hit = _detect_unsupported_policy(question)
    unsupported_syndrome = unsupported_hit.get("key") if unsupported_hit else None
    unsupported_matched_term = unsupported_hit.get("matched_term") if unsupported_hit else None
    unsupported_message = unsupported_hit.get("message") if unsupported_hit else None
    normalized_question, recognized = normalize_question(question)
    if forced_recognized:
        recognized = forced_recognized
        normalized_question = (
            question
            + f"\n\n[Continuing context: {recognized.get('display', '')} / "
            + f"{recognized.get('canonical', recognized.get('display', ''))}]"
        )

    # -- Intent classification (Session 8) ---
    state["last_user_intent"] = classify_intent(question)
    turn = _new_turn_context(
        raw_user_text=raw_user_text,
        chat_id=chat_id,
        state=state,
        active_before=active_before,
        fresh_recognized=recognized,
        selected_recognized=recognized or state.get("active_recognized"),
        unsupported_syndrome=unsupported_syndrome,
        unsupported_matched_term=unsupported_matched_term,
        unsupported_message=unsupported_message,
        intent=state["last_user_intent"],
        normalized_question=normalized_question,
        confirmation_pending=bool(pending_context),
    )

    explicit_drug_allowed = (
        recognized
        and recognized.get("category") == "drugs"
        and (not unsupported_hit or unsupported_hit.get("allowed_if_explicit_drug", True))
    )
    if unsupported_hit and not explicit_drug_allowed:
        recognized = None
        normalized_question = question
        body = unsupported_message
        answer = finalize_answer(body, None, None)
        _update_turn_after_selection(turn, state, None)
        envelope = _make_answer_envelope(
            state=state, turn_context=turn, recognized=None,
            active_before=active_before, final_body=body, final_answer=answer,
            deterministic_or_llm="deterministic_policy", llm_called=False,
            unsupported_syndrome=unsupported_syndrome,
            unsupported_matched_term=unsupported_matched_term,
            unsupported_message=unsupported_message,
            unsupported_action="blocked",
            blocked_reason="unsupported_syndrome",
        )
        _remember_answer(state, question, answer)
        _log_answer_envelope(t_start, chat_id, question, None, envelope)
        return answer

    if recognized and recognized.get("confidence") == "medium":
        label = recognized.get("display") or recognized.get("source_label") or "that protocol"
        matched = recognized.get("matched_alias") or label
        body = (
            f"Did you mean {label} when you wrote '{matched}'? "
            "Reply yes to use that protocol, or no and name the supported drug/protocol you want."
        )
        state["pending_context_confirmation"] = {
            "type": "fuzzy_alias",
            "question": question,
            "recognized": recognized,
        }
        turn.selected_recognized = None
        turn.confirmation_required = True
        answer = finalize_answer(body, None, None)
        envelope = _make_answer_envelope(
            state=state, turn_context=turn, recognized=None,
            active_before=active_before, final_body=body, final_answer=answer,
            deterministic_or_llm="deterministic_confirmation", llm_called=False,
            blocked_reason="fuzzy_alias_confirmation",
            confirmation_required=True,
        )
        _remember_answer(state, question, answer)
        _log_answer_envelope(t_start, chat_id, question, None, envelope)
        return answer

    if (not recognized
            and not state.get("active_recognized")
            and _is_obvious_nonclinical_message(question)):
        body = (
            "No active clinical protocol is selected, and this message does not match an uploaded "
            "clinical protocol. Please name a supported drug, protocol, or result if you want "
            "protocol-based guidance."
        )
        answer = finalize_answer(body, None, None)
        _update_turn_after_selection(turn, state, None)
        envelope = _make_answer_envelope(
            state=state, turn_context=turn, recognized=None,
            active_before=active_before, final_body=body, final_answer=answer,
            deterministic_or_llm="deterministic_policy", llm_called=False,
            blocked_reason="out_of_scope_no_protocol",
        )
        _remember_answer(state, question, answer)
        _log_answer_envelope(t_start, chat_id, question, None, envelope)
        return answer

    # -- Context source tracking / early activation ---
    if recognized and recognized.get("confidence") in ("exact", "high"):
        previous = state.get("active_recognized")
        if (previous and previous.get("protocol_file")
                and previous.get("protocol_file") != recognized.get("protocol_file")):
            state["last_recommended_antibiotics"] = []
        state["active_recognized"] = recognized
        _update_routing_state(state, recognized, "fresh_alias")
        state["collected_slots"] = _get_protocol_slots(state, recognized)

    if (not recognized
            and state.get("active_recognized")
            and not _looks_like_active_protocol_followup(question, state)):
        active = state.get("active_recognized")
        label = active.get("display") or active.get("source_label") or _label_for_protocol(active.get("protocol_file", ""))
        body = (
            f"I still have {label} as the active protocol, but I am not sure this message belongs to it. "
            f"Reply yes to apply it to {label}, no to leave the protocol unchanged, or name a different supported protocol."
        )
        state["pending_context_confirmation"] = {
            "question": question,
            "recognized": active,
        }
        answer = finalize_answer(body, None, active.get("source_label"))
        envelope = _make_answer_envelope(
            state=state, turn_context=turn, recognized=active,
            active_before=active_before, final_body=body, final_answer=answer,
            deterministic_or_llm="deterministic_confirmation", llm_called=False,
            blocked_reason="unclear_followup",
            confirmation_required=True,
        )
        _remember_answer(state, question, answer)
        _log_answer_envelope(t_start, chat_id, question, active, envelope)
        return answer

    # -- Organism-only disambiguation ---
    organism_prompt = _handle_organism_disambiguation(state, question, recognized)
    if organism_prompt is not None:
        source_label = recognized.get("source_label") if recognized else None
        answer = finalize_answer(organism_prompt, None, source_label)
        envelope = _make_answer_envelope(
            state=state, turn_context=turn, recognized=recognized,
            active_before=active_before, final_body=organism_prompt,
            final_answer=answer,
            deterministic_or_llm="deterministic_disambiguation",
            llm_called=False,
            blocked_reason="organism_disambiguation",
        )
        _remember_answer(state, question, answer)
        _log_answer_envelope(t_start, chat_id, question, recognized, envelope)
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
        selected = recognized or active
        envelope = _make_answer_envelope(
            state=state, turn_context=turn, recognized=selected,
            active_before=active_before, final_body=dosing_shortcut,
            final_answer=answer,
            deterministic_or_llm="deterministic_shortcut", llm_called=False,
            blocked_reason="dosing_shortcut",
        )
        _remember_answer(state, question, answer)
        _log_answer_envelope(t_start, chat_id, question, recognized, envelope)
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
        selected = recognized or active
        envelope = _make_answer_envelope(
            state=state, turn_context=turn, recognized=selected,
            active_before=active_before, final_body=dispatch_body,
            final_answer=answer,
            deterministic_or_llm="deterministic_tree", llm_called=False,
        )
        _remember_answer(state, question, answer)
        _log_answer_envelope(t_start, chat_id, question, recognized, envelope)
        return answer

    # -- Steroid equivalence calculator shortcut ---
    steroid_equivalence_body = _try_steroid_equivalence_shortcut(
        state, recognized or state.get("active_recognized"), question
    )
    if steroid_equivalence_body is not None:
        active = state.get("active_recognized") or recognized
        source_label = (active or {}).get("source_label")
        _pf = (active or {}).get("protocol_file")
        _parsed_steroid_eq = PROTOCOL_PARSED_BY_FILE.get(normalize_path(_pf)) if _pf else None
        _footer_steroid_eq = _parsed_steroid_eq.get("default_footer") if _parsed_steroid_eq else None
        answer = finalize_answer(steroid_equivalence_body, _footer_steroid_eq, source_label)
        envelope = _make_answer_envelope(
            state=state, turn_context=turn, recognized=active,
            active_before=active_before, final_body=steroid_equivalence_body,
            final_answer=answer,
            deterministic_or_llm="deterministic_steroid_equivalence",
            llm_called=False,
        )
        _remember_answer(state, question, answer)
        _log_answer_envelope(t_start, chat_id, question, active, envelope)
        return answer

    # -- Perioperative medication exact-entry shortcut ---
    periop_body = _try_periop_info_shortcut(state, recognized or state.get("active_recognized"), question)
    if periop_body is not None:
        active = state.get("active_recognized") or recognized
        source_label = (active or {}).get("source_label")
        _pf = (active or {}).get("protocol_file")
        _parsed_periop = PROTOCOL_PARSED_BY_FILE.get(normalize_path(_pf)) if _pf else None
        _footer_periop = _parsed_periop.get("default_footer") if _parsed_periop else None
        answer = finalize_answer(periop_body, _footer_periop, source_label)
        envelope = _make_answer_envelope(
            state=state, turn_context=turn, recognized=active,
            active_before=active_before, final_body=periop_body,
            final_answer=answer,
            deterministic_or_llm="deterministic_periop_info",
            llm_called=False,
        )
        _remember_answer(state, question, answer)
        _log_answer_envelope(t_start, chat_id, question, active, envelope)
        return answer

    # ----- Deterministic selection engine (Session 9) -----
    lang = _user_language(question)
    _active_for_det = recognized or state.get("active_recognized")
    det_body = _try_deterministic_selection(state, _active_for_det, question, lang)
    if det_body is not None:
        active = state.get("active_recognized")
        selection_trace = state.get("_last_selection_trace") or {}
        source_label = (active or recognized or {}).get("source_label")
        _pf = (active or recognized or {}).get("protocol_file")
        _parsed_det = PROTOCOL_PARSED_BY_FILE.get(normalize_path(_pf)) if _pf else None
        _footer_det = _parsed_det.get("default_footer") if _parsed_det else None
        answer = finalize_answer(det_body, _footer_det, source_label)
        selected = recognized or state.get("active_recognized")
        slots = selection_trace.get("slots") or state.get("collected_slots")
        envelope = _make_answer_envelope(
            state=state, turn_context=turn, recognized=selected,
            active_before=active_before, final_body=det_body,
            final_answer=answer,
            deterministic_or_llm="deterministic_selection",
            llm_called=False,
            selection_result=selection_trace,
            slots=slots,
            unsupported_syndrome=unsupported_syndrome,
            unsupported_matched_term=unsupported_matched_term,
            unsupported_message=unsupported_message,
            unsupported_action="ignored_explicit_drug" if unsupported_syndrome else None,
        )
        _remember_answer(state, question, answer)
        _log_answer_envelope(t_start, chat_id, question, selected, envelope)
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
    _remember_answer(state, question, answer)

    # Structured audit log — written after response is ready
    envelope = _make_answer_envelope(
        state=state, turn_context=turn, recognized=recognized,
        active_before=active_before, final_body=raw_answer,
        final_answer=answer, retrieved_chunks=retrieved_chunks,
        deterministic_or_llm="llm_rag", llm_called=True,
        slots=_get_protocol_slots(state, recognized) if recognized else {},
        unsupported_syndrome=unsupported_syndrome,
        unsupported_matched_term=unsupported_matched_term,
        unsupported_message=unsupported_message,
        unsupported_action="ignored_explicit_drug" if unsupported_syndrome and explicit_drug_allowed else None,
    )
    _log_answer_envelope(t_start, chat_id, question, recognized, envelope)

    return answer


def ask_ai(question, chat_id):
    t_start = time.monotonic()
    try:
        return _ask_ai_impl(question, chat_id)
    except Exception as exc:
        _log_safe_runtime_failure(t_start, chat_id, question, exc, "ask_ai")
        return SAFE_RUNTIME_FAILURE_MESSAGE


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
        return None, _get_protocol_slots(state, recognized), "deterministic decision-tree dispatcher"
    if mode in ("", "none"):
        return None, _get_protocol_slots(state, recognized), "LLM-generated RAG path"
    slots = extract_slots_from_query(
        question,
        parsed_protocol=parsed,
        existing_slots=_get_protocol_slots(state, recognized),
    )
    result = run_selection(parsed, slots, lang=_user_language(question))
    if result.no_match:
        return result, slots, "LLM-generated RAG path (deterministic engine no-match)"
    return result, slots, f"deterministic selection_engine ({result.mode_used})"


def build_debug_trace(debug_question, chat_id):
    unsupported_hit = _detect_unsupported_policy(debug_question)
    unsupported_syndrome = unsupported_hit.get("key") if unsupported_hit else None
    unsupported_matched_term = unsupported_hit.get("matched_term") if unsupported_hit else None
    unsupported_message = unsupported_hit.get("message") if unsupported_hit else None
    normalized_question, fresh_recognized = normalize_question(debug_question)
    state = get_chat_state(chat_id)
    state_before = _format_state_snapshot(state)
    active = state.get("active_recognized")
    recognized = fresh_recognized
    unsupported_action = "none"

    explicit_drug_allowed = (
        fresh_recognized
        and fresh_recognized.get("category") == "drugs"
        and (not unsupported_hit or unsupported_hit.get("allowed_if_explicit_drug", True))
    )
    if unsupported_hit and not explicit_drug_allowed:
        recognized = None
        active = None
        context_source = "unsupported_syndrome"
        unsupported_action = "blocked"
        reason = (
            f"unsupported syndrome '{unsupported_syndrome}' matched term "
            f"'{unsupported_matched_term}'; no supported explicit drug alias"
        )
    elif fresh_recognized:
        context_source = "fresh_alias"
        if unsupported_syndrome and fresh_recognized.get("category") == "drugs":
            unsupported_action = "ignored_explicit_drug"
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
    if unsupported_action == "blocked":
        output_source = "deterministic policy block (unsupported_syndrome)"
    selected_key = getattr(selection_result, "output_key", None) if selection_result else None
    default_used = getattr(selection_result, "default_used", None) if selection_result else None
    turn = _new_turn_context(
        raw_user_text=debug_question,
        chat_id=chat_id,
        state=state,
        active_before=active,
        fresh_recognized=fresh_recognized,
        selected_recognized=recognized,
        unsupported_syndrome=unsupported_syndrome,
        unsupported_matched_term=unsupported_matched_term,
        unsupported_message=unsupported_message,
        intent=classify_intent(debug_question),
        normalized_question=normalized_question,
    )
    turn.protocol_slots_after = dict(slots or {})
    trace = _make_answer_trace(
        state=state,
        recognized=recognized,
        active_before=active,
        deterministic_or_llm=output_source,
        llm_called=output_source.startswith("LLM-generated"),
        selection_result=selection_result,
        slots=slots,
        unsupported_syndrome=unsupported_syndrome,
        unsupported_matched_term=unsupported_matched_term,
        unsupported_message=unsupported_message,
        unsupported_action=unsupported_action,
        blocked_reason="unsupported_syndrome" if unsupported_action == "blocked" else None,
        turn_context=turn,
        retrieved_chunks=retrieved_chunks,
    )

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
        f"LLM called: {str(trace.get('llm_called')).lower()}",
        f"Selection output: {selected_key or ('default' if default_used else 'n/a')}",
        "Collected slots: " + (json.dumps(slots, ensure_ascii=False, sort_keys=True) if slots else "none"),
        f"Unsupported syndrome: {unsupported_syndrome or 'none'}",
        f"Unsupported key: {unsupported_syndrome or 'none'}",
        f"Unsupported matched term: {unsupported_matched_term or 'none'}",
        f"Unsupported action: {unsupported_action}",
        "Answer trace: " + json.dumps(trace, ensure_ascii=False, sort_keys=True),
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

async def _safe_reply_text(update, text):
    try:
        await update.message.reply_text(text)
        return True
    except Exception as exc:
        chat_id = _effective_chat_id(update)
        _log_runtime_error("telegram_reply_text", exc, chat_id, text)
        return False


async def _safe_send_action(update, action):
    try:
        await update.message.chat.send_action(action=action)
        return True
    except Exception as exc:
        chat_id = _effective_chat_id(update)
        _log_runtime_error("telegram_send_action", exc, chat_id)
        return False


async def _safe_reply_chunks(update, text):
    ok = True
    for chunk in split_message(text):
        if not await _safe_reply_text(update, chunk):
            ok = False
            break
    return ok


UNAUTHORIZED_MESSAGE = (
    "Ez a bot kÃ³rhÃ¡zi dolgozÃ³k szÃ¡mÃ¡ra Ã©rhetÅ‘ el. "
    "Ha jogosult vagy a hozzÃ¡fÃ©rÃ©sre, kÃ©rj meghÃ­vÃ³t."
)


def _effective_user_id(update):
    user = getattr(update, "effective_user", None)
    return getattr(user, "id", None)


def _effective_chat_id(update):
    chat = getattr(update, "effective_chat", None)
    if chat is not None and getattr(chat, "id", None) is not None:
        return chat.id
    message = getattr(update, "message", None)
    message_chat = getattr(message, "chat", None)
    if message_chat is not None and getattr(message_chat, "id", None) is not None:
        return message_chat.id
    user_id = _effective_user_id(update)
    return f"user:{user_id}" if user_id is not None else "unknown-chat"


async def _reply_unauthorized(update):
    await _safe_reply_text(update, UNAUTHORIZED_MESSAGE)


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _safe_reply_text(update, START_MESSAGE)


async def handle_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _effective_user_id(update)
    await _safe_reply_text(update, f"Telegram user id: {user_id}")


async def handle_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(_effective_user_id(update)):
        return
    chat_id = _effective_chat_id(update)
    CONVERSATION_STATE.pop(chat_id, None)
    await _safe_reply_text(update, "Conversation history cleared.")


async def handle_protocols(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(_effective_user_id(update)):
        await _safe_reply_text(update,
            "Ez a bot kórházi dolgozók számára érhető el. "
            "Ha jogosult vagy a hozzáférésre, kérj meghívót."
        )
        return
    await _safe_reply_chunks(update, format_protocols_output())


async def handle_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(_effective_user_id(update)):
        await _safe_reply_text(update,
            "Ez a bot kórházi dolgozók számára érhető el. "
            "Ha jogosult vagy a hozzáférésre, kérj meghívót."
        )
        return
    await _safe_reply_text(update, format_version_output())


async def handle_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _effective_user_id(update)
    if not _is_allowed(user_id) or not _is_admin(user_id):
        await _safe_reply_text(update, "Authorization: blocked for admin command.")
        return
    await _safe_reply_text(update,
        "Reload deferred. TODO: implement an atomic admin-only reload that rebuilds "
        "aliases, parsed protocols, chunks, and embeddings without serving a half-loaded state."
    )


async def handle_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(_effective_user_id(update)):
        await _safe_reply_text(update,
            "DEBUG - routing trace\n"
            "Authorization: blocked\n"
            "Blocked output: user is not authorized to run debug commands."
        )
        return
    chat_id = _effective_chat_id(update)
    # context.args contains words after /debug
    debug_question = " ".join(context.args).strip() if context.args else ""

    if not debug_question:
        await _safe_reply_text(update,
            "Please provide a question after /debug\nExample: /debug meropenem septic shock"
        )
        return

    answer = build_debug_trace(debug_question, chat_id)

    await _safe_reply_chunks(update, answer)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    question = update.message.text.strip()
    if not question:
        return

    user_id = _effective_user_id(update)
    if not _is_allowed(user_id):
        await _safe_reply_text(update,
            "Ez a bot kórházi dolgozók számára érhető el. "
            "Ha jogosult vagy a hozzáférésre, kérj meghívót."
        )
        return

    chat_id = _effective_chat_id(update)
    await _safe_send_action(update, "typing")

    answer = ask_ai(question, chat_id)

    await _safe_reply_chunks(update, answer)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    run_startup_checks()
    global _query_log, ALLOWED_USER_IDS, ADMIN_USER_IDS
    _refresh_runtime_settings()
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

    _maybe_run_alias_sync_on_startup()
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
    app.add_handler(CommandHandler("startup", handle_start))
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
