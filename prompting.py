"""
Prompt and rule-file assembly boundary.
"""

import bot_core as _core


def load_text_file(path):
    return _core.load_text_file(path)


def load_rule_files():
    return _core.load_rule_files()


def build_recognition_context(recognized):
    return _core.build_recognition_context(recognized)


def build_system_prompt(recognized, context):
    return _core.build_system_prompt(recognized, context)


def __getattr__(name):
    return getattr(_core, name)


__all__ = [
    "SYSTEM_RULES",
    "ANSWER_FORMAT_RULES",
    "ANSWER_STYLE_RULES",
    "SAFETY_RULES",
    "SOURCE_INSTRUCTION",
    "POLICY_INSTRUCTION",
    "load_text_file",
    "load_rule_files",
    "build_recognition_context",
    "build_system_prompt",
]
