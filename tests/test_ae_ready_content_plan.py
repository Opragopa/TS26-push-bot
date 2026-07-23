import types
import unittest
from unittest import mock
import os

import ae_content_plan
import tg_sheet_monitor as monitor


SAMPLE_TSV = """ВРЕМЯ\tАмфитеатр\tУРАЛ 1 (синий) (200 мест)\tУРАЛ 2 (красный) (200 мест)
ДЕНЬ 1  ·  20.07  ·  [ТЕМА: Тест]
10:00–11:00\tГлавная встреча Тема: «Тема открытия» Эксперты: Иванов Иван, директор\tПерерыв\tМастер-класс «Сила языка» Эксперт: Петров Петр, методист
"""


class FakeWorksheet:
    def __init__(self, title):
        self.title = title
        self.cleared = False
        self.values = None

    def clear(self):
        self.cleared = True

    def update(self, values, value_input_option=None):
        self.values = values
        self.value_input_option = value_input_option


class FakeSpreadsheet:
    def __init__(self, spreadsheet_id="ae123"):
        self.id = spreadsheet_id
        self.created_titles = []
        self._worksheets = {}

    def worksheets(self):
        return list(self._worksheets.values())

    def add_worksheet(self, title, rows, cols):
        worksheet = FakeWorksheet(title)
        self._worksheets[title] = worksheet
        self.created_titles.append(title)
        return worksheet


class FakeClient:
    def __init__(self):
        self.spreadsheet = FakeSpreadsheet()
        self.created = []
        self.opened = []

    def create(self, title):
        self.created.append(title)
        return self.spreadsheet

    def open_by_key(self, key):
        self.opened.append(key)
        return self.spreadsheet


class AEReadyContentPlanTests(unittest.TestCase):
    def setUp(self):
        self.args = types.SimpleNamespace(timeout=10)

    def test_parser_builds_ae_compatible_tables(self):
        rows = ae_content_plan.parse_table_rows(SAMPLE_TSV)
        records = ae_content_plan.build_records(rows, corrector=None)

        self.assertGreaterEqual(len(records["legacy_sessions"]), 2)
        first = records["legacy_sessions"][0]
        self.assertEqual(first["ДЕНЬ"], "ДЕНЬ 1")
        self.assertEqual(first["ПЛОЩАДКА"], "Амфитеатр")
        self.assertEqual(first["ТЕМА"], "Тема открытия")
        self.assertEqual(first["ИМЯ_КОМПОЗИЦИИ"], "Амфитеатр/Тема открытия")
        self.assertTrue(records["badges"])

    def test_sync_skips_when_source_hash_unchanged(self):
        state = {monitor.AE_READY_STATE_KEY: {"source_hash": "same", "spreadsheet_id": "ae123"}}
        with mock.patch.object(monitor, "fetch_sheet", return_value={"hash": "same", "cells": [], "rows": 0, "bytes": 0}), mock.patch.object(monitor, "get_google_client") as google:
            result = monitor.run_ae_ready_sync(self.args, state, force=False)

        self.assertFalse(result["changed"])
        google.assert_not_called()

    def test_sync_creates_private_sheet_and_writes_tabs(self):
        client = FakeClient()
        state = {}
        rows = ae_content_plan.parse_table_rows(SAMPLE_TSV)
        current = {"hash": "newhash", "cells": rows, "rows": len(rows), "bytes": len(SAMPLE_TSV)}

        with mock.patch.object(monitor, "fetch_sheet", return_value=current), mock.patch.object(monitor, "get_google_client", return_value=client), mock.patch.object(monitor, "build_ae_llm_corrector", return_value=None):
            result = monitor.run_ae_ready_sync(self.args, state, force=True)

        self.assertTrue(result["changed"])
        self.assertEqual(state[monitor.AE_READY_STATE_KEY]["spreadsheet_id"], "ae123")
        self.assertIn("content_plan_sessions", client.spreadsheet._worksheets)
        self.assertIn("warnings", client.spreadsheet._worksheets)
        sessions_values = client.spreadsheet._worksheets["content_plan_sessions"].values
        self.assertEqual(sessions_values[0], ae_content_plan.LEGACY_SESSION_FIELDS)
        self.assertEqual(client.created, [monitor.AE_READY_SPREADSHEET_TITLE])

    def test_llm_corrector_falls_back_to_groq(self):
        old_values = {key: os.environ.get(key) for key in ("AI_CORRECTION_PROVIDER", "AI_CORRECTION_FALLBACK_PROVIDER", "DEEPSEEK_API_KEY", "GROQ_API_KEY")}
        os.environ["AI_CORRECTION_PROVIDER"] = "deepseek"
        os.environ["AI_CORRECTION_FALLBACK_PROVIDER"] = "groq"
        os.environ.pop("DEEPSEEK_API_KEY", None)
        os.environ["GROQ_API_KEY"] = "groq-test"
        try:
            with mock.patch.object(monitor, "ae_correction_provider_request", side_effect=[monitor.ConfigError("no key"), {"topic": "Тема", "confidence": 0.9}]):
                corrector = monitor.build_ae_llm_corrector(self.args)
                result = corrector({"raw_text": "test"})
        finally:
            for key, value in old_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(result["topic"], "Тема")


if __name__ == "__main__":
    unittest.main()
