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
CHAT_MODEL = "gpt-5.4-mini"

PROTOCOL_CHUNKS = []

ALIASES = {}
ALIAS_INDEX = {}
PROTOCOL_FILE_TO_LABEL = {}

SYSTEM_RULES = ""
ANSWER_FORMAT_RULES = ""
ANSWER_STYLE_RULES = ""


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
    global SYSTEM_RULES, ANSWER_FORMAT_RULES, ANSWER_STYLE_RULES

    SYSTEM_RULES = load_text_file("system_rules.txt")
    ANSWER_FORMAT_RULES = load_text_file("answer_format_rules.txt")
    ANSWER_STYLE_RULES = load_text_file("answer_style_rules.txt")

    print("Loaded rule files")


def normalize_path(path):
    return str(Path(path)).replace("\\", "/").lower()


def derive_source_label(file_path):
    stem = Path(file_path).stem.lower()

    fallback_labels = {
        "tmpsmx": "TMP/SMX",
        "tmp_smx": "TMP/SMX",
        "ampicillin_sulbactam": "ampicillin/sulbactam",
        "amp_sul": "ampicillin/sulbactam",
        "meropenem": "meropenem",
        "cap": "CAP",
        "biofire": "BioFire",
        "pneumonia_pcr": "BioFire"
    }

    return fallback_labels.get(stem, stem.replace("_", " "))


def extract_source_label_from_text(text):
    """
    Optional metadata support.

    In a protocol txt file, you may add:
    source_label: CAP

    or:
    source_label: TMP/SMX
    """
    for line in text.splitlines()[:20]:
        match = re.match(
            r"^\s*source_label\s*:\s*(.+?)\s*$",
            line,
            flags=re.IGNORECASE
        )
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
            display = item.get("display", key)
            canonical = item.get("canonical", display)
            source_label = item.get("source_label", display)
            protocol_file = item.get("protocol_file", "")

            data = {
                "key": key,
                "category": category,
                "display": display,
                "canonical": canonical,
                "source_label": source_label,
                "protocol_file": protocol_file
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
    Returns:
    normalized_question, recognized_metadata_or_None
    """
    text = question.lower().strip()

    if not ALIAS_INDEX:
        return question, None

    # Exact alias match first.
    # Longer aliases first avoids "cap" matching inside longer phrases too early.
    for alias in sorted(ALIAS_INDEX.keys(), key=len, reverse=True):
        data = ALIAS_INDEX[alias]

        if len(alias) <= 4:
            pattern = r"\b" + re.escape(alias) + r"\b"
            matched = re.search(pattern, text) is not None
        else:
            matched = alias in text

        if matched:
            normalized_question = (
                question
                + f"\n\nRecognized term: {data['display']}"
                + f"\nCanonical term: {data['canonical']}"
            )

            return normalized_question, {
                **data,
                "matched_alias": alias,
                "confidence": "exact",
                "score": 100
            }

    # Fuzzy alias match.
    if not RAPIDFUZZ_AVAILABLE:
        return question, None

    match = process.extractOne(
        text,
        list(ALIAS_INDEX.keys()),
        scorer=fuzz.WRatio
    )

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
            **data,
            "matched_alias": alias,
            "confidence": "high",
            "score": score
        }

    if score >= 80:
        data = ALIAS_INDEX[alias]

        normalized_question = (
            question
            + f"\n\nPossible recognized term: {data['display']}"
            + f"\nCanonical term: {data['canonical']}"
        )

        return normalized_question, {
            **data,
            "matched_alias": alias,
            "confidence": "medium",
            "score": score
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
                chunks.append({
                    "source": source,
                    "source_label": source_label,
                    "text": current.strip()
                })
            current = section + "\n\n"

    if current.strip():
        chunks.append({
            "source": source,
            "source_label": source_label,
            "text": current.strip()
        })

    return chunks


def get_embedding(text):
    response = openai_client.embeddings.create(
        model=EMBEDDING_MODEL,
        input=text
    )
    return np.array(response.data[0].embedding)


def get_source_label_for_file(file_path, text):
    metadata_label = extract_source_label_from_text(text)
    if metadata_label:
        return metadata_label

    normalized_file_path = normalize_path(file_path)

    if normalized_file_path in PROTOCOL_FILE_TO_LABEL:
        return PROTOCOL_FILE_TO_LABEL[normalized_file_path]

    return derive_source_label(file_path)


def load_protocols():
    global PROTOCOL_CHUNKS

    files = glob.glob("protocols/**/*.txt", recursive=True)

    for file_path in files:
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()

        source_label = get_source_label_for_file(file_path, text)

        chunks = chunk_text(
            text=text,
            source=file_path,
            source_label=source_label
        )

        for chunk in chunks:
            chunk["embedding"] = get_embedding(chunk["text"])
            PROTOCOL_CHUNKS.append(chunk)

    print(f"Loaded {len(PROTOCOL_CHUNKS)} protocol chunks")


def search_protocols(question, top_k=3, preferred_file=None):
    question_embedding = get_embedding(question)

    preferred_file_norm = normalize_path(preferred_file) if preferred_file else None

    results = []

    for chunk in PROTOCOL_CHUNKS:
        similarity = float(np.dot(question_embedding, chunk["embedding"]))

        # Small boost if alias recognition points to a specific protocol file.
        if preferred_file_norm and normalize_path(chunk["source"]) == preferred_file_norm:
            similarity += 0.20

        results.append({
            "source": chunk["source"],
            "source_label": chunk["source_label"],
            "text": chunk["text"],
            "similarity": similarity
        })

    results.sort(key=lambda x: x["similarity"], reverse=True)

    return results[:top_k]


def format_debug_output(retrieved_chunks):
    debug_text = "DEBUG — retrieved protocol chunks:\n\n"

    for i, chunk in enumerate(retrieved_chunks, start=1):
        preview = chunk["text"][:600].replace("\n", " ")

        debug_text += (
            f"{i}. Source label: {chunk['source_label']}\n"
            f"   Source file: {chunk['source']}\n"
            f"   Similarity: {chunk['similarity']:.4f}\n"
            f"   Preview: {preview}...\n\n"
        )

    return debug_text


# -----------------------------
# AI answer generation
# -----------------------------

def build_recognition_context(recognized):
    """
    This is dynamic runtime context, not behavioural instruction.
    Behaviour remains in txt rule files.
    """
    if not recognized:
        return ""

    return f"""
RECOGNIZED QUERY TERM:
User term matched: {recognized["matched_alias"]}
Normalized to: {recognized["display"]}
Canonical name: {recognized["canonical"]}
Source label: {recognized["source_label"]}
Confidence: {recognized["confidence"]}
""".strip()


def ask_ai(question):
    normalized_question, recognized = normalize_question(question)

    preferred_file = recognized.get("protocol_file") if recognized else None

    retrieved_chunks = search_protocols(
        normalized_question,
        top_k=3,
        preferred_file=preferred_file
    )

    context = "\n\n---\n\n".join(
        [
            f"Source label: {c['source_label']}\n{c['text']}"
            for c in retrieved_chunks
        ]
    )

    recognition_context = build_recognition_context(recognized)

    system_content = f"""
{SYSTEM_RULES}

{ANSWER_FORMAT_RULES}

{ANSWER_STYLE_RULES}

{recognition_context}

PROTOCOL EXCERPTS:
{context}
""".strip()

    response = openai_client.responses.create(
        model=CHAT_MODEL,
        input=[
            {
                "role": "system",
                "content": system_content
            },
            {
                "role": "user",
                "content": question
            }
        ],
    )

    return response.output_text


# -----------------------------
# Discord helpers/events
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

    async with message.channel.typing():

        if question.lower().startswith("/debug"):
            debug_question = question.replace("/debug", "", 1).strip()

            if not debug_question:
                answer = "Please provide a question after /debug."
            else:
                normalized_question, recognized = normalize_question(debug_question)
                preferred_file = recognized.get("protocol_file") if recognized else None

                retrieved_chunks = search_protocols(
                    normalized_question,
                    top_k=5,
                    preferred_file=preferred_file
                )

                answer = ""

                if recognized:
                    answer += (
                        "DEBUG — recognized term:\n"
                        f"Matched alias: {recognized['matched_alias']}\n"
                        f"Normalized to: {recognized['display']}\n"
                        f"Source label: {recognized['source_label']}\n"
                        f"Confidence: {recognized['confidence']}\n\n"
                    )

                answer += format_debug_output(retrieved_chunks)

        else:
            answer = ask_ai(question)

    for chunk in split_message(answer):
        await message.channel.send(chunk)


client.run(DISCORD_TOKEN)