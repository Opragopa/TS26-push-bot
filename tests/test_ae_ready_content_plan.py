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

    def test_session_comp_name_shortens_plenary_amphitheater(self):
        self.assertEqual(
            ae_content_plan.session_comp_name("АМФИТЕАТР ОСНОВНАЯ / ПЛЕНАРНАЯ", "Выборы 2026"),
            "АМФИТЕАТР/Выборы 2026",
        )

    def test_parser_keeps_name_and_position_in_one_badge(self):
        sample_tsv = """ВРЕМЯ\tАмфитеатр\tУРАЛ 1 (синий) (200 мест)\tУРАЛ 2 (красный) (200 мест)
ДЕНЬ 1  ·  20.07  ·  [ТЕМА: Тест]
17:30-19:00\tСпикер: Памфилова Элла Александровна, Председатель Центральной избирательной комиссии Российской Федерации\t\t
"""
        rows = ae_content_plan.parse_table_rows(sample_tsv)
        records = ae_content_plan.build_records(rows, corrector=None)

        self.assertEqual(len(records["badges"]), 1)
        self.assertEqual(records["badges"][0]["ФИО спикера"], "Памфилова Элла Александровна")
        self.assertIn("Председатель Центральной избирательной комиссии Российской Федерации", records["badges"][0]["Должность"])

    def test_parser_shortens_topic_and_keeps_concise_description(self):
        rows = [
            ["ВРЕМЯ", "Амфитеатр", "УРАЛ 1 (синий) (200 мест)", "УРАЛ 2 (красный) (200 мест)"],
            ["ДЕНЬ 1  ·  20.07  ·  [ТЕМА: Тест]"],
            [
                "16:45-17:30",
                "ДЕЛОВОЕ открытие смены\nУстановочная встреча ТС-2026. Установка по деловой игре от Института социальной архитектуры.\nСпикеры:\n1) Литвиненко Егор Васильевич, Заместитель руководителя, Федеральное агентство по делам молодежи (Росмолодежь)",
                "",
                "",
            ],
        ]
        records = ae_content_plan.build_records(rows, corrector=None)

        session = records["legacy_sessions"][0]
        self.assertEqual(session["ТЕМА"], "ДЕЛОВОЕ открытие смены")
        self.assertEqual(session["ОПИСАНИЕ"], "Установочная встреча ТС-2026")

    def test_parser_drops_description_when_it_only_repeats_title(self):
        topic, description = ae_content_plan.normalize_topic_description(
            "Образ будущего: Россия-2036",
            'Встреча "Образ будущего: Россия-2036".',
        )
        self.assertEqual(topic, "Образ будущего: Россия-2036")
        self.assertEqual(description, "")

    def test_parser_keeps_main_meeting_day_label(self):
        topic, description = ae_content_plan.normalize_topic_description(
            "Платформа суверенного развития",
            'Главная встреча дня "Платформа суверенного развития".',
        )
        self.assertEqual(topic, "Платформа суверенного развития")
        self.assertEqual(description, "Главная встреча дня")

    def test_parser_splits_numbered_speakers_with_positions(self):
        sample_tsv = """ВРЕМЯ\tАмфитеатр\tУРАЛ 1 (синий) (200 мест)\tУРАЛ 2 (красный) (200 мест)
ДЕНЬ 1  ·  20.07  ·  [ТЕМА: Тест]
16:45-17:30\tСпикеры: 1) Иванов Иван Иванович, Заместитель руководителя Росмолодежи 2) Петров Петр Петрович, программный директор форума\t\t
"""
        rows = ae_content_plan.parse_table_rows(sample_tsv)
        records = ae_content_plan.build_records(rows, corrector=None)

        self.assertEqual(len(records["badges"]), 2)
        self.assertEqual(records["badges"][0]["ФИО спикера"], "Иванов Иван Иванович")
        self.assertEqual(records["badges"][1]["ФИО спикера"], "Петров Петр Петрович")
        self.assertEqual(records["badges"][0]["Должность"], "Заместитель руководителя Росмолодежи")
        self.assertEqual(records["badges"][1]["Должность"], "программный директор форума")

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

    def test_state_source_url_overrides_env_default(self):
        state = {monitor.AE_READY_STATE_KEY: {"source_url": "https://docs.google.com/spreadsheets/d/custom/edit?gid=1"}}
        self.assertEqual(monitor.ae_ready_source_url(state), "https://docs.google.com/spreadsheets/d/custom/edit?gid=1")

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

    def test_needs_llm_correction_for_long_or_numbered_cells(self):
        self.assertTrue(
            ae_content_plan.needs_llm_correction(
                "Главная встреча дня 1) Иванов Иван Иванович, модератор 2) Петров Петр Петрович, эксперт",
                "Очень длинная тема, которая выглядит скорее как целый абзац, а не как краткий заголовок мероприятия",
                "Очень длинное описание, которое тоже не должно без LLM попадать в итоговую таблицу как есть",
                [{"name": "Иванов Иван Иванович"}],
            )
        )

    def test_extract_json_object_text_recovers_json_from_wrapper(self):
        text = 'Вот ответ\n```json\n{"topic":"Тема","confidence":0.9}\n```\nспасибо'
        self.assertEqual(monitor.extract_json_object_text(text), '{"topic":"Тема","confidence":0.9}')

    def test_extract_chat_message_text_reads_openai_style_array(self):
        content = [
            {"type": "text", "text": '{"topic":"Тема"}'},
            {"type": "ignored", "value": "unused"},
        ]
        self.assertEqual(monitor.extract_chat_message_text(content), '{"topic":"Тема"}\nunused')

    def test_ae_correction_retries_after_invalid_json(self):
        old_values = {key: os.environ.get(key) for key in ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL")}
        os.environ["DEEPSEEK_API_KEY"] = "deepseek-test"
        try:
            responses = ['{"topic":"broken', '{"topic":"Тема","confidence":0.9}']
            with mock.patch.object(monitor, "chat_completion_text", side_effect=responses):
                result = monitor.ae_correction_provider_request("deepseek", {"raw_text": "test"}, timeout=10)
        finally:
            for key, value in old_values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.assertEqual(result["topic"], "Тема")


if __name__ == "__main__":
    unittest.main()
