"""
Audit logging, safe prompt redaction, and stdout trace formatting.
"""

import datetime as _dt
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler


def setup_logging(log_file):
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    query_logger = logging.getLogger("query")
    query_logger.setLevel(logging.INFO)
    query_logger.propagate = False

    fh = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(message)s"))
    query_logger.addHandler(fh)

    logging.info(f"Logging initialised -> {log_file}")
    return query_logger


def _safe_user_message_for_log(user_message, full_conversation_log=False):
    if full_conversation_log:
        return user_message
    return {
        "redacted": True,
        "length": len(user_message or ""),
        "sha256_12": hashlib.sha256((user_message or "").encode("utf-8")).hexdigest()[:12],
    }


def _safe_prompt_preview_for_stdout(user_message, full_conversation_log=False):
    if full_conversation_log:
        text = (user_message or "").replace("\n", " ").strip()
        return text[:117] + "..." if len(text) > 120 else text
    return "<redacted>"


def _reconstructable_turn_for_stdout(entry, full_conversation_log=False):
    if not full_conversation_log:
        return None
    return {
        "event": "conversation_turn",
        "ts": entry["ts"],
        "chat_id_hash": entry["chat_id_hash"],
        "user_message": entry["user_message"],
        "assistant_message": entry["final"],
        "raw_llm": entry["raw_llm"],
        "recognized": entry["recognized"],
        "retrieved": entry["retrieved"],
        "trace": entry.get("trace"),
        "duration_ms": entry["duration_ms"],
    }


def _format_trace_for_stdout(trace):
    if not trace:
        return ""
    parts = []
    if trace.get("selected_protocol_id"):
        parts.append(f"protocol={trace['selected_protocol_id']}")
    if trace.get("selection_output_key"):
        parts.append(f"output={trace['selection_output_key']}")
    if trace.get("deterministic_or_llm"):
        parts.append(f"path={trace['deterministic_or_llm']}")
    parts.append(f"llm_called={bool(trace.get('llm_called'))}")
    if trace.get("unsupported_syndrome"):
        matched = trace.get("unsupported_matched_term") or trace["unsupported_syndrome"]
        parts.append(
            f"unsupported={trace['unsupported_syndrome']}:{matched}:{trace.get('unsupported_action', 'n/a')}"
        )
    if trace.get("blocked_reason"):
        parts.append(f"blocked={trace['blocked_reason']}")
    if trace.get("confirmation_required"):
        parts.append("confirmation_required=true")
    return " ".join(parts)


def _log_query(
    query_log,
    chat_id,
    user_message,
    recognized,
    retrieved_chunks,
    raw_llm,
    final_response,
    duration_ms,
    *,
    trace=None,
    full_conversation_log=False,
):
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    chat_hash = hashlib.md5(str(chat_id).encode()).hexdigest()[:8]

    entry = {
        "ts": ts,
        "chat_id_hash": chat_hash,
        "user_message": _safe_user_message_for_log(user_message, full_conversation_log),
        "recognized": {
            "display": recognized["display"],
            "matched_alias": recognized.get("matched_alias", ""),
            "confidence": recognized["confidence"],
            "protocol_file": recognized.get("protocol_file", ""),
        } if recognized else None,
        "retrieved": [
            {"source_label": c["source_label"], "similarity": round(c["similarity"], 4)}
            for c in retrieved_chunks
        ],
        "raw_llm": raw_llm,
        "final": final_response,
        "duration_ms": duration_ms,
        "trace": trace or {},
    }

    if query_log is not None:
        query_log.info(json.dumps(entry, ensure_ascii=False))

    reconstructable_turn = _reconstructable_turn_for_stdout(entry, full_conversation_log)
    if reconstructable_turn is not None:
        print("[TURN] " + json.dumps(reconstructable_turn, ensure_ascii=False), flush=True)

    rec_str = (
        f"{recognized['display']} ({recognized['confidence']})"
        if recognized else "NO MATCH"
    )
    chunks_str = "  ".join(
        f"{c['source_label']}:{round(c['similarity'], 2)}"
        for c in retrieved_chunks
    ) or "none"
    trace_str = _format_trace_for_stdout(entry.get("trace") or {})

    answer_preview = (final_response or "").replace("\n", " ").strip()
    if len(answer_preview) > 120:
        answer_preview = answer_preview[:117] + "..."

    prompt_preview = _safe_prompt_preview_for_stdout(user_message, full_conversation_log)
    print(f"[Q] {chat_hash} | {prompt_preview!r} -> {rec_str}", flush=True)
    print(f"[R] {chunks_str}", flush=True)
    if trace_str:
        print(f"[T] {trace_str}", flush=True)
    print(f"[A] {answer_preview}  [{duration_ms}ms]", flush=True)


__all__ = [
    "setup_logging",
    "_safe_user_message_for_log",
    "_safe_prompt_preview_for_stdout",
    "_reconstructable_turn_for_stdout",
    "_format_trace_for_stdout",
    "_log_query",
]
