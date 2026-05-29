"""
Audit logging and safe prompt-redaction boundary.
"""

import bot_core as _core


def setup_logging():
    return _core.setup_logging()


def _safe_user_message_for_log(user_message):
    return _core._safe_user_message_for_log(user_message)


def _safe_prompt_preview_for_stdout(user_message):
    return _core._safe_prompt_preview_for_stdout(user_message)


def _log_query(chat_id, user_message, recognized, retrieved_chunks, raw_llm, final_response, duration_ms):
    return _core._log_query(
        chat_id,
        user_message,
        recognized,
        retrieved_chunks,
        raw_llm,
        final_response,
        duration_ms,
    )


def __getattr__(name):
    return getattr(_core, name)


__all__ = [
    "_query_log",
    "setup_logging",
    "_safe_user_message_for_log",
    "_safe_prompt_preview_for_stdout",
    "_log_query",
]
