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

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

PROTOCOL_CHUNKS        = []
ALIASES                = {}
ALIAS_INDEX            = {}
PROTOCOL_FILE_TO_LABEL = {}

SYSTEM_RULES        = ""
ANSWER_FORMAT_RULES = ""
ANSWER_STYLE_RULES  = ""
SAFETY_RULES        = ""

# Per-chat conversation state:
# { chat_id: {"history": [...], "active_recognized": {...} or None} }
CONVERSATION_STATE = {}


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
    Tries exact match first (longest alias first), then fuzzy if rapidfuzz available.
    """
    text = question.lower().strip()
    if not ALIAS_INDEX:
        return question, None

    # Exact match — longest first to avoid short-alias collisions
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

    # Fuzzy match
    if not RAPIDFUZZ_AVAILABLE:
        return question, None
    match = process.extractOne(text, list(ALIAS_INDEX.keys()), scorer=fuzz.WRatio)
    if not match:
        return question, None
    alias, score, _ = match
    if score >= 90:
        data = ALIAS_INDEX[alias]
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
        data = ALIAS_INDEX[alias]
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
    # Support both flat and nested protocol folder
    files = list(set(
        glob.glob("protocols/*.txt") +
        glob.glob("protocols/**/*.txt", recursive=True)
    ))

    loaded = 0
    for file_path in sorted(files):
        if Path(file_path).name in EXCLUDED_FROM_PROTOCOLS:
            print(f"Skipping excluded file: {file_path}")
            continue
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
        source_label = get_source_label_for_file(file_path, text)
        chunks = chunk_text(text=text, source=file_path, source_label=source_label)
        for chunk in chunks:
            chunk["embedding"] = get_embedding(chunk["text"])
            PROTOCOL_CHUNKS.append(chunk)
        loaded += 1
        print(f"  Loaded: {file_path} ({source_label})")

    print(f"Total: {len(PROTOCOL_CHUNKS)} chunks from {loaded} files")


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
        text = text + f'\n\nSource: {source_label}'   # 6. append correct source
    return text


# ---------------------------------------------------------------------------
# AI answer generation
# ---------------------------------------------------------------------------

SOURCE_INSTRUCTION = (
    "DO NOT write a Source line in your response. "
    "The source is appended automatically after your answer. "
    "Do not write 'Source:', 'Forrás:', 'Source file:', or any file path."
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
        build_recognition_context(recognized),
        f"PROTOCOL EXCERPTS:\n{context}",
    ]))


def get_chat_state(chat_id):
    if chat_id not in CONVERSATION_STATE:
        CONVERSATION_STATE[chat_id] = {"history": [], "active_recognized": None}
    return CONVERSATION_STATE[chat_id]


def ask_ai(question, chat_id):
    state = get_chat_state(chat_id)

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

async def handle_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    CONVERSATION_STATE.pop(chat_id, None)
    await update.message.reply_text("Conversation history cleared.")


async def handle_debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    chat_id = update.effective_chat.id
    await update.message.chat.send_action(action="typing")

    answer = ask_ai(question, chat_id)

    for chunk in split_message(answer):
        await update.message.reply_text(chunk)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    print("Loading rule files...")
    load_rule_files()

    print("Loading aliases...")
    load_aliases("protocols/aliases.json")

    print("Loading protocols and generating embeddings (this may take a moment)...")
    load_protocols()

    print("Starting Telegram bot...")
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("reset", handle_reset))
    app.add_handler(CommandHandler("clear", handle_reset))
    app.add_handler(CommandHandler("debug", handle_debug))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Bot is running.")
    print("Commands: /reset or /clear to clear history, /debug <query> to inspect retrieval.")
    app.run_polling()


if __name__ == "__main__":
    main()
