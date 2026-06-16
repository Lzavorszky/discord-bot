"""ID Bot rebuild tools (Phase 3+). Each tool returns a verbatim, structured
answer from an approved protocol — never a computed one."""
from .get_dose import (
    get_dose,
    render_dose,
    load_drug_dose,
    DoseResult,
    Tier,
    DoseError,
    GuardError,
    DEFAULT_ANSWER,
)

__all__ = [
    "get_dose", "render_dose", "load_drug_dose",
    "DoseResult", "Tier", "DoseError", "GuardError", "DEFAULT_ANSWER",
]
