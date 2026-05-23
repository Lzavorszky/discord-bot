import os
import glob
import json
import re
from pathlib import Path

import discord
import numpy as np
from openai import OpenAI

try:
    from rapidfuzz import process, fuzz
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

openai_client = OpenAI(api_key=OPENAI_API_KEY)

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

EMBEDDING_MODEL = "text-embedding-3-small"
CHAT_MODEL = "gpt-4o-mini"

PROTOCOL_CHUNKS = []

ALIASES = {}
ALIAS_INDEX = {}
PROTOCOL_FILE_TO_LABEL = {}

SYSTEM_RULES = ""
ANSWER_FORMAT_RULES = ""
ANSWER_STYLE_RULES = ""
SAFETY_RULES = ""

# Per-channel state:
#   {channel_id: {"history": [...], "active_recognized": {...} or None}}
#
# "active_recognized" carries the last successfully identified drug/condition
# across the whole conversation so that follow-up messages like
# "Steno BSI, 60 kg, GFR 60" still boost the correct protocol file.
CONVERSATION_STATE = {}
MAX_HISTORY_TURNS = 10


# -----------------------------
# Basic helpers
# -----------------------------

def load_text_file(path):
    if not os.path.exists(path):
        print(f"Rule file not found: {path}")
        return ""
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def load_rule_files():
    global SYSTEM_RULES, ANSWER_FORMAT_RULES, ANSWER_STYLE_RULES, SAFETY_RULES
    SYSTEM_RULES       = load_text_file("system_rules.txt")
    ANSWER_FORMAT_RULES = load_text_file("answer_format_rules.txt")
    ANSWER_STYLE_RULES  = load_text_file("answer_style_rules.txt")
    SAFETY_RULES        = load_text_file("safety_rules.txt")
    print("Loaded rule files")


def normalize_path(path):
    return str(Path(path)).replace("\\", "/").lower()


def derive_source_label(file_path):
    stem = Path(file_path).stem.lower()
    fallback_labels = {
        "tmpsmx":             "TMP/SMX",
        "tmp_smx":            "TMP/SMX",
        "ampicillin_sulbactam": "ampicillin/sulbactam",
        "amp_sul":            "ampicillin/sulbactam",
        "meropenem":          "meropenem",
        "cap":                "CAP",
        "biofire":            "BioFire",
        "pneumonia_pcr":      "BioFire",
    }
    return fallback_labels.get(stem, stem.replace("_", " "))


def extract_source_label_from_text(text):
    for line in text.splitlines()[:20]:
        match = re.match(r"^\s*source_label\s*:\s*(.+?)\s*$", line, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


# -----------------------------
# Alias recognition
# -----------------------------

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
    ALIAS_INDEX, PROTOCOL_FILE_TO_LABEL = build_alias_index(ALIASES)
    print(f"Loaded {len(ALIAS_INDEX)} aliases")
    if not RAPIDFUZZ_AVAILABLE:
        print("rapidfuzz not installed. Exact alias matching works; fuzzy matching disabled.")


def build_alias_index(alias_data):
    alias_index = {}
    protocol_file_to_label = {}
    for category in ["drugs", "conditions"]:
        for key, item in alias_data.get(category, {}).items():
            display      = item.get("display", key)
            canonical    = item.get("canonical", display)
            source_label = item.get("source_label", display)
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
    Only inspects the current message — caller is responsible for falling
    back to active_recognized when this returns None.
    """
    text = question.lower().strip()
    if not ALIAS_INDEX:
        return question, None

    # Exact match — longest aliases first to avoid early short-alias collisions
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


# -----------------------------
# Protocol loading and retrieval
# -----------------------------

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
    files = glob.glob("protocols/**/*.txt", recursive=True)
    for file_path in files:
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
        source_label = get_source_label_for_file(file_path, text)
        chunks = chunk_text(text=text, source=file_path, source_label=source_label)
        for chunk in chunks:
            chunk["embedding"] = get_embedding(chunk["text"])
            PROTOCOL_CHUNKS.append(chunk)
    print(f"Loaded {len(PROTOCOL_CHUNKS)} protocol chunks")


def search_protocols(question, top_k=3, preferred_file=None, guaranteed_slots=2):
    """
    When preferred_file is set, guarantee at least `guaranteed_slots` of the
    returned chunks come from that file — regardless of how other files score.
    This prevents a semantically strong but wrong protocol from crowding out
    the actively selected one during a multi-turn dosing conversation.
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
        # Always give the active protocol file its guaranteed slots first,
        # then fill the remainder with the best chunks from other files.
        slots_for_preferred = min(guaranteed_slots, len(preferred_chunks), top_k)
        slots_for_others = top_k - slots_for_preferred
        return preferred_chunks[:slots_for_preferred] + other_chunks[:slots_for_others]

    # No preferred file — pure semantic search across everything
    all_chunks = preferred_chunks + other_chunks
    all_chunks.sort(key=lambda x: x["similarity"], reverse=True)
    return all_chunks[:top_k]


def format_debug_output(retrieved_chunks):
    debug_text = "DEBUG — retrieved protocol chunks:\n\n"
    for i, chunk in enumerate(retrieved_chunks, start=1):
        preview = chunk["text"][:600].replace("\n", " ")
        debug_text += (
            f"{i}. Source label: {chunk['source_label']}\n"
            f"   Source file:  {chunk['source']}\n"
            f"   Similarity:   {chunk['similarity']:.4f}\n"
            f"   Preview: {preview}...\n\n"
        )
    return debug_text


# -----------------------------
# AI answer generation
# -----------------------------

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
        build_recognition_context(recognized),
        f"PROTOCOL EXCERPTS:\n{context}",
    ]))


def get_channel_state(channel_id):
    if channel_id not in CONVERSATION_STATE:
        CONVERSATION_STATE[channel_id] = {"history": [], "active_recognized": None}
    return CONVERSATION_STATE[channel_id]


def ask_ai(question, channel_id):
    state = get_channel_state(channel_id)

    normalized_question, recognized = normalize_question(question)

    if recognized:
        # A new drug/condition was explicitly mentioned — update the active context.
        state["active_recognized"] = recognized
    else:
        # No alias found in this message.
        # Reuse the active context from the current conversation so that
        # follow-up messages ("Steno BSI, 60 kg, GFR 60") still boost the
        # correct protocol file instead of drifting to a different drug.
        recognized = state["active_recognized"]
        if recognized:
            # Append the active-drug hint so the model also knows what we mean.
            normalized_question = (
                question
                + f"\n\n[Continuing context: {recognized['display']} / {recognized['canonical']}]"
            )

    preferred_file = recognized.get("protocol_file") if recognized else None

    retrieved_chunks = search_protocols(
        normalized_question, top_k=3, preferred_file=preferred_file
    )

    # Expose only the human-readable source_label to the model, never the file path.
    # This prevents the model from echoing "protocols/medical/antibiotics/tmpsmx.txt"
    # in its replies despite the answer_format_rules telling it not to.
    context = "\n\n---\n\n".join(
        f"Source: {c['source_label']}\n{c['text']}"
        for c in retrieved_chunks
    )

    system_prompt = build_system_prompt(recognized, context)

    history = state["history"]
    messages = history + [{"role": "user", "content": question}]

    response = openai_client.chat.completions.create(
        model=CHAT_MODEL,
        messages=[{"role": "system", "content": system_prompt}] + messages,
    )

    answer = response.choices[0].message.content

    # Update history, trim to MAX_HISTORY_TURNS pairs
    history = history + [
        {"role": "user",      "content": question},
        {"role": "assistant", "content": answer},
    ]
    max_messages = MAX_HISTORY_TURNS * 2
    if len(history) > max_messages:
        history = history[-max_messages:]

    state["history"] = history
    return answer


# -----------------------------
# Discord helpers / events
# -----------------------------

def split_message(text, max_length=1900):
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


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    load_rule_files()
    if not ALIAS_INDEX:
        load_aliases()
    if not PROTOCOL_CHUNKS:
        load_protocols()


@client.event
async def on_message(message):
    if message.author == client.user:
        return
    question = message.content.strip()
    if not question:
        return

    channel_id = message.channel.id

    async with message.channel.typing():

        if question.lower() in ("/reset", "/clear"):
            CONVERSATION_STATE.pop(channel_id, None)
            await message.channel.send("Conversation history cleared.")
            return

        if question.lower().startswith("/debug"):
            debug_question = question.replace("/debug", "", 1).strip()
            if not debug_question:
                answer = "Please provide a question after /debug."
            else:
                normalized_question, recognized = normalize_question(debug_question)
                # Also show active_recognized from state if nothing matched
                state = get_channel_state(channel_id)
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
                        f"Matched alias:    {recognized.get('matched_alias', '(carried from prior turn)')}\n"
                        f"Normalized to:    {recognized['display']}\n"
                        f"Source label:     {recognized['source_label']}\n"
                        f"Confidence:       {recognized['confidence']}\n\n"
                    )
                answer += format_debug_output(retrieved_chunks)
        else:
            answer = ask_ai(question, channel_id)

    for chunk in split_message(answer):
        await message.channel.send(chunk)


client.run(DISCORD_TOKEN)
