"""
Thin entrypoint for the infectious diseases Telegram bot.

Session 11 keeps the legacy public surface import-compatible while the
implementation lives behind focused modules. Importing this module returns
``bot_core`` so existing tests and scripts that monkeypatch ``telegram_bot``
continue to patch the implementation globals they expect.
"""

import sys as _sys

import bot_core as _core


if __name__ == "__main__":
    _core.main()
else:
    _sys.modules[__name__] = _core
