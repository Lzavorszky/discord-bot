"""
Audit logging, safe prompt redaction, and stdout trace formatting.
"""

import datetime as _dt
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import re


_TELEGRAM_TOKEN_RE = re.compile(r"bot\d+:[A-Za-z0-9_-]+")

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


class _TelegramTokenRedactionFilter(logging.Filter):
    def filter(self, record):
        if isinstance(record.msg, str):
            record.msg = _TELEGRAM_TOKEN_RE.sub("bot<redacted>", record.msg)
        if record.args:
            record.args = tuple(
                _TELEGRAM_TOKEN_RE.sub("bot<redacted>", arg)
                if isinstance(arg, str) else arg
                for arg in record.args
            )
        return True


def setup_logging(log_file):
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    for handler in logging.getLogger().handlers:
        handler.addFilter(_TelegramTokenRedactionFilter())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.WARNING)

    query_logger = logging.getLogger("query")
    query_logger.setLevel(logging.INFO)
    query_logger.propagate = False

    fh = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(message)s"))
    query_logger.addHandler(fh)

    logging.info(f"Logging initialised -> {log_file}")
    return query_logger


def _safe_user_message_for_log(user_message, full_conversation_log=False, preserve_user_message=False):
    if full_conversation_log or preserve_user_message:
        return user_message
    return {
        "redacted": True,
        "length": len(user_message or ""),
        "sha256_12": hashlib.sha256((user_message or "").encode("utf-8")).hexdigest()[:12],
    }


def _safe_prompt_preview_for_stdout(user_message, full_conversation_log=False, preserve_user_message=False):
    if full_conversation_log or preserve_user_message:
        text = (user_message or "").replace("\n", " ").strip()
        return text[:117] + "..." if len(text) > 120 else text
    return "<redacted>"


def _debug_options(debug_logging_options=None):
    options = dict(DEFAULT_DEBUG_LOGGING_OPTIONS)
    if isinstance(debug_logging_options, dict):
        options.update(debug_logging_options)
    return options


def _reconstructable_turn_for_stdout(entry, full_conversation_log=False, debug_logging_options=None):
    options = _debug_options(debug_logging_options)
    if not (full_conversation_log or options.get("stdout_full_turns")):
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


def _preserve_user_message_for_trace(trace, debug_logging_options=None):
    options = _debug_options(debug_logging_options)
    if options.get("log_user_messages"):
        return True
    return bool(
        options.get("log_admin_debug_notes")
        and (trace or {}).get("blocked_reason") == "admin_debug_note"
    )


def _logged_retrieved_chunks(retrieved_chunks, options):
    if not options.get("log_retrieved_chunks"):
        return []
    return [
        {"source_label": c["source_label"], "similarity": round(c["similarity"], 4)}
        for c in retrieved_chunks
    ]


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
    debug_logging_options=None,
):
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    chat_hash = hashlib.md5(str(chat_id).encode()).hexdigest()[:8]
    options = _debug_options(debug_logging_options)
    preserve_user_message = _preserve_user_message_for_trace(trace, options)
    logged_trace = (trace or {}) if options.get("log_routing_trace") else {}
    logged_final = final_response if options.get("log_bot_responses") else ""
    logged_raw_llm = raw_llm if options.get("log_raw_llm_responses") else ""
    logged_retrieved = _logged_retrieved_chunks(retrieved_chunks, options)

    entry = {
        "ts": ts,
        "chat_id_hash": chat_hash,
        "user_message": _safe_user_message_for_log(
            user_message,
            full_conversation_log,
            preserve_user_message,
        ),
        "recognized": {
            "display": recognized["display"],
            "matched_alias": recognized.get("matched_alias", ""),
            "confidence": recognized["confidence"],
            "protocol_file": recognized.get("protocol_file", ""),
        } if recognized else None,
        "retrieved": logged_retrieved,
        "raw_llm": logged_raw_llm,
        "final": logged_final,
        "duration_ms": duration_ms,
        "trace": logged_trace,
    }

    if query_log is not None:
        query_log.info(json.dumps(entry, ensure_ascii=False))

    reconstructable_turn = _reconstructable_turn_for_stdout(
        entry,
        full_conversation_log,
        options,
    )
    if reconstructable_turn is not None:
        print("[TURN] " + json.dumps(reconstructable_turn, ensure_ascii=False), flush=True)

    rec_str = (
        f"{recognized['display']} ({recognized['confidence']})"
        if recognized else "NO MATCH"
    )
    chunks_str = "  ".join(
        f"{c['source_label']}:{round(c['similarity'], 2)}"
        for c in retrieved_chunks
    ) if options.get("log_retrieved_chunks") else "<hidden>"
    chunks_str = chunks_str or "none"
    trace_str = _format_trace_for_stdout(entry.get("trace") or {})

    answer_preview = (
        (final_response or "").replace("\n", " ").strip()
        if options.get("log_bot_responses")
        else "<hidden>"
    )
    if len(answer_preview) > 120:
        answer_preview = answer_preview[:117] + "..."

    prompt_preview = _safe_prompt_preview_for_stdout(
        user_message,
        full_conversation_log,
        preserve_user_message or options.get("log_prompt_preview"),
    )
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
