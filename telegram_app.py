"""
Telegram command handlers and application startup boundary.
"""

import bot_core as _core


def split_message(text, max_length=4000):
    return _core.split_message(text, max_length)


async def handle_start(update, context):
    return await _core.handle_start(update, context)


async def handle_whoami(update, context):
    return await _core.handle_whoami(update, context)


async def handle_reset(update, context):
    return await _core.handle_reset(update, context)


async def handle_protocols(update, context):
    return await _core.handle_protocols(update, context)


async def handle_version(update, context):
    return await _core.handle_version(update, context)


async def handle_reload(update, context):
    return await _core.handle_reload(update, context)


async def handle_debug(update, context):
    return await _core.handle_debug(update, context)


async def handle_message(update, context):
    return await _core.handle_message(update, context)


def main():
    return _core.main()


def __getattr__(name):
    return getattr(_core, name)


__all__ = [
    "split_message",
    "handle_start",
    "handle_whoami",
    "handle_reset",
    "handle_protocols",
    "handle_version",
    "handle_reload",
    "handle_debug",
    "handle_message",
    "main",
]
