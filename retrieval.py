"""
Protocol loading, embedding cache, and semantic retrieval.

The public helpers can run with module-level state, while ``bot_core`` passes
its legacy globals in explicitly to preserve monkeypatch compatibility.
"""

import glob
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import numpy as np

from config import CACHE_DB as DEFAULT_CACHE_DB
from config import EMBEDDING_MODEL as DEFAULT_EMBEDDING_MODEL
from protocol_parser import _parse_protocol_text, extract_policy_header


PROTOCOL_CHUNKS = []
PROTOCOL_POLICY_BY_FILE = {}
PROTOCOL_PARSED_BY_FILE = {}
PROTOCOL_FILE_TO_LABEL = {}
_CACHE_DISABLED = False

EXCLUDED_FROM_PROTOCOLS = {
    "system_rules.txt",
    "answer_format_rules.txt",
    "answer_style_rules.txt",
    "safety_rules.txt",
    "aliases.json",
}


def normalize_path(path):
    return str(Path(path)).replace("\\", "/").lower()


def derive_source_label(file_path):
    stem = Path(file_path).stem.lower()
    fallback_labels = {
        "tmpsmx": "TMP/SMX",
        "tmp_smx": "TMP/SMX",
        "ampsul": "ampicillin/sulbactam",
        "ampicillin_sulbactam": "ampicillin/sulbactam",
        "amp_sul": "ampicillin/sulbactam",
        "meropenem": "meropenem",
        "cap": "CAP",
        "biofire": "BioFire",
        "pneumonia_pcr": "BioFire",
        "general_rules_antibiotic_dosing": "General antibiotic dosing rules",
    }
    return fallback_labels.get(stem, stem.replace("_", " ").title())


def extract_source_label_from_text(text):
    import re

    for line in text.splitlines()[:20]:
        match = re.match(r"^\s*source_label\s*:\s*(.+?)\s*$", line, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def get_source_label_for_file(file_path, text, protocol_file_to_label=None):
    metadata_label = extract_source_label_from_text(text)
    if metadata_label:
        return metadata_label
    labels = PROTOCOL_FILE_TO_LABEL if protocol_file_to_label is None else protocol_file_to_label
    normalized = normalize_path(file_path)
    if normalized in labels:
        return labels[normalized]
    return derive_source_label(file_path)


def _compute_file_hash(file_path):
    h = hashlib.md5()
    with open(file_path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def init_cache_db(cache_db=DEFAULT_CACHE_DB, cache_disabled=False):
    if cache_disabled:
        return False, True
    try:
        cache_parent = os.path.dirname(os.path.abspath(cache_db))
        if cache_parent:
            os.makedirs(cache_parent, exist_ok=True)
        with sqlite3.connect(cache_db) as con:
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
        return True, False
    except (OSError, sqlite3.Error) as exc:
        print(f"[startup] WARNING: embedding cache disabled ({cache_db}): {exc}")
        return False, True


def _init_cache_db():
    global _CACHE_DISABLED
    ok, _CACHE_DISABLED = init_cache_db(DEFAULT_CACHE_DB, _CACHE_DISABLED)
    return ok


def load_from_cache(file_hash, cache_db=DEFAULT_CACHE_DB, cache_disabled=False):
    if cache_disabled:
        return None
    try:
        with sqlite3.connect(cache_db) as con:
            row = con.execute(
                "SELECT chunks_json FROM embedding_cache WHERE file_hash = ?",
                (file_hash,),
            ).fetchone()
    except (json.JSONDecodeError, sqlite3.Error, OSError) as exc:
        print(f"[cache] WARNING: cache read skipped: {exc}")
        return None
    if row is None:
        return None
    try:
        raw = json.loads(row[0])
        for chunk in raw:
            chunk["embedding"] = np.array(chunk["embedding"])
        return raw
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        print(f"[cache] WARNING: corrupt cache entry ignored: {exc}")
        return None


def _load_from_cache(file_hash):
    return load_from_cache(file_hash, DEFAULT_CACHE_DB, _CACHE_DISABLED)


def save_to_cache(file_hash, chunks, cache_db=DEFAULT_CACHE_DB, cache_disabled=False):
    if cache_disabled or not chunks:
        return
    serialisable = []
    for chunk in chunks:
        serialisable.append({
            "source": chunk["source"],
            "source_label": chunk["source_label"],
            "text": chunk["text"],
            "embedding": chunk["embedding"].tolist(),
        })
    try:
        with sqlite3.connect(cache_db) as con:
            con.execute(
                """INSERT OR REPLACE INTO embedding_cache
                   (file_hash, source, source_label, chunks_json)
                   VALUES (?, ?, ?, ?)""",
                (
                    file_hash,
                    chunks[0]["source"],
                    chunks[0]["source_label"],
                    json.dumps(serialisable),
                ),
            )
            con.commit()
    except (sqlite3.Error, OSError) as exc:
        print(f"[cache] WARNING: cache write skipped: {exc}")


def _save_to_cache(file_hash, chunks):
    return save_to_cache(file_hash, chunks, DEFAULT_CACHE_DB, _CACHE_DISABLED)


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


def get_embedding(text, embedding_client, embedding_model=DEFAULT_EMBEDDING_MODEL):
    response = embedding_client.embeddings.create(model=embedding_model, input=text)
    return np.array(response.data[0].embedding)


def load_protocols(
    *,
    protocol_chunks=None,
    policy_by_file=None,
    parsed_by_file=None,
    protocol_file_to_label=None,
    cache_db=DEFAULT_CACHE_DB,
    cache_disabled=False,
    embedding_client=None,
    embedding_model=DEFAULT_EMBEDDING_MODEL,
):
    chunks_store = PROTOCOL_CHUNKS if protocol_chunks is None else protocol_chunks
    policy_store = PROTOCOL_POLICY_BY_FILE if policy_by_file is None else policy_by_file
    parsed_store = PROTOCOL_PARSED_BY_FILE if parsed_by_file is None else parsed_by_file
    label_store = PROTOCOL_FILE_TO_LABEL if protocol_file_to_label is None else protocol_file_to_label

    _, cache_disabled = init_cache_db(cache_db, cache_disabled)
    raw = glob.glob("protocols/*.txt") + glob.glob("protocols/**/*.txt", recursive=True)
    files = list({os.path.abspath(f): f for f in raw}.values())

    loaded = cached = fresh = 0
    for file_path in sorted(files):
        if Path(file_path).name in EXCLUDED_FROM_PROTOCOLS:
            print(f"Skipping excluded file: {file_path}")
            continue

        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()

        policy_header = extract_policy_header(text)
        if policy_header:
            policy_store[normalize_path(file_path)] = policy_header

        parsed = _parse_protocol_text(text, path=file_path)
        parsed_store[normalize_path(file_path)] = parsed
        for warning in parsed.get("warnings", []):
            print(f"[startup] WARNING: {warning}")

        file_hash = _compute_file_hash(file_path)
        cached_chunks = load_from_cache(file_hash, cache_db, cache_disabled)

        if cached_chunks is not None:
            chunks_store.extend(cached_chunks)
            print(f"  [cache] {file_path} ({len(cached_chunks)} chunks)")
            cached += 1
        else:
            if embedding_client is None:
                raise RuntimeError("embedding_client is required when protocol chunks are not cached")
            source_label = get_source_label_for_file(file_path, text, label_store)
            chunks = chunk_text(text=text, source=file_path, source_label=source_label)
            for chunk in chunks:
                chunk["embedding"] = get_embedding(chunk["text"], embedding_client, embedding_model)
            save_to_cache(file_hash, chunks, cache_db, cache_disabled)
            chunks_store.extend(chunks)
            print(f"  [fresh] {file_path} ({len(chunks)} chunks, embeddings computed)")
            fresh += 1

        loaded += 1

    print(f"Total: {len(chunks_store)} chunks from {loaded} files "
          f"({cached} from cache, {fresh} freshly embedded)")
    return cache_disabled


def search_protocols(
    question,
    top_k=3,
    preferred_file=None,
    guaranteed_slots=2,
    *,
    protocol_chunks=None,
    embedding_client=None,
    embedding_model=DEFAULT_EMBEDDING_MODEL,
):
    chunks_store = PROTOCOL_CHUNKS if protocol_chunks is None else protocol_chunks
    if embedding_client is None:
        raise RuntimeError("embedding_client is required for semantic search")
    question_embedding = get_embedding(question, embedding_client, embedding_model)
    preferred_file_norm = normalize_path(preferred_file) if preferred_file else None

    preferred_chunks = []
    other_chunks = []
    for chunk in chunks_store:
        similarity = float(np.dot(question_embedding, chunk["embedding"]))
        entry = {
            "source": chunk["source"],
            "source_label": chunk["source_label"],
            "text": chunk["text"],
            "similarity": similarity,
        }
        if preferred_file_norm and normalize_path(chunk["source"]) == preferred_file_norm:
            preferred_chunks.append(entry)
        else:
            other_chunks.append(entry)

    preferred_chunks.sort(key=lambda x: x["similarity"], reverse=True)
    other_chunks.sort(key=lambda x: x["similarity"], reverse=True)

    if preferred_file_norm and preferred_chunks:
        slots_for_preferred = min(guaranteed_slots, len(preferred_chunks), top_k)
        slots_for_others = top_k - slots_for_preferred
        return preferred_chunks[:slots_for_preferred] + other_chunks[:slots_for_others]

    all_chunks = preferred_chunks + other_chunks
    all_chunks.sort(key=lambda x: x["similarity"], reverse=True)
    return all_chunks[:top_k]


__all__ = [
    "PROTOCOL_CHUNKS",
    "PROTOCOL_POLICY_BY_FILE",
    "PROTOCOL_PARSED_BY_FILE",
    "PROTOCOL_FILE_TO_LABEL",
    "EXCLUDED_FROM_PROTOCOLS",
    "normalize_path",
    "derive_source_label",
    "extract_source_label_from_text",
    "get_source_label_for_file",
    "_compute_file_hash",
    "init_cache_db",
    "_init_cache_db",
    "load_from_cache",
    "_load_from_cache",
    "save_to_cache",
    "_save_to_cache",
    "chunk_text",
    "get_embedding",
    "load_protocols",
    "search_protocols",
]
