"""
Allowlist and admin authorization helpers.
"""

import bot_core as _core


def _load_allowlist():
    return _core._load_allowlist()


def _load_admin_ids():
    return _core._load_admin_ids()


def _is_allowed(user_id: int) -> bool:
    return _core._is_allowed(user_id)


def _is_admin(user_id: int) -> bool:
    return _core._is_admin(user_id)


def __getattr__(name):
    return getattr(_core, name)


__all__ = [
    "ALLOWED_USER_IDS",
    "ADMIN_USER_IDS",
    "_load_allowlist",
    "_load_admin_ids",
    "_is_allowed",
    "_is_admin",
]
