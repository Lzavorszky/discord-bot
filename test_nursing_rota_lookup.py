import importlib.util
import os
import sys
import types
import unittest
from datetime import date
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

import nursing_rota_lookup


class _Executable:
    def __init__(self, payload):
        self.payload = payload

    def execute(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class _FakeValues:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get(self, **kwargs):
        self.calls.append(kwargs)
        return _Executable({"values": self.rows})


class _FakeSpreadsheets:
    def __init__(self, rows):
        self.values_resource = _FakeValues(rows)

    def values(self):
        return self.values_resource


class _FakeSheetsService:
    def __init__(self, rows):
        self.spreadsheets_resource = _FakeSpreadsheets(rows)

    def spreadsheets(self):
        return self.spreadsheets_resource


def _sample_rows():
    rows = [[""] * 4 for _ in range(38)]
    rows[1][1:4] = ["MA", "HOLNAP", "HOLNAPUTAN"]
    rows[3][1:4] = ["2026.06.05.", "2026.06.06.", "2026.06.07."]
    rows[5][0:4] = ["NAPPAL", "6+0", "7+0", "7+1"]
    rows[6][0:4] = ["M\u0171szakvezet\u0151:", "Day Lead", "Tomorrow Lead", "Third Lead"]
    rows[7][0:4] = ["Beteget visz:", "Day Carrier", "Tomorrow Carrier", "Third Carrier"]
    rows[8][1:4] = ["Day Nurse A", "Tomorrow Nurse A", "Third Nurse A"]
    rows[9][1:4] = ["Day Nurse B", "Tomorrow Nurse B", "Third Nurse B"]
    rows[18][0:4] = ["Seg\u00e9d\u00e1pol\u00f3:", "Day Assistant", "", "Third Assistant"]
    rows[21][0:4] = ["\u00c9JSZAKA", "7+1", "6+0", "5+0"]
    rows[22][0:4] = ["M\u0171szakvezet\u0151:", "Night Lead", "Tomorrow Night Lead", "Third Night Lead"]
    rows[23][0:4] = ["Beteget visz:", "Night Carrier", "Tomorrow Night Carrier", "Third Night Carrier"]
    rows[24][1:4] = ["Night Nurse A", "Tomorrow Night Nurse A", "Third Night Nurse A"]
    rows[34][0:4] = ["Seg\u00e9d\u00e1pol\u00f3:", "Night Assistant", "", ""]
    return rows


class TestNursingRotaLookup(unittest.TestCase):
    def setUp(self):
        self.old_sheet_id = os.environ.get("NURSING_ROTA_SPREADSHEET_ID")
        os.environ["NURSING_ROTA_SPREADSHEET_ID"] = "nursing-sheet"

    def tearDown(self):
        if self.old_sheet_id is None:
            os.environ.pop("NURSING_ROTA_SPREADSHEET_ID", None)
        else:
            os.environ["NURSING_ROTA_SPREADSHEET_ID"] = self.old_sheet_id

    def test_default_lookup_uses_today_column_b(self):
        service = _FakeSheetsService(_sample_rows())

        result = nursing_rota_lookup.lookup_nursing_rota(service=service)

        self.assertEqual(result.status, nursing_rota_lookup.STATUS_FOUND)
        self.assertEqual(result.date_value, date(2026, 6, 5))
        self.assertEqual(result.day.declared_count, "6+0")
        self.assertEqual(result.day.nurses, ("Day Lead", "Day Carrier", "Day Nurse A", "Day Nurse B", "Day Assistant"))
        self.assertEqual(result.night.declared_count, "7+1")
        self.assertEqual(result.night.nurses, ("Night Lead", "Night Carrier", "Night Nurse A", "Night Assistant"))

    def test_tomorrow_lookup_uses_column_c(self):
        service = _FakeSheetsService(_sample_rows())

        result = nursing_rota_lookup.lookup_nursing_rota("tomorrow", service=service)

        self.assertEqual(result.status, nursing_rota_lookup.STATUS_FOUND)
        self.assertEqual(result.date_value, date(2026, 6, 6))
        self.assertEqual(result.day.declared_count, "7+0")
        self.assertEqual(result.day.nurses[:2], ("Tomorrow Lead", "Tomorrow Carrier"))
        self.assertEqual(result.night.nurses[:2], ("Tomorrow Night Lead", "Tomorrow Night Carrier"))

    def test_missing_nursing_sheet_id_returns_configuration_error(self):
        os.environ.pop("NURSING_ROTA_SPREADSHEET_ID", None)

        result = nursing_rota_lookup.lookup_nursing_rota(service=_FakeSheetsService(_sample_rows()))

        self.assertEqual(result.status, nursing_rota_lookup.STATUS_CONFIGURATION_ERROR)


class TestNursingCommandHandler(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_allowed = set(bot.ALLOWED_USER_IDS)

    def tearDown(self):
        bot.ALLOWED_USER_IDS = self.old_allowed

    async def test_apolo_handler_refuses_access_before_lookup(self):
        bot.ALLOWED_USER_IDS = set()
        message = types.SimpleNamespace(
            text="/apolo",
            chat=types.SimpleNamespace(send_action=AsyncMock()),
            reply_text=AsyncMock(),
        )
        update = types.SimpleNamespace(
            message=message,
            effective_user=types.SimpleNamespace(id=123),
            effective_chat=types.SimpleNamespace(id=456),
        )
        context = types.SimpleNamespace(args=[])

        with patch.object(bot, "lookup_nursing_rota") as lookup_mock:
            await bot.handle_apolo(update, context)

        lookup_mock.assert_not_called()
        message.reply_text.assert_awaited()

    async def test_apolo_handler_replies_without_clinical_footer(self):
        bot.ALLOWED_USER_IDS = {123}
        message = types.SimpleNamespace(
            text="/apolo",
            chat=types.SimpleNamespace(send_action=AsyncMock()),
            reply_text=AsyncMock(),
        )
        update = types.SimpleNamespace(
            message=message,
            effective_user=types.SimpleNamespace(id=123),
            effective_chat=types.SimpleNamespace(id=456),
        )
        context = types.SimpleNamespace(args=[])
        result = nursing_rota_lookup.NursingRotaResult(
            nursing_rota_lookup.STATUS_FOUND,
            date(2026, 6, 5),
            "Napi",
            nursing_rota_lookup.NursingShift("Nappal", "2+0", ("Day A", "Day B")),
            nursing_rota_lookup.NursingShift("\u00c9jszaka", "1+0", ("Night A",)),
        )

        with patch.object(bot, "lookup_nursing_rota", return_value=result):
            await bot.handle_apolo(update, context)

        reply = message.reply_text.await_args.args[0]
        self.assertIn("/apolo", bot.ROTA_COMMANDS_TEXT)
        self.assertIn("Day A", reply)
        self.assertIn("Night A", reply)
        self.assertIn("Source: Napi", reply)
        self.assertNotIn(bot.SAFETY_FOOTER, reply)

    async def test_apolo_handler_rejects_direct_date_lookup(self):
        bot.ALLOWED_USER_IDS = {123}
        message = types.SimpleNamespace(
            text="/apolo 2026-06-05",
            chat=types.SimpleNamespace(send_action=AsyncMock()),
            reply_text=AsyncMock(),
        )
        update = types.SimpleNamespace(
            message=message,
            effective_user=types.SimpleNamespace(id=123),
            effective_chat=types.SimpleNamespace(id=456),
        )
        context = types.SimpleNamespace(args=["2026-06-05"])

        with patch.object(bot, "lookup_nursing_rota") as lookup_mock:
            await bot.handle_apolo(update, context)

        lookup_mock.assert_not_called()
        self.assertEqual(message.reply_text.await_args.args[0], bot.APOLO_USAGE)

    async def test_apolo_holnap_calls_tomorrow_lookup(self):
        bot.ALLOWED_USER_IDS = {123}
        message = types.SimpleNamespace(
            text="/apolo holnap",
            chat=types.SimpleNamespace(send_action=AsyncMock()),
            reply_text=AsyncMock(),
        )
        update = types.SimpleNamespace(
            message=message,
            effective_user=types.SimpleNamespace(id=123),
            effective_chat=types.SimpleNamespace(id=456),
        )
        context = types.SimpleNamespace(args=["holnap"])
        result = nursing_rota_lookup.NursingRotaResult(
            nursing_rota_lookup.STATUS_FOUND,
            date(2026, 6, 6),
            "Napi",
            nursing_rota_lookup.NursingShift("Nappal", "1+0", ("Tomorrow Day",)),
            nursing_rota_lookup.NursingShift("\u00c9jszaka", "1+0", ("Tomorrow Night",)),
        )

        with patch.object(bot, "lookup_nursing_rota", return_value=result) as lookup_mock:
            await bot.handle_apolo(update, context)

        lookup_mock.assert_called_once_with("tomorrow")
        self.assertIn("Tomorrow Day", message.reply_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
