import importlib.util
import os
import sys
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

if importlib.util.find_spec("openai") is None:
    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = MagicMock
    sys.modules["openai"] = fake_openai

if importlib.util.find_spec("telegram") is None:
    fake_telegram = types.ModuleType("telegram")
    fake_telegram.Update = type("Update", (), {})
    fake_ext = types.ModuleType("telegram.ext")
    fake_ext.ApplicationBuilder = MagicMock
    fake_ext.MessageHandler = MagicMock
    fake_ext.CommandHandler = MagicMock
    fake_ext.ContextTypes = type("ContextTypes", (), {"DEFAULT_TYPE": object})
    fake_ext.filters = type("filters", (), {"TEXT": MagicMock(), "COMMAND": MagicMock()})
    sys.modules["telegram"] = fake_telegram
    sys.modules["telegram.ext"] = fake_ext

_openai_patch = patch("openai.OpenAI", return_value=MagicMock())
_openai_patch.start()
import telegram_bot as bot
_openai_patch.stop()

import rota_lookup


class _Executable:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class _FakeValues:
    def __init__(self, tab_values):
        self.tab_values = list(tab_values)
        self.calls = []

    def get(self, **kwargs):
        self.calls.append(kwargs)
        return _Executable({"values": self.tab_values.pop(0)})


class _FakeSpreadsheets:
    def __init__(self, tabs):
        self.tabs = tabs
        self.values_resource = _FakeValues([rows for _, rows in tabs])

    def get(self, **kwargs):
        return _Executable(
            {
                "sheets": [
                    {"properties": {"title": title}}
                    for title, _rows in self.tabs
                ]
            }
        )

    def values(self):
        return self.values_resource


class _FakeSheetsService:
    def __init__(self, tabs):
        self.spreadsheets_resource = _FakeSpreadsheets(tabs)

    def spreadsheets(self):
        return self.spreadsheets_resource


class TestRotaLookup(unittest.TestCase):
    def setUp(self):
        self.old_sheet_id = os.environ.get("ROTA_SPREADSHEET_ID")
        self.old_google_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        os.environ["ROTA_SPREADSHEET_ID"] = "test-sheet"

    def tearDown(self):
        if self.old_sheet_id is None:
            os.environ.pop("ROTA_SPREADSHEET_ID", None)
        else:
            os.environ["ROTA_SPREADSHEET_ID"] = self.old_sheet_id
        if self.old_google_json is None:
            os.environ.pop("GOOGLE_SERVICE_ACCOUNT_JSON", None)
        else:
            os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = self.old_google_json

    def test_relative_dates_use_budapest_reference_date(self):
        reference = datetime(2026, 6, 3, 23, 30, tzinfo=timezone(timedelta(hours=2)))

        self.assertEqual(rota_lookup.parse_date_input("today", reference), date(2026, 6, 3))
        self.assertEqual(rota_lookup.parse_date_input("ma", reference), date(2026, 6, 3))
        self.assertEqual(rota_lookup.parse_date_input("tomorrow", reference), date(2026, 6, 4))
        self.assertEqual(rota_lookup.parse_date_input("holnap", reference), date(2026, 6, 4))

    def test_absolute_date_formats_parse(self):
        self.assertEqual(rota_lookup.parse_date_input("2026-06-05"), date(2026, 6, 5))
        self.assertEqual(rota_lookup.parse_date_input("2026.06.05"), date(2026, 6, 5))
        self.assertEqual(rota_lookup.parse_date_input("2026.06. 05"), date(2026, 6, 5))

    def test_role_aliases_normalize(self):
        cases = {
            "eges": "\u00c9g\u00e9s",
            "\u00e9g\u00e9s": "\u00c9g\u00e9s",
            "ugyeletvezeto": "\u00dcgyeletvezet\u0151",
            "\u00fcgyeletvezet\u0151": "\u00dcgyeletvezet\u0151",
            "aneszt": "Aneszt \u00fcgyelet",
            "aneszt ugyelet": "Aneszt \u00fcgyelet",
            "szivseb ugyelet": "Sz\u00edvseb \u00fcgyelet",
            "surgos ugyelet": "S\u00fcrg\u0151s \u00fcgyelet",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(rota_lookup.normalize_role(raw), expected)

    def test_simple_weekly_grid_returns_eges_assignment(self):
        service = _FakeSheetsService(
            [
                (
                    "Beoszt\u00e1s",
                    [
                        ["", "2026-06-05", "2026-06-06"],
                        ["\u00dcgyeletvezet\u0151", "LEAD", "NEXT"],
                        ["\u00c9g\u00e9s", "ABC", "DEF"],
                    ],
                )
            ]
        )

        result = rota_lookup.lookup_rota(date(2026, 6, 5), "eges", service=service)

        self.assertEqual(result.status, rota_lookup.STATUS_FOUND)
        self.assertEqual(result.assignment, "ABC")
        self.assertEqual(result.source, "Beoszt\u00e1s")

    def test_ugyeletvezeto_query_returns_assignment(self):
        service = _FakeSheetsService(
            [
                (
                    "Beoszt\u00e1s",
                    [
                        ["", "2026-06-05 00:00:00"],
                        ["\u00dcgyeletvezet\u0151", "LEAD"],
                    ],
                )
            ]
        )

        result = rota_lookup.lookup_rota(date(2026, 6, 5), "ugyeletvezeto", service=service)

        self.assertEqual(result.status, rota_lookup.STATUS_FOUND)
        self.assertEqual(result.assignment, "LEAD")

    def test_numeric_prefixed_eges_row_returns_assignment(self):
        service = _FakeSheetsService(
            [
                (
                    "2026",
                    [
                        ["", "23. h\u00e9t", "2026.06.05"],
                        ["", "Nappali munka", "p\u00e9ntek"],
                        ["", "14 \u00c9g\u00e9s", "IVE"],
                    ],
                )
            ]
        )

        result = rota_lookup.lookup_daily_rota(date(2026, 6, 5), "\u00e9g\u00e9s", service=service)

        self.assertEqual(result.status, rota_lookup.STATUS_FOUND)
        self.assertEqual(result.assignment, "IVE")

    def test_napi_vezeto_matches_ugyeletvezeto_alias(self):
        service = _FakeSheetsService(
            [
                (
                    "2026",
                    [
                        ["", "23. h\u00e9t", "2026.06.05"],
                        ["", "Nappali munka", "p\u00e9ntek"],
                        ["", "Napi vezet\u0151", "LEAD"],
                    ],
                )
            ]
        )

        result = rota_lookup.lookup_daily_rota(date(2026, 6, 5), "ugyeletvezeto", service=service)

        self.assertEqual(result.status, rota_lookup.STATUS_FOUND)
        self.assertEqual(result.assignment, "LEAD")

    def test_multi_row_aneszt_ugyelet_combines_assignments(self):
        service = _FakeSheetsService(
            [
                (
                    "2026",
                    [
                        ["", "23. h\u00e9t", "2026.06.05"],
                        ["", "\u00dcgyeletek", ""],
                        ["", "Aneszt \u00fcgyelet 1", "VBA"],
                        ["", "Aneszt \u00fcgyelet 2", "KKA"],
                        ["", "Aneszt \u00fcgyelet 3", ""],
                    ],
                )
            ]
        )

        result = rota_lookup.lookup_oncall(date(2026, 6, 5), "aneszt ugyelet", service=service)

        self.assertEqual(result.status, rota_lookup.STATUS_FOUND)
        self.assertEqual(result.assignment, "Aneszt \u00fcgyelet 1: VBA\nAneszt \u00fcgyelet 2: KKA")

    def test_oncall_uses_only_ugyeletek_section(self):
        service = _FakeSheetsService(
            [
                (
                    "2026",
                    [
                        ["", "23. h\u00e9t", "2026.06.05"],
                        ["", "Nappali munka", "p\u00e9ntek"],
                        ["", "S\u00fcrg\u0151s", "DAY"],
                        ["", "Hossz\u00fa", ""],
                        ["", "Aneszt hossz\u00fa 1", "LONG"],
                        ["", "\u00dcgyeletek", ""],
                        ["", "S\u00fcrg\u0151s", "CALL"],
                        ["", "T\u00e1voll\u00e9tek", ""],
                    ],
                )
            ]
        )

        result = rota_lookup.lookup_oncall(date(2026, 6, 5), "surgos", service=service)

        self.assertEqual(result.status, rota_lookup.STATUS_FOUND)
        self.assertEqual(result.assignment, "CALL")

    def test_daily_rota_uses_only_nappali_section(self):
        service = _FakeSheetsService(
            [
                (
                    "2026",
                    [
                        ["", "23. h\u00e9t", "2026.06.05"],
                        ["", "Nappali munka", "p\u00e9ntek"],
                        ["", "S\u00fcrg\u0151s", "DAY"],
                        ["", "\u00dcgyeletek", ""],
                        ["", "S\u00fcrg\u0151s", "CALL"],
                    ],
                )
            ]
        )

        result = rota_lookup.lookup_daily_rota(date(2026, 6, 5), "surgos", service=service)

        self.assertEqual(result.status, rota_lookup.STATUS_FOUND)
        self.assertEqual(result.assignment, "DAY")

    def test_long_rota_uses_hosszu_section(self):
        service = _FakeSheetsService(
            [
                (
                    "2026",
                    [
                        ["", "23. h\u00e9t", "2026.06.05"],
                        ["", "Nappali munka", "p\u00e9ntek"],
                        ["", "Aneszt1", "DAY"],
                        ["", "Hossz\u00fa", ""],
                        ["", "Aneszt hossz\u00fa 1", "LONG1"],
                        ["", "Aneszt hossz\u00fa 2", "LONG2"],
                        ["", "\u00dcgyeletek", ""],
                        ["", "Aneszt \u00fcgyelet 1", "CALL"],
                    ],
                )
            ]
        )

        result = rota_lookup.lookup_long_rota(date(2026, 6, 5), "aneszt", service=service)

        self.assertEqual(result.status, rota_lookup.STATUS_FOUND)
        self.assertEqual(result.assignment, "Aneszt hossz\u00fa 1: LONG1\nAneszt hossz\u00fa 2: LONG2")

    def test_holvagyok_finds_person_in_daily_section_only(self):
        service = _FakeSheetsService(
            [
                (
                    "2026",
                    [
                        ["", "23. h\u00e9t", "2026.06.05"],
                        ["", "Nappali munka", "p\u00e9ntek"],
                        ["", "14 \u00c9g\u00e9s", "IVE"],
                        ["", "S\u00fcrg\u0151s", "SAD, HAM"],
                        ["", "\u00dcgyeletek", ""],
                        ["", "Aneszt1", "IVE"],
                    ],
                )
            ]
        )

        result = rota_lookup.lookup_daily_person(date(2026, 6, 5), "IVE", service=service)

        self.assertEqual(result.status, rota_lookup.STATUS_FOUND)
        self.assertEqual(result.assignment, "14 \u00c9g\u00e9s: IVE")

    def test_napirota_summary_returns_daily_label_lines(self):
        service = _FakeSheetsService(
            [
                (
                    "2026",
                    [
                        ["", "23. h\u00e9t", "2026.06.05"],
                        ["", "Nappali munka", "p\u00e9ntek"],
                        ["", "14 \u00c9g\u00e9s", "IVE"],
                        ["", "S\u00fcrg\u0151s", "SAD, HAM"],
                        ["", "Hossz\u00fa", ""],
                        ["", "Aneszt hossz\u00fa 1", "LONG"],
                    ],
                )
            ]
        )

        result = rota_lookup.lookup_daily_summary(date(2026, 6, 5), service=service)

        self.assertEqual(result.status, rota_lookup.STATUS_FOUND)
        self.assertEqual(result.assignment, "14 \u00c9g\u00e9s: IVE\nS\u00fcrg\u0151s: SAD, HAM")

    def test_napirota_summary_merges_duplicate_date_blocks(self):
        service = _FakeSheetsService(
            [
                (
                    "2026",
                    [
                        ["", "23. h\u00e9t", "2026.06.04"],
                        ["", "Nappali munka", "cs\u00fct\u00f6rt\u00f6k"],
                        ["", "14 \u00c9g\u00e9s", "ZLR"],
                        ["", "24. h\u00e9t", "2026.06.04"],
                        ["", "Nappali munka", "cs\u00fct\u00f6rt\u00f6k"],
                        ["", "Aneszt1", "KUA"],
                    ],
                )
            ]
        )

        result = rota_lookup.lookup_daily_summary(date(2026, 6, 4), service=service)

        self.assertEqual(result.status, rota_lookup.STATUS_FOUND)
        self.assertEqual(result.assignment, "14 \u00c9g\u00e9s: ZLR\nAneszt1: KUA")

    def test_hosszu_summary_returns_unique_staff_list(self):
        service = _FakeSheetsService(
            [
                (
                    "2026",
                    [
                        ["", "23. h\u00e9t", "2026.06.05"],
                        ["", "Nappali munka", "p\u00e9ntek"],
                        ["", "14 \u00c9g\u00e9s", "DAY"],
                        ["", "Hossz\u00fa", ""],
                        ["", "Aneszt hossz\u00fa 1", "ZLR, ZKA"],
                        ["", "Sz\u00edvseb hossz\u00fa", "PAT, ZLR"],
                        ["", "\u00dcgyeletek", ""],
                    ],
                )
            ]
        )

        result = rota_lookup.lookup_long_summary(date(2026, 6, 5), service=service)

        self.assertEqual(result.status, rota_lookup.STATUS_FOUND)
        self.assertEqual(result.assignment, "ZLR, ZKA, PAT")

    def test_hosszu_summary_merges_duplicate_date_blocks(self):
        service = _FakeSheetsService(
            [
                (
                    "2026",
                    [
                        ["", "23. h\u00e9t", "2026.06.04"],
                        ["", "Hossz\u00fa", ""],
                        ["", "Aneszt hossz\u00fa 1", "ZLR, ZKA"],
                        ["", "24. h\u00e9t", "2026.06.04"],
                        ["", "Hossz\u00fa", ""],
                        ["", "Sz\u00edvseb hossz\u00fa", "PAT, ZLR"],
                    ],
                )
            ]
        )

        result = rota_lookup.lookup_long_summary(date(2026, 6, 4), service=service)

        self.assertEqual(result.status, rota_lookup.STATUS_FOUND)
        self.assertEqual(result.assignment, "ZLR, ZKA, PAT")

    def test_ugyelet_summary_returns_grouped_lines(self):
        service = _FakeSheetsService(
            [
                (
                    "2026",
                    [
                        ["", "23. h\u00e9t", "2026.06.05"],
                        ["", "\u00dcgyeletek", ""],
                        ["", "Multi ITO1", "ZKA"],
                        ["", "Multi ITO2", "MPE"],
                        ["", "S\u00fcrg\u0151s", "KFM"],
                        ["", "Sz\u00edvseb ITO", "SAD"],
                        ["", "Aneszt1", "KUA"],
                        ["", "Aneszt2", "OTM"],
                        ["", "Aneszt3", "TPE"],
                        ["", "Szub", "SUB"],
                        ["", "II. th.", "II"],
                        ["", "Sz\u00edv.telefon", "TEL1"],
                        ["", "TEL", "TEL2"],
                    ],
                )
            ]
        )

        result = rota_lookup.lookup_oncall_summary(date(2026, 6, 5), service=service)

        self.assertEqual(result.status, rota_lookup.STATUS_FOUND)
        self.assertEqual(
            result.assignment,
            "ITO: ZKA, MPE, KFM\n"
            "Sz\u00edv: SAD\n"
            "M\u0171t\u0151: KUA, OTM\n"
            "Telefon: TPE, SUB, II, TEL1, TEL2",
        )

    def test_rotahely_returns_matching_role_locations(self):
        service = _FakeSheetsService(
            [
                (
                    "2026",
                    [
                        ["", "23. h\u00e9t", "2026.06.05"],
                        ["", "Nappali munka", "p\u00e9ntek"],
                        ["", "S\u00fcrg\u0151s", "DAY"],
                        ["", "\u00dcgyeletek", ""],
                        ["", "S\u00fcrg\u0151s", "CALL"],
                    ],
                )
            ]
        )

        result = rota_lookup.lookup_role_locations(date(2026, 6, 5), "surgos", service=service)

        self.assertEqual(result.status, rota_lookup.STATUS_FOUND)
        self.assertEqual(result.assignment, "Napi - S\u00fcrg\u0151s: DAY\n\u00dcgyelet - S\u00fcrg\u0151s: CALL")

    def test_empty_cell_returns_not_found(self):
        service = _FakeSheetsService(
            [
                (
                    "Beoszt\u00e1s",
                    [
                        ["", "2026.06. 05"],
                        ["S\u00fcrg\u0151s \u00fcgyelet", ""],
                    ],
                )
            ]
        )

        result = rota_lookup.lookup_oncall(date(2026, 6, 5), "surgos ugyelet", service=service)

        self.assertEqual(result.status, rota_lookup.STATUS_NOT_FOUND)

    def test_conflicting_matches_across_tabs_return_multiple_matches(self):
        service = _FakeSheetsService(
            [
                ("Week A", [["", "2026-06-05"], ["\u00c9g\u00e9s", "ABC"]]),
                ("Week B", [["", "2026-06-05"], ["\u00c9g\u00e9s", "XYZ"]]),
            ]
        )

        result = rota_lookup.lookup_rota(date(2026, 6, 5), "eges", service=service)

        self.assertEqual(result.status, rota_lookup.STATUS_MULTIPLE_MATCHES)

    def test_no_credentials_configured_returns_configuration_error(self):
        os.environ.pop("GOOGLE_SERVICE_ACCOUNT_JSON", None)

        result = rota_lookup.lookup_oncall(date(2026, 6, 5), "eges")

        self.assertEqual(result.status, rota_lookup.STATUS_CONFIGURATION_ERROR)

    def test_safe_configuration_diagnostics_do_not_include_secret_values(self):
        os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = "not-json-secret-value"

        problems = rota_lookup.safe_configuration_diagnostics()

        self.assertIn("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON", problems)
        self.assertNotIn("not-json-secret-value", "\n".join(problems))


class TestRotaCommandHandler(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_allowed = set(bot.ALLOWED_USER_IDS)

    def tearDown(self):
        bot.ALLOWED_USER_IDS = self.old_allowed

    async def test_rota_handler_refuses_access_when_allowlist_is_empty(self):
        bot.ALLOWED_USER_IDS = set()
        message = types.SimpleNamespace(
            text="/rotahely today eges",
            chat=types.SimpleNamespace(send_action=AsyncMock()),
            reply_text=AsyncMock(),
        )
        update = types.SimpleNamespace(
            message=message,
            effective_user=types.SimpleNamespace(id=123),
            effective_chat=types.SimpleNamespace(id=456),
        )
        context = types.SimpleNamespace(args=["today", "eges"])

        with patch.object(bot, "lookup_role_locations") as lookup_mock:
            await bot.handle_rotahely(update, context)

        lookup_mock.assert_not_called()
        message.reply_text.assert_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
