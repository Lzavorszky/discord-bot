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
import html
import hashlib
import sqlite3
import logging
import time
import unicodedata
from logging.handlers import RotatingFileHandler
from pathlib import Path

import authorization as authorization_helpers
import logging_audit as audit_helpers
import state as state_helpers
from openai import OpenAI
from rota_lookup import (
    STATUS_CONFIGURATION_ERROR,
    STATUS_FOUND,
    STATUS_MULTIPLE_MATCHES,
    STATUS_NOT_FOUND,
    STATUS_PERMISSION_ERROR,
    STATUS_SHEET_ERROR,
    lookup_daily_summary,
    lookup_daily_person,
    lookup_long_summary,
    lookup_oncall_summary,
    lookup_role_locations,
    normalize_role as normalize_rota_role,
    parse_date_input as parse_rota_date_input,
)
from nursing_rota_lookup import lookup_nursing_rota
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters

try:
    from rapidfuzz import process, fuzz
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False

# Old-pipeline decommissioned (Part C): alias_sync / selection_engine removed.
# These flags remain False so any residual dead references stay import-safe.
ALIAS_SYNC_AVAILABLE = False
SELECTION_ENGINE_AVAILABLE = False


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
DEBUG_LOGGING_OPTIONS = dict(RUNTIME_OPTIONS.get("debug_logging") or {})
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


def _full_conversation_log_enabled():
    return (
        _local_debug_enabled()
        or _env_flag("FULL_CONVERSATION_LOG")
        or bool(DEBUG_LOGGING_OPTIONS.get("stdout_full_turns"))
    )


def _effective_access_mode():
    if _local_debug_enabled() and not (
        RUNTIME_OPTIONS.get("_access_mode_explicit")
    ):
        return "open"
    return str(RUNTIME_OPTIONS.get("access_mode") or "closed").strip().lower()


def _refresh_runtime_settings():
    global RUNTIME_OPTIONS, ACCESS_MODE, DEBUG_LOGGING_OPTIONS, FULL_CONVERSATION_LOG
    RUNTIME_OPTIONS = get_runtime_options()
    DEBUG_LOGGING_OPTIONS = dict(RUNTIME_OPTIONS.get("debug_logging") or {})
    ACCESS_MODE = _effective_access_mode()
    FULL_CONVERSATION_LOG = _full_conversation_log_enabled()
    return RUNTIME_OPTIONS


_refresh_runtime_settings()


def _format_debug_logging_summary():
    if not DEBUG_LOGGING_OPTIONS:
        return "unavailable"
    enabled = [
        key for key, value in sorted(DEBUG_LOGGING_OPTIONS.items())
        if bool(value)
    ]
    disabled = [
        key for key, value in sorted(DEBUG_LOGGING_OPTIONS.items())
        if not bool(value)
    ]
    enabled_text = ", ".join(enabled) if enabled else "none"
    disabled_text = ", ".join(disabled) if disabled else "none"
    return f"enabled: {enabled_text}; disabled: {disabled_text}"

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

    # Part C: the old clinical pipeline's startup validation (rule files,
    # aliases.json, protocols/ corpus, protocol linter) is decommissioned. The
    # id_bot2 pipeline owns and validates its own protocol library under
    # id_bot2/protocols/ (see id_bot2/validate_protocols.py + its test-suite).

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
    return audit_helpers._safe_user_message_for_log(
        user_message,
        FULL_CONVERSATION_LOG,
        preserve_user_message=DEBUG_LOGGING_OPTIONS.get("log_user_messages"),
    )



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
        debug_logging_options=DEBUG_LOGGING_OPTIONS,
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


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Alias recognition
# ---------------------------------------------------------------------------

_OBVIOUS_NONCLINICAL_RE = re.compile(
    r"^\s*(?:"
    r"hi|hello|hey|good morning|good afternoon|good evening|"
    r"how are you\??|thanks?|thank you|ok|okay|"
    r"what is the capital of .+|capital of .+|"
    r"tell me a joke|what'?s the weather\??"
    r")\s*$",
    re.IGNORECASE,
)


_EXPLICIT_HEIGHT_CM_RE = re.compile(r"\b\d+(?:\.\d+)?\s*cm\b", re.IGNORECASE)
_EXPLICIT_WEIGHT_KG_RE = re.compile(r"\b\d+(?:\.\d+)?\s*kg\b", re.IGNORECASE)
_ECHO_LVOT_VTI_RE = re.compile(r"\blvot\s+vti\s*[:=]?\s*\d+(?:\.\d+)?\s*(?:mm|cm)\b", re.IGNORECASE)
_ECHO_LVOT_DIAMETER_RE = re.compile(
    r"\blvot\s+(?:diam(?:eter)?|d)\s*[:=]?\s*\d+(?:\.\d+)?\s*(?:mm|cm)\b",
    re.IGNORECASE,
)
_ECHO_AV_VTI_RE = re.compile(r"\b(?:av|aortic(?:\s+valve)?)\s+vti\s*[:=]?\s*\d+(?:\.\d+)?\s*(?:mm|cm)\b", re.IGNORECASE)
_ECHO_CO_INTENT_RE = re.compile(r"\b(?:cardiac\s+output|heart\s*rate|hr|co)\b", re.IGNORECASE)


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


# ---------------------------------------------------------------------------
# Protocol parser — extracted to protocol_parser.py
# All names re-exported here for backward compatibility.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Response post-processing — extracted to postprocess.py; re-imported here
# for backward compatibility.
# ---------------------------------------------------------------------------


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


_CORRECTION_RE = re.compile(
    r"\b(?:not|nem|actually|instead|rather|correction|correct|but|hanem|helyett)\b",
    re.IGNORECASE,
)


_ADMIN_DEBUG_NOTE_RE = re.compile(
    r"^\s*(?:"
    r"/debug\b|"
    r"debug\s*:|debug\s+(?:note|not)\s*:|"
    r"dedebug\s+(?:note|not)\s*:|deubg\s*:|"
    r"admin\s*:|note\s+to\s+self\s*:|audit\s*:|log\s*:|todo\s*:)"
    ,
    re.IGNORECASE,
)


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


_PCR_PANEL_CLARIFICATION_ALIASES = [
    (
        re.compile(
            r"^\s*(?:ji|ji\s*(?:-|/)?\s*pcr|biofire\s+ji|"
            r"joint\s+infection(?:\s+(?:panel|pcr))?)\s*$",
            re.IGNORECASE,
        ),
        "joint infection panel",
    ),
    (
        re.compile(
            r"^\s*(?:pn|pn\s*(?:-|/)?\s*pcr|biofire\s+pn|"
            r"pneumonia(?:\s+(?:panel|pcr))?)\s*$",
            re.IGNORECASE,
        ),
        "pneumonia pcr",
    ),
]


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


# ---------------------------------------------------------------------------
# Cross-protocol handoff helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Deterministic selection engine integration (Session 9)
# ---------------------------------------------------------------------------

_CLEAR_SLOT_RE = re.compile(
    r"\b(?:delete|clear|remove|forget|reset)\s+(?:previous\s+)?"
    r"(?:pathogens?|organisms?|genes?|results?|slots?|data|facts?|"
    r"gfr|egfr|crcl|renal|kidney|weight|body\s+weight|indication|status)\b",
    re.IGNORECASE,
)


_YES_RE = re.compile(r"^\s*(?:yes|y|igen|ja|apply|use it)\s*[\.\?!]*\s*$", re.IGNORECASE)
_NO_RE = re.compile(r"^\s*(?:no|n|nem|nope|cancel|do not)\s*[\.\?!]*\s*$", re.IGNORECASE)
_YES_PREFIX_RE = re.compile(r"^\s*(?:yes|y|igen|ja)\b", re.IGNORECASE)
_NO_PREFIX_RE = re.compile(r"^\s*(?:no|n|nem|nope)\b", re.IGNORECASE)


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
_PERIOP_SHORT_ENTRY_TERMS = {
    "asa",
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
        "aliases": ["dexamethasone", "dexamethason", "dexa"],
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


# ---------------------------------------------------------------------------
# AI answer generation (cont.)
# ---------------------------------------------------------------------------

def _answer_via_id_bot2(question, chat_id):
    """Delegate to the new id_bot2 pipeline through its Channel adapter. Kept as a
    single integration point a messaging channel calls (Plan D principle 8)."""
    from id_bot2 import channel as _idch
    return _idch.answer_for(question, chat_id)


def ask_ai(question, chat_id):
    # Part C: the old clinical pipeline (_ask_ai_impl) is decommissioned. All
    # questions are answered by the id_bot2 pipeline via the Channel adapter.
    t_start = time.monotonic()
    try:
        return _answer_via_id_bot2(question, chat_id)
    except Exception as exc:
        _log_safe_runtime_failure(t_start, chat_id, question, exc, "ask_ai")
        return SAFE_RUNTIME_FAILURE_MESSAGE


# ---------------------------------------------------------------------------
# Debug output
# ---------------------------------------------------------------------------

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


def _short_build_id(value):
    value = str(value or "").strip()
    return value[:12] if value else ""


def get_build_id():
    for env_name in (
        "BOT_BUILD_SHA",
        "RAILWAY_GIT_COMMIT_SHA",
        "SOURCE_VERSION",
        "GIT_COMMIT_SHA",
        "COMMIT_SHA",
    ):
        build_id = _short_build_id(os.getenv(env_name))
        if build_id:
            return build_id

    try:
        head_path = Path(".git") / "HEAD"
        head = head_path.read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref_path = Path(".git") / head.split(":", 1)[1].strip()
            return _short_build_id(ref_path.read_text(encoding="utf-8").strip())
        return _short_build_id(head)
    except OSError:
        return "unavailable"


def format_protocols_output():
    """List the id_bot2 protocol ids, grouped by kind. Part C: the old loader is
    gone; the new pipeline owns the protocol library under id_bot2/protocols/."""
    try:
        from id_bot2 import channel as _idch
        router = _idch.get_router()
    except Exception as exc:  # never crash the command
        return f"Protocols: unavailable ({type(exc).__name__}: {exc})"

    groups = [
        ("Drug dosing", getattr(router, "registry", {})),
        ("Pathways", getattr(router, "pathways", {})),
        ("PCR panels", getattr(router, "panels", {})),
        ("Table lookups", getattr(router, "tables", {})),
        ("Calculators", getattr(router, "calcs", {})),
        ("Prose", getattr(router, "prose", {})),
    ]
    total = sum(len(g) for _, g in groups)
    lines = [f"id_bot2 protocols ({total}):"]
    for label, reg in groups:
        ids = sorted(reg.keys())
        if not ids:
            continue
        lines.append(f"\n{label} ({len(ids)}):")
        for pid in ids:
            lines.append(f"  - {pid}")
    return "\n".join(lines)


def format_version_output():
    return (
        f"Bot version: {BOT_VERSION}\n"
        f"Build: {get_build_id()}\n"
        f"Protocol library version: {get_protocol_library_version()}"
    )


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

def _find_telegram_italic_spans(text):
    spans = []
    safety_footer = (SAFETY_FOOTER or "").strip()
    if safety_footer and safety_footer.lower() != "(none)":
        start = 0
        while True:
            idx = text.find(safety_footer, start)
            if idx == -1:
                break
            end = idx + len(safety_footer)
            before_ok = idx == 0 or text[idx - 2:idx] == "\n\n"
            after_ok = end == len(text) or text[end:end + 2] == "\n\n"
            if before_ok and after_ok:
                spans.append((idx, end))
            start = end

    source_match = re.search(r"(?:^|\n\n)Source: [^\n\r]*\s*$", text)
    if source_match:
        source_start = source_match.start()
        if text.startswith("\n\n", source_start):
            source_start += 2
        spans.append((source_start, len(text)))

    spans.sort()
    merged = []
    for start, end in spans:
        if merged and start < merged[-1][1]:
            continue
        merged.append((start, end))
    return merged


def _telegram_html_text(text):
    spans = _find_telegram_italic_spans(text)
    if not spans:
        return None

    parts = []
    cursor = 0
    for start, end in spans:
        parts.append(html.escape(text[cursor:start], quote=False))
        parts.append(f"<i>{html.escape(text[start:end], quote=False)}</i>")
        cursor = end
    parts.append(html.escape(text[cursor:], quote=False))
    return "".join(parts)


async def _safe_reply_text(update, text):
    try:
        html_text = _telegram_html_text(text)
        if html_text is None:
            await update.message.reply_text(text)
        else:
            await update.message.reply_text(html_text, parse_mode="HTML")
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
    # Part C: the old pipeline's hot-reload is gone. id_bot2 loads its own
    # protocol library at startup (and caches the Router); a reload now means a
    # redeploy. Kept as a no-op so the command still responds politely.
    await _safe_reply_text(update,
        "Reload is no longer needed: the id_bot2 pipeline loads its protocol "
        "library at startup. To pick up protocol changes, redeploy the bot."
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

    answer = _format_id_bot2_debug_trace(debug_question)

    await _safe_reply_chunks(update, answer)


def _format_id_bot2_debug_trace(question):
    """Render a readable trace of the id_bot2 RouterResult for /debug. Stateless
    (no chat memory) so the trace reflects exactly how the router sees the text."""
    try:
        from id_bot2 import channel as _idch
        res = _idch.route(question)
    except Exception as exc:
        return f"DEBUG - id_bot2 routing trace\nERROR: {type(exc).__name__}: {exc}"

    lines = [
        "DEBUG - id_bot2 RouterResult",
        f"question: {question}",
        f"route: {res.route}",
        f"tool: {res.tool}",
        f"protocol: {res.protocol}",
        f"via: {res.via}",
        f"needs_clarification: {res.needs_clarification}",
        f"slots: {dict(res.slots or {})}",
    ]
    if res.candidates:
        lines.append(f"candidates: {list(res.candidates)}")
    if res.organisms:
        lines.append(f"organisms: {list(res.organisms)}")
    if res.markers:
        lines.append(f"markers: {list(res.markers)}")
    lines.append(f"phrased: {res.phrased} / phrasing_blocked: {res.phrasing_blocked}")
    lines.append("---")
    lines.append("answer:")
    lines.append(res.answer or "(none)")
    return "\n".join(lines)


ROTAHELY_USAGE = "Usage: /rotahely [date] <role>\nExample: /rotahely \u00c9g\u00e9s or /rotahely tomorrow \u00c9g\u00e9s"
NAPIROTA_USAGE = "Usage: /napirota [date]\nExample: /napirota or /napirota tomorrow"
HOSSZU_USAGE = "Usage: /hosszu [date]\nExample: /hosszu or /hosszu tomorrow"
UGYELET_USAGE = "Usage: /ugyelet [date]\nExample: /ugyelet or /ugyelet tomorrow"
HOLVAGYOK_USAGE = "Usage: /holvagyok [date] <name>\nExample: /holvagyok IVE or /holvagyok tomorrow IVE"
APOLO_USAGE = "Usage: /apolo [ma|holnap]\nExample: /apolo, /apolo ma, or /apolo holnap"
ROTA_UNAVAILABLE_MESSAGE = "Rota lookup is unavailable. Check Google Sheets configuration."
ROTA_COMMANDS_TEXT = (
    "Rota commands:\n"
    "/napirota [date]\n"
    "/hosszu [date]\n"
    "/ugyelet [date]\n"
    "/rotahely [date] <role>\n"
    "/holvagyok [date] <name>\n"
    "/apolo [ma|holnap]\n\n"
    "Doctor rota dates: today, tomorrow, ma, holnap, 2026-06-05, 2026.06.05\n"
    "No date = today\n"
    "Apolo: no argument/ma/today = today, holnap/tomorrow = tomorrow"
)
COMMANDS_TEXT = (
    "Commands:\n"
    "/commands, /help, /segits - show this help\n"
    "/start, /startup - show the intro message\n"
    "/whoami - show your Telegram user id\n"
    "/protocols - list loaded protocols\n"
    "/version - show bot, build, and protocol library version\n"
    "/reset, /clear - clear conversation history\n"
    "/reload - admin-only reload placeholder\n"
    "/debug <query> - show routing/debug trace\n\n"
    "Conversation controls:\n"
    "new patient, new case, reset, clear - clear the active patient/protocol flow\n\n"
    f"{ROTA_COMMANDS_TEXT}"
)


def _is_rota_allowed(user_id: int) -> bool:
    return bool(ALLOWED_USER_IDS) and user_id in ALLOWED_USER_IDS


def _format_rota_assignment(assignment, *, bullets=True):
    lines = [line.strip() for line in str(assignment or "").splitlines() if line.strip()]
    if bullets:
        return "\n".join(f"- {line}" for line in lines)
    return "\n".join(lines)


def _format_rota_result(result, date_value, role_label, not_found_noun="rota entry", *, bullets=True):
    separator = "\u2014"
    if result.status == STATUS_FOUND:
        source = result.source or "rota"
        return (
            f"{date_value.isoformat()} {separator} {result.role or role_label}\n"
            f"{_format_rota_assignment(result.assignment, bullets=bullets)}\n\n"
            f"Source: {source}"
        )
    if result.status == STATUS_MULTIPLE_MATCHES:
        return (
            f"Multiple rota entries found for {date_value.isoformat()} {separator} {role_label}. "
            "Please check the rota sheet."
        )
    if result.status in {STATUS_CONFIGURATION_ERROR, STATUS_PERMISSION_ERROR, STATUS_SHEET_ERROR}:
        return ROTA_UNAVAILABLE_MESSAGE
    if result.status == STATUS_NOT_FOUND:
        return f"No {not_found_noun} found for {date_value.isoformat()} {separator} {role_label}."
    return ROTA_UNAVAILABLE_MESSAGE


def _format_nursing_shift(shift):
    declared = shift.declared_count or "n/a"
    header = f"{shift.label}: {declared} ({shift.listed_count} listed)"
    lines = [header]
    lines.extend(f"- {name}" for name in shift.nurses)
    return "\n".join(lines)


def _format_nursing_rota_result(result, day):
    if result.status == STATUS_FOUND:
        date_label = result.date_value.isoformat() if result.date_value else day
        sections = []
        if result.day:
            sections.append(_format_nursing_shift(result.day))
        if result.night:
            sections.append(_format_nursing_shift(result.night))
        source = result.source or "nursing rota"
        return f"{date_label} - \u00c1pol\u00f3i beoszt\u00e1s\n" + "\n\n".join(sections) + f"\n\nSource: {source}"
    if result.status in {STATUS_CONFIGURATION_ERROR, STATUS_PERMISSION_ERROR, STATUS_SHEET_ERROR}:
        return ROTA_UNAVAILABLE_MESSAGE
    date_label = result.date_value.isoformat() if result.date_value else day
    return f"No nursing rota found for {date_label}."


async def handle_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _safe_reply_text(update, COMMANDS_TEXT)


async def _handle_rota_role_command(update, context, usage, lookup_func, not_found_noun="rota entry", *, bullets=True):
    user_id = _effective_user_id(update)
    if not _is_rota_allowed(user_id):
        await _reply_unauthorized(update)
        return

    args = list(getattr(context, "args", None) or [])
    if not args:
        await _safe_reply_text(update, usage)
        return

    try:
        date_value = parse_rota_date_input(args[0])
        role_args = args[1:]
    except ValueError:
        date_value = parse_rota_date_input("today")
        role_args = args

    role_text = " ".join(role_args).strip()
    if not role_text:
        await _safe_reply_text(update, usage)
        return

    role_label = normalize_rota_role(role_text)
    await _safe_send_action(update, "typing")
    result = lookup_func(date_value, role_text)
    await _safe_reply_text(
        update,
        _format_rota_result(result, date_value, role_label, not_found_noun, bullets=bullets),
    )


async def handle_rotahely(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_rota_role_command(
        update,
        context,
        ROTAHELY_USAGE,
        lookup_role_locations,
        "rota assignment",
    )


async def _handle_rota_date_command(update, context, usage, lookup_func, title, *, bullets):
    user_id = _effective_user_id(update)
    if not _is_rota_allowed(user_id):
        await _reply_unauthorized(update)
        return

    args = list(getattr(context, "args", None) or [])
    if len(args) > 1:
        await _safe_reply_text(update, usage)
        return

    try:
        date_value = parse_rota_date_input(args[0] if args else "today")
    except ValueError:
        await _safe_reply_text(update, usage)
        return

    await _safe_send_action(update, "typing")
    result = lookup_func(date_value)
    await _safe_reply_text(
        update,
        _format_rota_result(result, date_value, title, "rota assignment", bullets=bullets),
    )


async def handle_napirota(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_rota_date_command(
        update,
        context,
        NAPIROTA_USAGE,
        lookup_daily_summary,
        "Napi rota",
        bullets=True,
    )


async def handle_hosszu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_rota_date_command(
        update,
        context,
        HOSSZU_USAGE,
        lookup_long_summary,
        "Hossz\u00fa",
        bullets=False,
    )


async def handle_ugyelet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_rota_date_command(
        update,
        context,
        UGYELET_USAGE,
        lookup_oncall_summary,
        "\u00dcgyelet",
        bullets=False,
    )


async def handle_holvagyok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _effective_user_id(update)
    if not _is_rota_allowed(user_id):
        await _reply_unauthorized(update)
        return

    args = list(getattr(context, "args", None) or [])
    if not args:
        await _safe_reply_text(update, HOLVAGYOK_USAGE)
        return

    try:
        date_value = parse_rota_date_input(args[0])
        person_args = args[1:]
    except ValueError:
        date_value = parse_rota_date_input("today")
        person_args = args

    person_text = " ".join(person_args).strip()
    if not person_text:
        await _safe_reply_text(update, HOLVAGYOK_USAGE)
        return

    await _safe_send_action(update, "typing")
    result = lookup_daily_person(date_value, person_text)
    await _safe_reply_text(
        update,
        _format_rota_result(result, date_value, person_text, "daily assignment"),
    )


async def handle_apolo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = _effective_user_id(update)
    if not _is_rota_allowed(user_id):
        await _reply_unauthorized(update)
        return

    args = list(getattr(context, "args", None) or [])
    if len(args) > 1:
        await _safe_reply_text(update, APOLO_USAGE)
        return

    day = "today"
    if args:
        key = args[0].strip().casefold()
        if key in {"ma", "today"}:
            day = "today"
        elif key in {"holnap", "tomorrow"}:
            day = "tomorrow"
        else:
            await _safe_reply_text(update, APOLO_USAGE)
            return

    await _safe_send_action(update, "typing")
    result = lookup_nursing_rota(day)
    await _safe_reply_text(update, _format_nursing_rota_result(result, day))


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
    print(f"[startup] Access mode: {ACCESS_MODE}")
    print(f"[startup] Debug logging: {_format_debug_logging_summary()}")
    if ALLOWED_USER_IDS:
        print(f"[startup] Allowlist active: {len(ALLOWED_USER_IDS)} user(s) authorised.")
    if ADMIN_USER_IDS:
        print(f"[startup] Admin commands enabled for {len(ADMIN_USER_IDS)} user(s).")
    _query_log = setup_logging()
    logging.info("Bot starting up")

    # Part C: the old clinical pipeline (rule files / aliases / protocol parse +
    # embeddings) is decommissioned. The id_bot2 pipeline loads its own protocol
    # library lazily on first use (cached Router). Nothing to load here.

    print(
        f"Bot version: {BOT_VERSION}; build: {get_build_id()}; "
        f"protocol library version: {get_protocol_library_version()}"
    )
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
    app.add_handler(CommandHandler("commands", handle_commands))
    app.add_handler(CommandHandler("help", handle_commands))
    app.add_handler(CommandHandler("segits", handle_commands))
    app.add_handler(CommandHandler("rotahely", handle_rotahely))
    app.add_handler(CommandHandler("napirota", handle_napirota))
    app.add_handler(CommandHandler("hosszu", handle_hosszu))
    app.add_handler(CommandHandler("ugyelet", handle_ugyelet))
    app.add_handler(CommandHandler("holvagyok", handle_holvagyok))
    app.add_handler(CommandHandler("apolo", handle_apolo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot is running.")
    print(
        "Commands: /whoami, /protocols, /version, /reset, /clear, /debug <query>, "
        "/commands, /help, /segits, /rotahely [date] <role>, /napirota [date], "
        "/hosszu [date], /ugyelet [date], /holvagyok [date] <name>, "
        "/apolo [ma|holnap]."
    )
    app.run_polling()


if __name__ == "__main__":
    main()
