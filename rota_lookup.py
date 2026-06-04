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

SECTION_DAILY = "daily"
SECTION_LONG = "long"
SECTION_ONCALL = "oncall"

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
    "napi vezeto": UGYELETVEZETO,
    _role_key(ANESZT_UGYELET): ANESZT_UGYELET,
    "aneszt": ANESZT_UGYELET,
    "aneszt ugyelet": ANESZT_UGYELET,
    _role_key(SZIVSEB_UGYELET): SZIVSEB_UGYELET,
    "szivseb": SZIVSEB_UGYELET,
    "szivseb ugyelet": SZIVSEB_UGYELET,
    _role_key(SURGOS_UGYELET): SURGOS_UGYELET,
    "surgos": SURGOS_UGYELET,
    "surgos ugyelet": SURGOS_UGYELET,
}

SECTION_MARKERS = {
    "nappali munka": SECTION_DAILY,
    "hosszu": SECTION_LONG,
    "ugyeletek": SECTION_ONCALL,
    "tavolletek": "away",
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


def _is_real_assignment(value: str) -> bool:
    return bool(value and value.strip() and value.strip() not in {"-", "/"})


def _label_key(value: str) -> str:
    key = _role_key(value)
    key = re.sub(r"^\d+\s+", "", key)
    key = re.sub(r"\s+\d+$", "", key)
    return key.strip()


def _row_label(row) -> str:
    # In the rota export, labels are in column B. Fall back to any non-empty
    # cell so the parser remains usable if the sheet shifts later.
    if row and len(row) > 1 and str(row[1]).strip():
        return str(row[1]).strip()
    for cell in row or []:
        if str(cell).strip():
            return str(cell).strip()
    return ""


def _candidate_row_labels(row) -> list[str]:
    labels: list[str] = []
    if row and len(row) > 1 and str(row[1]).strip():
        labels.append(str(row[1]).strip())
    for cell in row or []:
        value = str(cell).strip()
        if value and value not in labels:
            labels.append(value)
    return labels


def _row_has_any_date(row) -> bool:
    return any(_parse_sheet_date_cell(cell) is not None for cell in row or [])


def _next_date_row_index(rows, start_row_index: int) -> int:
    for row_index in range(start_row_index + 1, len(rows)):
        if _row_has_any_date(rows[row_index]):
            return row_index
    return len(rows)


def _section_for_label(label: str) -> str | None:
    return SECTION_MARKERS.get(_label_key(label))


def _nearest_section(rows, row_index: int) -> str:
    for previous_index in range(row_index - 1, -1, -1):
        section = _section_for_label(_row_label(rows[previous_index] or []))
        if section:
            return section
    return ""


def _label_matches_role(label: str, canonical: str, section: str) -> bool:
    key = _label_key(label)
    requested_key = _role_key(canonical)

    if key == requested_key:
        return True
    if ROLE_ALIASES.get(key) == canonical:
        return True
    if requested_key == "eges" and key == "eges":
        return True
    if requested_key == "aneszt ugyelet" and key.startswith("aneszt ugyelet"):
        return True
    if requested_key == "aneszt ugyelet" and section == SECTION_ONCALL and key.startswith("aneszt"):
        return True
    if requested_key == "aneszt ugyelet" and section == SECTION_LONG and key.startswith("aneszt hosszu"):
        return True
    if requested_key == "szivseb ugyelet" and section == SECTION_ONCALL and key.startswith("szivseb"):
        return True
    if requested_key == "szivseb ugyelet" and section == SECTION_LONG and key.startswith("szivseb hosszu"):
        return True
    if requested_key == "surgos ugyelet" and section == SECTION_ONCALL and key == "surgos":
        return True
    return False


def _matching_row_label(rows, row_index: int, requested_role: str) -> str | None:
    row = rows[row_index] or []
    canonical = normalize_role(requested_role)
    section = _nearest_section(rows, row_index)
    for label in _candidate_row_labels(row):
        if _label_matches_role(label, canonical, section):
            return label
    return None


def _iter_date_columns(rows, requested_date: date):
    for date_row_index, row in enumerate(rows):
        for date_column_index, cell in enumerate(row or []):
            if _parse_sheet_date_cell(cell) == requested_date:
                yield date_row_index, date_column_index, _next_date_row_index(rows, date_row_index)


def _section_bounds(rows, block_start: int, block_end: int, section: str | None) -> tuple[int, int]:
    if section is None:
        return block_start + 1, block_end

    start = None
    for row_index in range(block_start + 1, block_end):
        row_section = _section_for_label(_row_label(rows[row_index] or []))
        if row_section == section:
            start = row_index + 1
            break
    if start is None:
        return block_end, block_end

    end = block_end
    for row_index in range(start, block_end):
        if _section_for_label(_row_label(rows[row_index] or [])):
            end = row_index
            break
    return start, end


def _find_tab_matches(
    rows,
    tab_title: str,
    requested_date: date,
    requested_role: str,
    *,
    section: str | None = None,
) -> list[RotaMatch]:
    matches: list[RotaMatch] = []

    for date_row_index, date_column_index, block_end in _iter_date_columns(rows, requested_date):
        first_role_row, last_role_row = _section_bounds(rows, date_row_index, block_end, section)
        row_assignments: list[tuple[str, str, int]] = []
        for role_row_index in range(first_role_row, last_role_row):
            matched_label = _matching_row_label(rows, role_row_index, requested_role)
            if not matched_label:
                continue
            assignment = _get_cell(rows, role_row_index, date_column_index)
            if assignment:
                row_assignments.append((matched_label, assignment, role_row_index))

        if row_assignments:
            if len(row_assignments) == 1:
                assignment = row_assignments[0][1]
                row_index = row_assignments[0][2]
            else:
                assignment = "\n".join(
                    f"{label}: {assignment}"
                    for label, assignment, _row_index in row_assignments
                )
                row_index = row_assignments[0][2]
            matches.append(
                RotaMatch(
                    assignment=assignment,
                    source=tab_title,
                    row_index=row_index,
                    column_index=date_column_index,
                )
            )

    return matches


def _assignment_contains_person(assignment: str, person: str) -> bool:
    assignment_key = _role_key(assignment)
    person_key = _role_key(person)
    if not person_key:
        return False
    tokens = [token for token in re.split(r"\s+", assignment_key) if token]
    return person_key in tokens


def _find_person_daily_matches(rows, tab_title: str, requested_date: date, person: str) -> list[RotaMatch]:
    matches: list[RotaMatch] = []
    for date_row_index, date_column_index, block_end in _iter_date_columns(rows, requested_date):
        first_role_row, last_role_row = _section_bounds(rows, date_row_index, block_end, SECTION_DAILY)
        for role_row_index in range(first_role_row, last_role_row):
            assignment = _get_cell(rows, role_row_index, date_column_index)
            if not _assignment_contains_person(assignment, person):
                continue
            label = _row_label(rows[role_row_index] or [])
            matches.append(
                RotaMatch(
                    assignment=f"{label}: {assignment}",
                    source=tab_title,
                    row_index=role_row_index,
                    column_index=date_column_index,
                )
            )
    return matches


def _section_rows(rows, date_row_index: int, block_end: int, section: str, date_column_index: int):
    first_role_row, last_role_row = _section_bounds(rows, date_row_index, block_end, section)
    for row_index in range(first_role_row, last_role_row):
        label = _row_label(rows[row_index] or [])
        assignment = _get_cell(rows, row_index, date_column_index)
        if label and _is_real_assignment(assignment):
            yield label, assignment, row_index


def _find_section_summary_matches(
    rows,
    tab_title: str,
    requested_date: date,
    section: str,
    *,
    include_labels: bool,
) -> list[RotaMatch]:
    matches: list[RotaMatch] = []
    for date_row_index, date_column_index, block_end in _iter_date_columns(rows, requested_date):
        section_rows = list(_section_rows(rows, date_row_index, block_end, section, date_column_index))
        if not section_rows:
            continue
        if include_labels:
            assignment = "\n".join(f"{label}: {value}" for label, value, _row_index in section_rows)
        else:
            assignment = _join_staff_tokens(value for _label, value, _row_index in section_rows)
        if assignment:
            matches.append(
                RotaMatch(
                    assignment=assignment,
                    source=tab_title,
                    row_index=section_rows[0][2],
                    column_index=date_column_index,
                )
            )
    return matches


def _staff_tokens(value: str) -> list[str]:
    tokens: list[str] = []
    for part in str(value or "").split(","):
        token = part.strip()
        if _is_real_assignment(token):
            tokens.append(token)
    return tokens


def _join_staff_tokens(values) -> str:
    seen = set()
    tokens: list[str] = []
    for value in values:
        for token in _staff_tokens(value):
            key = _role_key(token)
            if key and key not in seen:
                seen.add(key)
                tokens.append(token)
    return ", ".join(tokens)


def _find_role_location_matches(rows, tab_title: str, requested_date: date, requested_role: str) -> list[RotaMatch]:
    matches: list[RotaMatch] = []
    for date_row_index, date_column_index, block_end in _iter_date_columns(rows, requested_date):
        row_assignments: list[tuple[str, str, int]] = []
        for row_index in range(date_row_index + 1, block_end):
            matched_label = _matching_row_label(rows, row_index, requested_role)
            if not matched_label:
                continue
            assignment = _get_cell(rows, row_index, date_column_index)
            if _is_real_assignment(assignment):
                section = _nearest_section(rows, row_index)
                prefix = {
                    SECTION_DAILY: "Napi",
                    SECTION_LONG: "Hossz\u00fa",
                    SECTION_ONCALL: "\u00dcgyelet",
                }.get(section, "Rota")
                row_assignments.append((f"{prefix} - {matched_label}", assignment, row_index))

        if row_assignments:
            assignment = "\n".join(
                f"{label}: {assignment}"
                for label, assignment, _row_index in row_assignments
            )
            matches.append(
                RotaMatch(
                    assignment=assignment,
                    source=tab_title,
                    row_index=row_assignments[0][2],
                    column_index=date_column_index,
                )
            )
    return matches


def _find_oncall_summary_matches(rows, tab_title: str, requested_date: date) -> list[RotaMatch]:
    matches: list[RotaMatch] = []
    for date_row_index, date_column_index, block_end in _iter_date_columns(rows, requested_date):
        by_label: dict[str, str] = {}
        first_row_index = None
        for label, assignment, row_index in _section_rows(rows, date_row_index, block_end, SECTION_ONCALL, date_column_index):
            by_label[_label_key(label)] = assignment
            if first_row_index is None:
                first_row_index = row_index

        if not by_label:
            continue

        def values_for(*labels):
            return [by_label[label] for label in labels if by_label.get(label)]

        lines = [
            "ITO: " + _join_staff_tokens(values_for("multi ito1", "multi ito2", "surgos")),
            "Sz\u00edv: " + _join_staff_tokens(values_for("szivseb ito")),
            "M\u0171t\u0151: " + _join_staff_tokens(values_for("aneszt1", "aneszt2")),
            "Telefon: " + _join_staff_tokens(values_for("aneszt3", "szub", "ii th", "sziv telefon", "tel")),
        ]
        assignment = "\n".join(line for line in lines if not line.endswith(": "))
        if assignment:
            matches.append(
                RotaMatch(
                    assignment=assignment,
                    source=tab_title,
                    row_index=first_row_index or date_row_index,
                    column_index=date_column_index,
                )
            )
    return matches


def _is_permission_error(exc: Exception) -> bool:
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None) or getattr(exc, "status_code", None)
    try:
        return int(status) in {401, 403}
    except (TypeError, ValueError):
        return False


def _lookup_matches(match_builder, *, service=None, spreadsheet_id: str | None = None, used_range: str = DEFAULT_USED_RANGE):
    spreadsheet_id = spreadsheet_id or os.getenv("ROTA_SPREADSHEET_ID", "").strip()
    if not spreadsheet_id:
        return STATUS_CONFIGURATION_ERROR, []

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
            all_matches.extend(match_builder(rows, tab_title))
    except RotaConfigurationError:
        return STATUS_CONFIGURATION_ERROR, []
    except Exception as exc:
        status = STATUS_PERMISSION_ERROR if _is_permission_error(exc) else STATUS_SHEET_ERROR
        return status, []

    return None, all_matches


def _result_from_matches(date_value: date, role: str, matches: list[RotaMatch]) -> RotaResult:
    if not matches:
        return RotaResult(STATUS_NOT_FOUND, date_value, role)

    distinct_assignments = {match.assignment for match in matches}
    if len(distinct_assignments) > 1:
        return RotaResult(
            STATUS_MULTIPLE_MATCHES,
            date_value,
            role,
            matches=tuple(matches),
        )

    first_match = matches[0]
    return RotaResult(
        STATUS_FOUND,
        date_value,
        role,
        assignment=first_match.assignment,
        source=first_match.source,
        matches=tuple(matches),
    )


def _summary_result_from_matches(date_value: date, role: str, matches: list[RotaMatch]) -> RotaResult:
    if not matches:
        return RotaResult(STATUS_NOT_FOUND, date_value, role)

    lines: list[str] = []
    seen = set()
    for match in matches:
        for line in str(match.assignment or "").splitlines():
            line = line.strip()
            if not line:
                continue
            key = _role_key(line)
            if key in seen:
                continue
            seen.add(key)
            lines.append(line)

    if not lines:
        return RotaResult(STATUS_NOT_FOUND, date_value, role, matches=tuple(matches))

    return RotaResult(
        STATUS_FOUND,
        date_value,
        role,
        assignment="\n".join(lines),
        source=matches[0].source,
        matches=tuple(matches),
    )


def _staff_list_result_from_matches(date_value: date, role: str, matches: list[RotaMatch]) -> RotaResult:
    if not matches:
        return RotaResult(STATUS_NOT_FOUND, date_value, role)

    assignment = _join_staff_tokens(match.assignment for match in matches)
    if not assignment:
        return RotaResult(STATUS_NOT_FOUND, date_value, role, matches=tuple(matches))

    return RotaResult(
        STATUS_FOUND,
        date_value,
        role,
        assignment=assignment,
        source=matches[0].source,
        matches=tuple(matches),
    )


def lookup_rota(
    date_value: date,
    role: str,
    *,
    section: str | None = None,
    service=None,
    spreadsheet_id: str | None = None,
    used_range: str = DEFAULT_USED_RANGE,
) -> RotaResult:
    """Look up one role assignment in a named rota section across all tabs."""
    canonical_role = normalize_role(role)
    error_status, matches = _lookup_matches(
        lambda rows, tab_title: _find_tab_matches(
            rows,
            tab_title,
            date_value,
            canonical_role,
            section=section,
        ),
        service=service,
        spreadsheet_id=spreadsheet_id,
        used_range=used_range,
    )
    if error_status:
        return RotaResult(error_status, date_value, canonical_role)
    return _result_from_matches(date_value, canonical_role, matches)


def lookup_oncall(date_value: date, role: str, **kwargs) -> RotaResult:
    return lookup_rota(date_value, role, section=SECTION_ONCALL, **kwargs)


def lookup_daily_rota(date_value: date, role: str, **kwargs) -> RotaResult:
    return lookup_rota(date_value, role, section=SECTION_DAILY, **kwargs)


def lookup_long_rota(date_value: date, role: str, **kwargs) -> RotaResult:
    return lookup_rota(date_value, role, section=SECTION_LONG, **kwargs)


def lookup_role_locations(
    date_value: date,
    role: str,
    *,
    service=None,
    spreadsheet_id: str | None = None,
    used_range: str = DEFAULT_USED_RANGE,
) -> RotaResult:
    canonical_role = normalize_role(role)
    error_status, matches = _lookup_matches(
        lambda rows, tab_title: _find_role_location_matches(rows, tab_title, date_value, canonical_role),
        service=service,
        spreadsheet_id=spreadsheet_id,
        used_range=used_range,
    )
    if error_status:
        return RotaResult(error_status, date_value, canonical_role)
    return _result_from_matches(date_value, canonical_role, matches)


def lookup_daily_summary(
    date_value: date,
    *,
    service=None,
    spreadsheet_id: str | None = None,
    used_range: str = DEFAULT_USED_RANGE,
) -> RotaResult:
    error_status, matches = _lookup_matches(
        lambda rows, tab_title: _find_section_summary_matches(
            rows,
            tab_title,
            date_value,
            SECTION_DAILY,
            include_labels=True,
        ),
        service=service,
        spreadsheet_id=spreadsheet_id,
        used_range=used_range,
    )
    if error_status:
        return RotaResult(error_status, date_value, "Napi rota")
    return _summary_result_from_matches(date_value, "Napi rota", matches)


def lookup_long_summary(
    date_value: date,
    *,
    service=None,
    spreadsheet_id: str | None = None,
    used_range: str = DEFAULT_USED_RANGE,
) -> RotaResult:
    error_status, matches = _lookup_matches(
        lambda rows, tab_title: _find_section_summary_matches(
            rows,
            tab_title,
            date_value,
            SECTION_LONG,
            include_labels=False,
        ),
        service=service,
        spreadsheet_id=spreadsheet_id,
        used_range=used_range,
    )
    if error_status:
        return RotaResult(error_status, date_value, "Hossz\u00fa")
    return _staff_list_result_from_matches(date_value, "Hossz\u00fa", matches)


def lookup_oncall_summary(
    date_value: date,
    *,
    service=None,
    spreadsheet_id: str | None = None,
    used_range: str = DEFAULT_USED_RANGE,
) -> RotaResult:
    error_status, matches = _lookup_matches(
        lambda rows, tab_title: _find_oncall_summary_matches(rows, tab_title, date_value),
        service=service,
        spreadsheet_id=spreadsheet_id,
        used_range=used_range,
    )
    if error_status:
        return RotaResult(error_status, date_value, "\u00dcgyelet")
    return _summary_result_from_matches(date_value, "\u00dcgyelet", matches)


def lookup_daily_person(
    date_value: date,
    person: str,
    *,
    service=None,
    spreadsheet_id: str | None = None,
    used_range: str = DEFAULT_USED_RANGE,
) -> RotaResult:
    person_label = str(person or "").strip()
    error_status, matches = _lookup_matches(
        lambda rows, tab_title: _find_person_daily_matches(rows, tab_title, date_value, person_label),
        service=service,
        spreadsheet_id=spreadsheet_id,
        used_range=used_range,
    )
    if error_status:
        return RotaResult(error_status, date_value, person_label)
    if not matches:
        return RotaResult(STATUS_NOT_FOUND, date_value, person_label)
    assignment = "\n".join(match.assignment for match in matches)
    return RotaResult(
        STATUS_FOUND,
        date_value,
        person_label,
        assignment=assignment,
        source=matches[0].source,
        matches=tuple(matches),
    )


def _print_cli_result(result: RotaResult) -> int:
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


def _diagnose(argv: list[str]) -> int:
    if len(argv) < 3:
        print('Usage: python rota_lookup.py <date> "<role>"')
        return 2
    try:
        requested_date = parse_date_input(argv[1])
    except ValueError:
        print("Invalid date.")
        return 2

    return _print_cli_result(lookup_oncall(requested_date, " ".join(argv[2:])))


if __name__ == "__main__":
    import sys

    raise SystemExit(_diagnose(sys.argv))
