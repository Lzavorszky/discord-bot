"""
config.py — Bot configuration and editable-text loading.

All environment variables, model names, and user-facing text strings
live here. Import from this module rather than reading os.getenv()
scattered across the codebase.
"""

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
# Misc
# ---------------------------------------------------------------------------

MAX_HISTORY_TURNS = 10
CACHE_DB          = "embeddings_cache.db"
LOG_FILE          = "bot_queries.log"
