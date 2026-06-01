"""
Allowlist and admin authorization helpers.

The helpers here are the implementation boundary. ``bot_core`` keeps wrapper
functions and compatibility globals so older imports and monkeypatches continue
to behave as before.
"""

import os


ALLOWED_USER_IDS: set[int] = set()
ADMIN_USER_IDS: set[int] = set()


def _parse_user_id_set(raw, label: str) -> set[int]:
    ids = set()
    if isinstance(raw, (list, tuple, set)):
        parts = raw
    else:
        parts = (raw or "").split(",")
    for part in parts:
        part = str(part).strip()
        if not part:
            continue
        if part.isdigit():
            ids.add(int(part))
        else:
            print(f"[{label}] WARNING: ignoring non-numeric entry: {part!r}")
    return ids


def _load_allowlist(runtime_options=None):
    """Return allowed Telegram user IDs, or an empty set for unrestricted use."""
    if runtime_options and "allowed_user_ids" in runtime_options:
        return _parse_user_id_set(runtime_options.get("allowed_user_ids"), "allowlist")
    return _parse_user_id_set(os.getenv("ALLOWED_USER_IDS", "").strip(), "allowlist")


def _load_admin_ids(runtime_options=None):
    """Return Telegram user IDs allowed to run admin-only commands."""
    if runtime_options and "admin_user_ids" in runtime_options:
        return _parse_user_id_set(runtime_options.get("admin_user_ids"), "admin")
    return _parse_user_id_set(os.getenv("ADMIN_USER_IDS", "").strip(), "admin")


def is_allowed(user_id: int, allowed_user_ids=None) -> bool:
    allowed = ALLOWED_USER_IDS if allowed_user_ids is None else allowed_user_ids
    return not allowed or user_id in allowed


def is_admin(user_id: int, admin_user_ids=None) -> bool:
    admins = ADMIN_USER_IDS if admin_user_ids is None else admin_user_ids
    return user_id in admins


def _is_allowed(user_id: int) -> bool:
    return is_allowed(user_id)


def _is_admin(user_id: int) -> bool:
    return is_admin(user_id)


__all__ = [
    "ALLOWED_USER_IDS",
    "ADMIN_USER_IDS",
    "_load_allowlist",
    "_load_admin_ids",
    "is_allowed",
    "is_admin",
    "_is_allowed",
    "_is_admin",
]
