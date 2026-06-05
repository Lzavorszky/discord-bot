"""
Read-only Google Sheets lookup for the nursing rota Telegram command.

This module intentionally stays independent from the clinical RAG/LLM path.
It reads the rendered values from the fixed nursing "Napi" tab and extracts
the day and night nurse lists for the requested date column.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import os
import re

from rota_lookup import (
    DEFAULT_USED_RANGE,
    RotaConfigurationError,
    STATUS_CONFIGURATION_ERROR,
    STATUS_FOUND,
    STATUS_NOT_FOUND,
    STATUS_PERMISSION_ERROR,
    STATUS_SHEET_ERROR,
    build_sheets_service,
    parse_date_input,
)


NURSING_SPREADSHEET_ENV = "NURSING_ROTA_SPREADSHEET_ID"
NURSING_TAB_TITLE = "Napi"
NURSING_USED_RANGE = "A:AZ"

TODAY_COLUMN_INDEX = 1  # Column B.
TOMORROW_COLUMN_INDEX = 2  # Column C.
DATE_ROW_INDEX = 3  # Row 4.
DAY_COUNT_ROW_INDEX = 5  # Row 6.
DAY_NAMES_START_INDEX = 6  # Row 7.
DAY_NAMES_END_INDEX = 21  # Row 21, exclusive.
NIGHT_COUNT_ROW_INDEX = 21  # Row 22.
NIGHT_NAMES_START_INDEX = 22  # Row 23.
NIGHT_NAMES_END_INDEX = 37  # Row 37, exclusive.


@dataclass(frozen=True)
class NursingShift:
    label: str
    declared_count: str
    nurses: tuple[str, ...]

    @property
    def listed_count(self) -> int:
        return len(self.nurses)


@dataclass(frozen=True)
class NursingRotaResult:
    status: str
    date_value: date | None = None
    source: str | None = None
    day: NursingShift | None = None
    night: NursingShift | None = None


def _sheet_range(title: str, used_range: str = DEFAULT_USED_RANGE) -> str:
    escaped_title = str(title).replace("'", "''")
    return f"'{escaped_title}'!{used_range}"


def _get_cell(rows, row_index: int, column_index: int) -> str:
    if row_index < 0 or row_index >= len(rows):
        return ""
    row = rows[row_index] or []
    if column_index < 0 or column_index >= len(row):
        return ""
    return str(row[column_index] or "").strip()


def _parse_sheet_date_cell(value) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None

    iso_prefix = re.match(r"^(\d{4}-\d{1,2}-\d{1,2})(?:[ T].*)?$", raw)
    if iso_prefix:
        try:
            return parse_date_input(iso_prefix.group(1))
        except ValueError:
            return None

    dotted_prefix = re.match(r"^(\d{4}\s*\.\s*\d{1,2}\s*\.\s*\d{1,2})(?:\.|\s.*)?$", raw)
    if dotted_prefix:
        try:
            return parse_date_input(dotted_prefix.group(1))
        except ValueError:
            return None

    return None


def _is_permission_error(exc: Exception) -> bool:
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None) or getattr(exc, "status_code", None)
    try:
        return int(status) in {401, 403}
    except (TypeError, ValueError):
        return False


def _is_name(value: str) -> bool:
    text = str(value or "").strip()
    if not text or text in {"-", "/"}:
        return False
    if re.fullmatch(r"\d+(?:\+\d+)+", text):
        return False
    if text.casefold() in {"nappal", "ejszaka", "éjszaka", "egyeb", "egyéb"}:
        return False
    return True


def _unique_names(values) -> tuple[str, ...]:
    seen = set()
    names: list[str] = []
    for value in values:
        name = str(value or "").strip()
        if not _is_name(name):
            continue
        key = re.sub(r"\s+", " ", name).casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return tuple(names)


def _column_values(rows, column_index: int, start_index: int, end_index: int) -> list[str]:
    return [_get_cell(rows, row_index, column_index) for row_index in range(start_index, end_index)]


def _column_for_day(day: str) -> int:
    return TOMORROW_COLUMN_INDEX if day == "tomorrow" else TODAY_COLUMN_INDEX


def _build_shift(
    rows,
    *,
    label: str,
    column_index: int,
    count_row_index: int,
    names_start_index: int,
    names_end_index: int,
) -> NursingShift:
    return NursingShift(
        label=label,
        declared_count=_get_cell(rows, count_row_index, column_index),
        nurses=_unique_names(_column_values(rows, column_index, names_start_index, names_end_index)),
    )


def _parse_nursing_rota(rows, day: str = "today") -> NursingRotaResult:
    column_index = _column_for_day(day)
    date_value = _parse_sheet_date_cell(_get_cell(rows, DATE_ROW_INDEX, column_index))

    day = _build_shift(
        rows,
        label="Nappal",
        column_index=column_index,
        count_row_index=DAY_COUNT_ROW_INDEX,
        names_start_index=DAY_NAMES_START_INDEX,
        names_end_index=DAY_NAMES_END_INDEX,
    )
    night = _build_shift(
        rows,
        label="\u00c9jszaka",
        column_index=column_index,
        count_row_index=NIGHT_COUNT_ROW_INDEX,
        names_start_index=NIGHT_NAMES_START_INDEX,
        names_end_index=NIGHT_NAMES_END_INDEX,
    )

    if not day.nurses and not night.nurses:
        return NursingRotaResult(STATUS_NOT_FOUND, date_value, NURSING_TAB_TITLE, day, night)
    return NursingRotaResult(STATUS_FOUND, date_value, NURSING_TAB_TITLE, day, night)


def lookup_nursing_rota(
    day: str = "today",
    *,
    service=None,
    spreadsheet_id: str | None = None,
    used_range: str = NURSING_USED_RANGE,
) -> NursingRotaResult:
    """Return day and night nursing staff for the Napi sheet today/tomorrow column."""
    day = "tomorrow" if str(day or "").strip().casefold() == "tomorrow" else "today"
    spreadsheet_id = spreadsheet_id or os.getenv(NURSING_SPREADSHEET_ENV, "").strip()
    if not spreadsheet_id:
        return NursingRotaResult(STATUS_CONFIGURATION_ERROR, None, NURSING_TAB_TITLE)

    try:
        sheets_service = service or build_sheets_service()
        rows = (
            sheets_service.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=_sheet_range(NURSING_TAB_TITLE, used_range))
            .execute()
            .get("values", [])
        )
    except RotaConfigurationError:
        return NursingRotaResult(STATUS_CONFIGURATION_ERROR, None, NURSING_TAB_TITLE)
    except Exception as exc:
        status = STATUS_PERMISSION_ERROR if _is_permission_error(exc) else STATUS_SHEET_ERROR
        return NursingRotaResult(status, None, NURSING_TAB_TITLE)

    return _parse_nursing_rota(rows, day)
