"""
Read-only Google Sheets rota lookup for the Telegram bot.

This module intentionally stays independent from the clinical RAG/LLM path.
It reads rendered spreadsheet values, searches for a requested date column and
role row, and returns the assignment cell without interpreting staff initials.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import os
import re
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


try:
    BUDAPEST_TZ = ZoneInfo("Europe/Budapest")
except ZoneInfoNotFoundError:
    BUDAPEST_TZ = timezone(timedelta(hours=1), "Europe/Budapest")
SHEETS_READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
DEFAULT_USED_RANGE = "A:ZZ"

STATUS_FOUND = "found"
STATUS_NOT_FOUND = "not_found"
STATUS_MULTIPLE_MATCHES = "multiple_matches"
STATUS_CONFIGURATION_ERROR = "configuration_error"
STATUS_PERMISSION_ERROR = "permission_error"
STATUS_SHEET_ERROR = "sheet_error"

EGES = "\u00c9g\u00e9s"
UGYELETVEZETO = "\u00dcgyeletvezet\u0151"
ANESZT_UGYELET = "Aneszt \u00fcgyelet"
SZIVSEB_UGYELET = "Sz\u00edvseb \u00fcgyelet"
SURGOS_UGYELET = "S\u00fcrg\u0151s \u00fcgyelet"


@dataclass(frozen=True)
class RotaMatch:
    assignment: str
    source: str
    row_index: int
    column_index: int


@dataclass(frozen=True)
class RotaResult:
    status: str
    date_value: date | None = None
    role: str | None = None
    assignment: str | None = None
    source: str | None = None
    matches: tuple[RotaMatch, ...] = ()


class RotaConfigurationError(RuntimeError):
    """Raised when local configuration is missing or invalid."""


def safe_configuration_diagnostics() -> list[str]:
    """Return non-secret configuration problems useful for local diagnostics."""
    problems: list[str] = []
    if not os.getenv("ROTA_SPREADSHEET_ID", "").strip():
        problems.append("ROTA_SPREADSHEET_ID is not set")

    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw_json:
        problems.append("GOOGLE_SERVICE_ACCOUNT_JSON is not set")
    else:
        try:
            json.loads(raw_json)
        except json.JSONDecodeError:
            problems.append("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON")

    try:
        import google.oauth2.service_account  # noqa: F401
    except ImportError:
        problems.append("google-auth is not installed in this Python environment")

    try:
        import googleapiclient.discovery  # noqa: F401
    except ImportError:
        problems.append("google-api-python-client is not installed in this Python environment")

    return problems


def _strip_accents(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _role_key(value: str) -> str:
    value = _strip_accents(str(value or "")).casefold()
    value = re.sub(r"[^\w\s]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


ROLE_ALIASES = {
    _role_key(EGES): EGES,
    "eges": EGES,
    _role_key(UGYELETVEZETO): UGYELETVEZETO,
    "ugyeletvezeto": UGYELETVEZETO,
    _role_key(ANESZT_UGYELET): ANESZT_UGYELET,
    "aneszt": ANESZT_UGYELET,
    "aneszt ugyelet": ANESZT_UGYELET,
    _role_key(SZIVSEB_UGYELET): SZIVSEB_UGYELET,
    "szivseb ugyelet": SZIVSEB_UGYELET,
    _role_key(SURGOS_UGYELET): SURGOS_UGYELET,
    "surgos ugyelet": SURGOS_UGYELET,
}


def normalize_role(role_text: str) -> str:
    """Return the canonical role label when a known alias is supplied."""
    text = str(role_text or "").strip()
    return ROLE_ALIASES.get(_role_key(text), text)


def normalise_role(role_text: str) -> str:
    """British spelling alias kept for readability in tests and callers."""
    return normalize_role(role_text)


def parse_date_input(value: str, reference_now: datetime | date | None = None) -> date:
    """Parse Telegram date input using Europe/Budapest for relative words."""
    raw = str(value or "").strip()
    key = _role_key(raw)

    if reference_now is None:
        base_date = datetime.now(BUDAPEST_TZ).date()
    elif isinstance(reference_now, datetime):
        if reference_now.tzinfo is None:
            reference_now = reference_now.replace(tzinfo=BUDAPEST_TZ)
        base_date = reference_now.astimezone(BUDAPEST_TZ).date()
    elif isinstance(reference_now, date):
        base_date = reference_now
    else:
        raise TypeError("reference_now must be a date, datetime, or None")

    if key in {"today", "ma"}:
        return base_date
    if key in {"tomorrow", "holnap"}:
        return base_date + timedelta(days=1)

    iso_match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    dotted_match = re.fullmatch(r"(\d{4})\s*\.\s*(\d{1,2})\s*\.\s*(\d{1,2})\.?", raw)
    match = iso_match or dotted_match
    if match:
        year, month, day = (int(part) for part in match.groups())
        return date(year, month, day)

    raise ValueError(f"Unsupported rota date format: {value!r}")


def _parse_sheet_date_cell(value) -> date | None:
    raw = str(value or "").strip()
    if not raw:
        return None

    # Rendered Sheets values may include a midnight timestamp after the date.
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


def build_sheets_service():
    """Build a read-only Google Sheets client from service account JSON."""
    raw_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw_json:
        raise RotaConfigurationError("GOOGLE_SERVICE_ACCOUNT_JSON is not configured")

    try:
        service_account_info = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise RotaConfigurationError("GOOGLE_SERVICE_ACCOUNT_JSON is invalid JSON") from exc

    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RotaConfigurationError("Google Sheets dependencies are not installed") from exc

    credentials = Credentials.from_service_account_info(
        service_account_info,
        scopes=[SHEETS_READONLY_SCOPE],
    )
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def _sheet_range(title: str, used_range: str = DEFAULT_USED_RANGE) -> str:
    escaped_title = str(title).replace("'", "''")
    return f"'{escaped_title}'!{used_range}"


def _get_cell(rows, row_index: int, column_index: int) -> str:
    if row_index < 0 or row_index >= len(rows):
        return ""
    row = rows[row_index] or []
    if column_index < 0 or column_index >= len(row):
        return ""
    return str(row[column_index]).strip()


def _row_contains_role(row, requested_role: str) -> bool:
    requested_key = _role_key(normalize_role(requested_role))
    for cell in row or []:
        if _role_key(normalize_role(str(cell))) == requested_key:
            return True
    return False


def _find_tab_matches(rows, tab_title: str, requested_date: date, requested_role: str) -> list[RotaMatch]:
    matches: list[RotaMatch] = []

    for date_row_index, row in enumerate(rows):
        for date_column_index, cell in enumerate(row or []):
            if _parse_sheet_date_cell(cell) != requested_date:
                continue

            # Human rota blocks usually have a date row followed by role rows.
            # The bounded scan avoids drifting into unrelated repeated blocks.
            first_role_row = date_row_index + 1
            last_role_row = min(len(rows), date_row_index + 41)
            for role_row_index in range(first_role_row, last_role_row):
                role_row = rows[role_row_index] or []
                if not _row_contains_role(role_row, requested_role):
                    continue
                assignment = _get_cell(rows, role_row_index, date_column_index)
                if assignment:
                    matches.append(
                        RotaMatch(
                            assignment=assignment,
                            source=tab_title,
                            row_index=role_row_index,
                            column_index=date_column_index,
                        )
                    )
                break

    return matches


def _is_permission_error(exc: Exception) -> bool:
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None) or getattr(exc, "status_code", None)
    try:
        return int(status) in {401, 403}
    except (TypeError, ValueError):
        return False


def lookup_oncall(
    date_value: date,
    role: str,
    *,
    service=None,
    spreadsheet_id: str | None = None,
    used_range: str = DEFAULT_USED_RANGE,
) -> RotaResult:
    """Look up one rota assignment across all worksheet tabs."""
    canonical_role = normalize_role(role)
    spreadsheet_id = spreadsheet_id or os.getenv("ROTA_SPREADSHEET_ID", "").strip()
    if not spreadsheet_id:
        return RotaResult(STATUS_CONFIGURATION_ERROR, date_value, canonical_role)

    try:
        sheets_service = service or build_sheets_service()
        spreadsheets = sheets_service.spreadsheets()
        metadata = spreadsheets.get(spreadsheetId=spreadsheet_id).execute()
        tab_titles = [
            sheet.get("properties", {}).get("title")
            for sheet in metadata.get("sheets", [])
            if sheet.get("properties", {}).get("title")
        ]

        all_matches: list[RotaMatch] = []
        for tab_title in tab_titles:
            values_response = (
                spreadsheets.values()
                .get(spreadsheetId=spreadsheet_id, range=_sheet_range(tab_title, used_range))
                .execute()
            )
            rows = values_response.get("values", [])
            all_matches.extend(_find_tab_matches(rows, tab_title, date_value, canonical_role))
    except RotaConfigurationError:
        return RotaResult(STATUS_CONFIGURATION_ERROR, date_value, canonical_role)
    except Exception as exc:
        status = STATUS_PERMISSION_ERROR if _is_permission_error(exc) else STATUS_SHEET_ERROR
        return RotaResult(status, date_value, canonical_role)

    if not all_matches:
        return RotaResult(STATUS_NOT_FOUND, date_value, canonical_role)

    distinct_assignments = {match.assignment for match in all_matches}
    if len(distinct_assignments) > 1:
        return RotaResult(
            STATUS_MULTIPLE_MATCHES,
            date_value,
            canonical_role,
            matches=tuple(all_matches),
        )

    first_match = all_matches[0]
    return RotaResult(
        STATUS_FOUND,
        date_value,
        canonical_role,
        assignment=first_match.assignment,
        source=first_match.source,
        matches=tuple(all_matches),
    )


def _diagnose(argv: list[str]) -> int:
    if len(argv) < 3:
        print('Usage: python rota_lookup.py <date> "<role>"')
        return 2
    try:
        requested_date = parse_date_input(argv[1])
    except ValueError:
        print("Invalid date.")
        return 2

    result = lookup_oncall(requested_date, " ".join(argv[2:]))
    if result.status == STATUS_FOUND:
        print(f"{result.date_value.isoformat()} - {result.role}")
        print(f"- {result.assignment}")
        print(f"Source: {result.source}")
        return 0
    if result.status == STATUS_CONFIGURATION_ERROR:
        print(result.status)
        problems = safe_configuration_diagnostics()
        for problem in problems:
            print(f"- {problem}")
        if not problems:
            print("- configuration failed while building the Sheets client")
        return 1
    print(result.status)
    return 1


if __name__ == "__main__":
    import sys

    raise SystemExit(_diagnose(sys.argv))
