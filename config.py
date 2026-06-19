"""
config.py — Bot configuration and editable-text loading.

All environment variables, model names, and user-facing text strings
live here. Import from this module rather than reading os.getenv()
scattered across the codebase.
"""

import json
import os


# ---------------------------------------------------------------------------
# Editable text files
# ---------------------------------------------------------------------------

def _load_text_file(filepath, fallback, label):
    """Load text from a file; warn and return fallback if missing."""
    try:
        with open(filepath, "r", encoding="utf-8") as _f:
            return _f.read().strip()
    except FileNotFoundError:
        print(f"[startup] WARNING: {label} file not found ({filepath}), using built-in fallback.")
        return fallback


SAFETY_FOOTER = _load_text_file(
    "safety_footer.txt",
    "  ⚠️ de ellenőrizd!",
    "Safety footer"
)

START_MESSAGE = _load_text_file(
    "intro_message.txt",
    (
        "Bár sokat tud, ez végső soron csak egy chatbot, aki néhány txt fájlt olvasgat.\n"
        "\n"
        "Első a józan ész.\n"
        "\n"
        "Parancsok:\n"
        "  /reset  — beszélgetési előzmények törlése\n"
        "  /debug <kérdés>  — mutatja, melyik protokoll részletek töltődtek be"
    ),
    "Intro message"
)


# ---------------------------------------------------------------------------
# Environment / API
# ---------------------------------------------------------------------------

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# ---------------------------------------------------------------------------
# Model names
# ---------------------------------------------------------------------------

EMBEDDING_MODEL = "text-embedding-3-small"
CHAT_MODEL      = "gpt-4o-mini"

# ---------------------------------------------------------------------------
# Plan D rebuild (id_bot2) — model wiring behind the not-yet-built LLMProvider.
# These are READ ONLY by id_bot2; the live bot above keeps using CHAT_MODEL.
# Decision (roadmap, 2026-06-16): GPT-5.5 router; mini/nano permitted for pure
# phrasing. Router can drop to GPT-5.4 later via the provider seam (config flip).
# Override any of these via environment variables without code changes.
# ---------------------------------------------------------------------------

ROUTER_MODEL    = os.getenv("ID_BOT2_ROUTER_MODEL",    "gpt-4o-mini")   # the single decision (tool-calling)
PHRASING_MODEL  = os.getenv("ID_BOT2_PHRASING_MODEL",  "gpt-4o-mini")  # phrase tool results (mini vs nano TBD, Phase 7)
VERIFIER_MODEL  = os.getenv("ID_BOT2_VERIFIER_MODEL",  "gpt-4o-mini")  # grounding checks (Phase 4)
ROUTER_PROVIDER = os.getenv("ID_BOT2_ROUTER_PROVIDER", "openai")    # provider key for the LLMProvider factory (Phase 1)

# Cutover flag (Phase 6): when true, bot_core.ask_ai routes user messages through
# the new id_bot2 pipeline (router -> tool -> phrase -> verify) instead of the old
# _ask_ai_impl. Default OFF — flipping this (env USE_ID_BOT2=1) IS the cutover; the
# old pipeline stays importable as instant rollback.
USE_ID_BOT2 = os.getenv("USE_ID_BOT2", "0").strip().lower() in ("1", "true", "yes", "on")

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------

MAX_HISTORY_TURNS = 10

_ON_RAILWAY = bool(os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"))
CACHE_DB = os.getenv(
    "CACHE_DB",
    "/tmp/id_bot_embeddings_cache.db" if _ON_RAILWAY else "embeddings_cache.db",
)
LOG_FILE = os.getenv("LOG_FILE", "bot_queries.log")


# ---------------------------------------------------------------------------
# Runtime options
# ---------------------------------------------------------------------------

DEFAULT_DEBUG_LOGGING_OPTIONS = {
    "log_user_messages": False,
    "log_bot_responses": True,
    "log_raw_llm_responses": True,
    "log_retrieved_chunks": True,
    "log_routing_trace": True,
    "log_prompt_preview": False,
    "log_admin_debug_notes": True,
    "stdout_full_turns": False,
}

DEFAULT_RUNTIME_OPTIONS = {
    "access_mode": "closed",
    "log_user_messages": False,
    "allowed_user_ids": [],
    "admin_user_ids": [],
    "debug_logging": DEFAULT_DEBUG_LOGGING_OPTIONS,
}


def _parse_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "open"}


def _parse_id_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw_items = value
    else:
        raw_items = str(value).split(",")
    ids = []
    for item in raw_items:
        text = str(item).strip()
        if not text:
            continue
        if text.isdigit():
            ids.append(int(text))
    return ids


def _load_runtime_options_file(path):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[startup] WARNING: runtime options file could not be read ({path}): {exc}")
        return {}
    if not isinstance(data, dict):
        print(f"[startup] WARNING: runtime options file must contain a JSON object ({path}).")
        return {}
    return data


def _normalise_debug_logging_options(value, legacy_log_user_messages=False):
    options = dict(DEFAULT_DEBUG_LOGGING_OPTIONS)
    if isinstance(value, dict):
        options.update(value)

    for key, default in DEFAULT_DEBUG_LOGGING_OPTIONS.items():
        options[key] = _parse_bool(options.get(key), default)

    if legacy_log_user_messages:
        options["log_user_messages"] = True
        options["log_prompt_preview"] = True
        options["stdout_full_turns"] = True

    return options


def get_runtime_options():
    """Load runtime options from runtime_options.json, then apply env overrides."""
    path = os.getenv("RUNTIME_OPTIONS_FILE", "runtime_options.json")
    file_options = _load_runtime_options_file(path)
    options = dict(DEFAULT_RUNTIME_OPTIONS)
    options["debug_logging"] = dict(DEFAULT_DEBUG_LOGGING_OPTIONS)
    options.update(file_options)

    env_access_mode = os.getenv("ACCESS_MODE") or os.getenv("BOT_ACCESS_MODE")
    options["_access_mode_explicit"] = "access_mode" in file_options or bool(env_access_mode)
    if env_access_mode:
        options["access_mode"] = env_access_mode

    if os.getenv("LOG_USER_MESSAGES") is not None:
        options["log_user_messages"] = _parse_bool(os.getenv("LOG_USER_MESSAGES"))

    if os.getenv("FULL_CONVERSATION_LOG") is not None:
        full_log = _parse_bool(os.getenv("FULL_CONVERSATION_LOG"))
        debug_logging = dict(options.get("debug_logging") or {})
        if full_log:
            debug_logging.update({
                "log_user_messages": True,
                "log_prompt_preview": True,
                "stdout_full_turns": True,
            })
        options["debug_logging"] = debug_logging

    if os.getenv("ALLOWED_USER_IDS") is not None:
        options["allowed_user_ids"] = _parse_id_list(os.getenv("ALLOWED_USER_IDS"))
    else:
        options["allowed_user_ids"] = _parse_id_list(options.get("allowed_user_ids"))

    if os.getenv("ADMIN_USER_IDS") is not None:
        options["admin_user_ids"] = _parse_id_list(os.getenv("ADMIN_USER_IDS"))
    else:
        options["admin_user_ids"] = _parse_id_list(options.get("admin_user_ids"))

    options["access_mode"] = str(options.get("access_mode") or "closed").strip().lower()
    options["log_user_messages"] = _parse_bool(options.get("log_user_messages"), False)
    options["debug_logging"] = _normalise_debug_logging_options(
        options.get("debug_logging"),
        legacy_log_user_messages=options["log_user_messages"],
    )
    return options
