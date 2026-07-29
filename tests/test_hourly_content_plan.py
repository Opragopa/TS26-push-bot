import datetime as dt
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

import tg_sheet_monitor as monitor


class HourlyContentPlanTests(unittest.TestCase):
    def setUp(self):
        self.args = types.SimpleNamespace(
            timeout=10,
            no_telegram=False,
            no_macos_notifications=True,
            quiet=True,
            no_admin_buttons=False,
            no_plaque_form=False,
        )
        self.sheet = {
            "label": "Контент-план",
            "url": "https://docs.google.com/spreadsheets/d/test/edit?gid=1",
        }

    def test_empty_hour_marks_boundary_without_sending(self):
        state = {}
        moment = dt.datetime(2026, 7, 22, 15, 0, tzinfo=monitor.CONTENT_PLAN_TIME_ZONE)
        with mock.patch.object(monitor, "send_telegram_chunks_to_chat_ids") as send:
            changed = monitor.flush_content_plan_digest(self.args, [self.sheet], state, moment=moment)
        self.assertTrue(changed)
        self.assertEqual(state[monitor.CONTENT_PLAN_DIGEST_STATE_KEY]["last_flush_hour"], "2026-07-22T15")
        send.assert_not_called()

    def test_time_zone_falls_back_to_netherlands_offset_without_tzdata(self):
        with mock.patch.object(monitor, "ZoneInfo", side_effect=monitor.ZoneInfoNotFoundError("No time zone")):
            timezone = monitor.load_time_zone("Europe/Amsterdam", 1)
        self.assertEqual(timezone.utcoffset(None), dt.timedelta(hours=1))
        self.assertEqual(timezone.tzname(None), "Europe/Amsterdam")

    def test_google_client_falls_back_to_rest_for_oauth_without_gspread(self):
        old_oauth = os.environ.get("GOOGLE_OAUTH_USER_JSON")
        old_service = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
        os.environ["GOOGLE_OAUTH_USER_JSON"] = '{"client_id":"id","client_secret":"secret","refresh_token":"refresh","type":"authorized_user"}'
        os.environ.pop("GOOGLE_SERVICE_ACCOUNT_JSON", None)
        try:
            with mock.patch.dict(sys.modules, {"gspread": None}):
                client = monitor.get_google_client()
        finally:
            if old_oauth is None:
                os.environ.pop("GOOGLE_OAUTH_USER_JSON", None)
            else:
                os.environ["GOOGLE_OAUTH_USER_JSON"] = old_oauth
            if old_service is None:
                os.environ.pop("GOOGLE_SERVICE_ACCOUNT_JSON", None)
            else:
                os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"] = old_service
        self.assertIsInstance(client, monitor.GoogleOAuthRestClient)

    def test_rest_worksheet_batch_update_uses_sheet_title_prefix(self):
        requests = []

        class FakeClient:
            def request(self, method, url, payload=None):
                requests.append((method, url, payload))
                return {}

        spreadsheet = monitor.GoogleRestSpreadsheet(FakeClient(), "spreadsheet-id", metadata={"sheets": []})
        worksheet = monitor.GoogleRestWorksheet(spreadsheet, {"title": "МОУШЕН", "sheetId": 123})
        worksheet.batch_update([{"range": "A280", "values": [["Иванов Иван"]]}])
        self.assertEqual(requests[0][0], "POST")
        self.assertIn("/values:batchUpdate", requests[0][1])
        self.assertEqual(requests[0][2]["data"][0]["range"], "'МОУШЕН'!A280")

    def test_queue_survives_state_save_and_load(self):
        state = {}
        monitor.queue_content_plan_change(state, "Контент-план: тестовый diff.", captured_at="2026-07-22 14:30:00")
        state[monitor.CONTENT_PLAN_DIGEST_STATE_KEY]["last_flush_hour"] = "2026-07-22T14"
        with tempfile.TemporaryDirectory() as directory:
            path = monitor.Path(directory) / "sheet_state.json"
            monitor.save_state(path, state)
            restored = monitor.load_state(path)
        digest = restored[monitor.CONTENT_PLAN_DIGEST_STATE_KEY]
        self.assertEqual(digest["last_flush_hour"], "2026-07-22T14")
        self.assertEqual(digest["events"][0]["diff"], "Контент-план: тестовый diff.")

    def test_flush_sends_queue_and_clears_only_after_delivery(self):
        state = {}
        monitor.queue_content_plan_change(state, "Контент-план: строка «10:00», колонка «Зал» - было «пусто», стало «Открытие».")
        state[monitor.CONTENT_PLAN_DIGEST_STATE_KEY]["last_flush_hour"] = "2026-07-22T14"
        moment = dt.datetime(2026, 7, 22, 15, 0, tzinfo=monitor.CONTENT_PLAN_TIME_ZONE)
        sent = []

        def fake_send(_args, chat_ids, title, message, subtitle="", url=""):
            sent.append((chat_ids, title, message, subtitle, url))
            return 1

        old_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        os.environ["TELEGRAM_CHAT_ID"] = "123"
        try:
            with mock.patch.object(monitor, "build_ai_content_plan_summary", return_value="Добавлено открытие."), mock.patch.object(monitor, "send_telegram_chunks_to_chat_ids", side_effect=fake_send):
                changed = monitor.flush_content_plan_digest(self.args, [self.sheet], state, moment=moment)
        finally:
            if old_chat_id is None:
                os.environ.pop("TELEGRAM_CHAT_ID", None)
            else:
                os.environ["TELEGRAM_CHAT_ID"] = old_chat_id

        self.assertTrue(changed)
        self.assertEqual(state[monitor.CONTENT_PLAN_DIGEST_STATE_KEY]["events"], [])
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0][1], "TS26: AI-сводка за час")
        self.assertEqual(sent[1][1], "TS26: полный diff за час")
        self.assertIn("Добавлено открытие.", sent[0][2])
        self.assertIn("Полный diff", sent[1][2])

    def test_content_plan_recipients_are_admins_and_selected_users(self):
        state = {"_content_plan_chat_ids": ["333"]}
        sheet = {
            "label": "Контент-план",
            "url": "https://docs.google.com/spreadsheets/d/test/edit?gid=1",
            "extra_chat_ids": ["222"],
        }
        old_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        old_admin_ids = os.environ.get("TELEGRAM_ADMIN_CHAT_IDS")
        os.environ["TELEGRAM_CHAT_ID"] = "111"
        os.environ["TELEGRAM_ADMIN_CHAT_IDS"] = "999"
        try:
            recipients = monitor.recipient_chat_ids(sheet, state=state)
        finally:
            if old_chat_id is None:
                os.environ.pop("TELEGRAM_CHAT_ID", None)
            else:
                os.environ["TELEGRAM_CHAT_ID"] = old_chat_id
            if old_admin_ids is None:
                os.environ.pop("TELEGRAM_ADMIN_CHAT_IDS", None)
            else:
                os.environ["TELEGRAM_ADMIN_CHAT_IDS"] = old_admin_ids

        self.assertEqual(recipients, ["999", "222", "333"])

    def test_openai_failure_keeps_full_diff(self):
        state = {}
        original_diff = "Контент-план: строка «10:00», колонка «Зал» - было «пусто», стало «Открытие»."
        monitor.queue_content_plan_change(state, original_diff)
        state[monitor.CONTENT_PLAN_DIGEST_STATE_KEY]["last_flush_hour"] = "2026-07-22T14"
        moment = dt.datetime(2026, 7, 22, 15, 0, tzinfo=monitor.CONTENT_PLAN_TIME_ZONE)
        messages = []

        old_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        os.environ["TELEGRAM_CHAT_ID"] = "123"
        try:
            with mock.patch.object(monitor, "build_ai_content_plan_summary", side_effect=monitor.MonitorError("timeout")), mock.patch.object(monitor, "send_telegram_chunks_to_chat_ids", side_effect=lambda *args, **kwargs: messages.append(args[3]) or 1):
                monitor.flush_content_plan_digest(self.args, [self.sheet], state, moment=moment)
        finally:
            if old_chat_id is None:
                os.environ.pop("TELEGRAM_CHAT_ID", None)
            else:
                os.environ["TELEGRAM_CHAT_ID"] = old_chat_id

        self.assertEqual(len(messages), 2)
        self.assertIn("AI-сводка недоступна", messages[0])
        self.assertIn(original_diff, messages[1])

    def test_summary_send_failure_does_not_block_full_diff(self):
        state = {}
        original_diff = "Контент-план: строка «10:00», колонка «Зал» - было «пусто», стало «Открытие»."
        monitor.queue_content_plan_change(state, original_diff)
        state[monitor.CONTENT_PLAN_DIGEST_STATE_KEY]["last_flush_hour"] = "2026-07-22T14"
        moment = dt.datetime(2026, 7, 22, 15, 0, tzinfo=monitor.CONTENT_PLAN_TIME_ZONE)
        sent = []

        def fake_send(_args, _chat_ids, title, message, subtitle="", url=""):
            if "AI-сводка" in title:
                raise monitor.MonitorError("summary failed")
            sent.append((title, message))
            return 1

        old_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        os.environ["TELEGRAM_CHAT_ID"] = "123"
        try:
            with mock.patch.object(monitor, "build_ai_content_plan_summary", return_value="Добавлено открытие."), mock.patch.object(monitor, "send_telegram_chunks_to_chat_ids", side_effect=fake_send):
                changed = monitor.flush_content_plan_digest(self.args, [self.sheet], state, moment=moment)
        finally:
            if old_chat_id is None:
                os.environ.pop("TELEGRAM_CHAT_ID", None)
            else:
                os.environ["TELEGRAM_CHAT_ID"] = old_chat_id

        self.assertTrue(changed)
        self.assertEqual(state[monitor.CONTENT_PLAN_DIGEST_STATE_KEY]["events"], [])
        self.assertEqual(sent, [("TS26: полный diff за час", "Полный diff\n{}".format(original_diff))])

    def test_groq_provider_uses_llama_default_model(self):
        captured = []

        def fake_groq(payload, _timeout):
            captured.append(payload)
            return "Изменена программа закрытия."

        old_groq_key = os.environ.get("GROQ_API_KEY")
        old_provider = os.environ.get("AI_SUMMARY_PROVIDER")
        os.environ["GROQ_API_KEY"] = "test-key"
        os.environ.pop("AI_SUMMARY_PROVIDER", None)
        try:
            with mock.patch.object(monitor, "groq_chat_completion_text", side_effect=fake_groq):
                summary = monitor.build_ai_content_plan_summary("Контент-план: тестовый diff.", timeout=10)
        finally:
            if old_groq_key is None:
                os.environ.pop("GROQ_API_KEY", None)
            else:
                os.environ["GROQ_API_KEY"] = old_groq_key
            if old_provider is None:
                os.environ.pop("AI_SUMMARY_PROVIDER", None)
            else:
                os.environ["AI_SUMMARY_PROVIDER"] = old_provider

        self.assertEqual(summary, "Изменена программа закрытия.")
        self.assertEqual(captured[0]["model"], "llama-3.3-70b-versatile")
        self.assertEqual(captured[0]["messages"][0]["role"], "system")
        self.assertEqual(captured[0]["messages"][1]["content"], "Контент-план: тестовый diff.")

    def test_hourly_summary_renders_as_telegram_quote(self):
        message = "{}\nКоротко за час\nИзменена программа закрытия.\n{}\n\nПолный diff\nКонтент-план: строка «10:00», колонка «Зал» - было «пусто», стало «Открытие».".format(
            monitor.TELEGRAM_QUOTE_START,
            monitor.TELEGRAM_QUOTE_END,
        )
        rendered = monitor.render_telegram_message("TS26: обновления за час", message, subtitle="Контент-план")
        self.assertIn("<blockquote><b>Коротко за час</b>\nИзменена программа закрытия.</blockquote>", rendered)
        self.assertIn("<b>Полный diff</b>", rendered)
        self.assertNotIn(monitor.TELEGRAM_QUOTE_START, rendered)
        self.assertNotIn(monitor.TELEGRAM_QUOTE_END, rendered)

    def test_plain_service_message_keeps_simple_lines_without_bullets(self):
        rendered = monitor.render_telegram_message(
            "TS26: плашки через бот",
            "Пакетная отправка плашек\nИтог: добавлено 1, обновлено 0.\n\n1. Добавлена: Тестовый тест — тест\nСтрока 288 · Моушен\nhttps://docs.google.com/example",
        )
        self.assertIn("Пакетная отправка плашек\nИтог: добавлено 1, обновлено 0.", rendered)
        self.assertIn("1. Добавлена: Тестовый тест — тест\nСтрока 288 · Моушен", rendered)
        self.assertNotIn("• Пакетная отправка", rendered)
        self.assertNotIn("• Итог:", rendered)

    def test_long_diff_is_chunked_below_telegram_limit(self):
        lines = ["Контент-план: строка «{}», колонка «Зал» - было «пусто», стало «{}».".format(index, "Текст " * 40) for index in range(70)]
        chunks = monitor.telegram_message_chunks("TS26: обновления за час", "\n".join(lines), subtitle="Контент-план")
        self.assertGreater(len(chunks), 1)
        for title, message, subtitle, url in chunks:
            rendered = monitor.render_telegram_message(title, message, subtitle=subtitle, url=url)
            self.assertLessEqual(len(rendered), monitor.MAX_TELEGRAM_MESSAGE_CHARS)
        for marker in ("строка «0»", "строка «35»", "строка «69»"):
            self.assertTrue(any(marker in message for _title, message, _subtitle, _url in chunks))

    def test_content_plan_is_queued_but_recording_plan_stays_immediate(self):
        content_sheet = dict(self.sheet)
        recording_sheet = {"label": "План записи", "url": self.sheet["url"].replace("gid=1", "gid=2")}
        current = {"hash": "new", "rows": 1, "bytes": 1, "cells": [["header"], ["value"]]}
        state = {
            monitor.sheet_key(content_sheet): {"hash": "old", "cells": [["header"], ["old"]]},
            monitor.sheet_key(recording_sheet): {"hash": "old", "cells": [["header"], ["old"]]},
        }
        queued = []
        notified = []
        with mock.patch.object(monitor, "fetch_sheet", return_value=dict(current)), mock.patch.object(monitor, "build_change_summary", return_value="diff"), mock.patch.object(monitor, "queue_content_plan_change", side_effect=lambda *_args, **_kwargs: queued.append(True) or 1), mock.patch.object(monitor, "notify", side_effect=lambda *_args, **_kwargs: notified.append(True)):
            monitor.check_sheet(content_sheet, state, self.args)
            monitor.check_sheet(recording_sheet, state, self.args)
        self.assertEqual(queued, [True])
        self.assertEqual(notified, [True])

    def test_current_day_plate_changes_are_separated(self):
        previous = {
            "cells": [
                ["ДЕНЬ 1 · 27.07", "", ""],
                ["", "10:00", "Иванов Иван"],
                ["ДЕНЬ 2 · 28.07", "", ""],
                ["", "10:00", "Петров Петр"],
            ]
        }
        current = {
            "cells": [
                ["ДЕНЬ 1 · 27.07", "", ""],
                ["", "10:00", "Сидоров Сидор"],
                ["ДЕНЬ 2 · 28.07", "", ""],
                ["", "10:00", "Иванов Иван"],
            ]
        }
        moment = dt.datetime(2026, 7, 27, 12, 0, tzinfo=monitor.CONTENT_PLAN_TIME_ZONE)
        messages, names = monitor.current_day_change_details("План записи", previous, current, moment=moment)
        self.assertEqual(len(messages), 1)
        self.assertIn("Сидоров Сидор", messages[0])
        self.assertEqual(names, ["Иванов Иван", "Сидоров Сидор"])

    def test_yandex_plate_links_use_existing_public_urls(self):
        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

        with mock.patch.dict(os.environ, {"YANDEX_DISK_TOKEN": "token"}), mock.patch.object(
            monitor.urllib.request,
            "urlopen",
            return_value=FakeResponse({"_embedded": {"items": [{"type": "file", "name": "17-30_Иванов Иван.mov", "public_url": "https://disk.yandex.ru/i/test"}]}}),
        ):
            links, errors = monitor.yandex_plate_links(["Иванов Иван"], timeout=5)
        self.assertEqual(links, {"Иванов Иван": "https://disk.yandex.ru/i/test"})
        self.assertEqual(errors, [])

    def test_pending_plate_link_is_sent_after_render_appears(self):
        state = {
            monitor.PENDING_PLATE_LINKS_STATE_KEY: {
                "date": "27.07",
                "names": ["Иванов Иван"],
            }
        }
        sent = []
        sheet = {"label": "План записи", "url": "https://docs.google.com/test"}
        with mock.patch.object(monitor, "yandex_plate_links", return_value=({"Иванов Иван": "https://disk.yandex.ru/i/test"}, [])), mock.patch.object(
            monitor, "notify", side_effect=lambda *_args, **_kwargs: sent.append(True)
        ):
            with mock.patch.object(monitor, "moscow_now", return_value=dt.datetime(2026, 7, 27, tzinfo=monitor.CONTENT_PLAN_TIME_ZONE)):
                changed = monitor.maybe_send_pending_plate_links(self.args, sheet, state)
        self.assertTrue(changed)
        self.assertEqual(sent, [True])
        self.assertNotIn(monitor.PENDING_PLATE_LINKS_STATE_KEY, state)

    def test_parse_plaque_batch_accepts_multiple_rows(self):
        entries = monitor.parse_plaque_batch("Иванов Иван_Должность 1\nДмитриев Дмитрий _ Должность 2")
        self.assertEqual(
            entries,
            [
                {"name": "Иванов Иван", "position": "Должность 1"},
                {"name": "Дмитриев Дмитрий", "position": "Должность 2"},
            ],
        )

    def test_parse_plaque_batch_rejects_multiline_without_separator(self):
        with self.assertRaises(monitor.ConfigError):
            monitor.parse_plaque_batch("Иванов Иван_Должность 1\nДмитриев Дмитрий Должность 2")

    def test_confirm_batch_hides_sheet_links_from_user(self):
        state = {
            "_plaque_sessions": {
                "555": {
                    "entries": [
                        {"name": "Иванов Иван", "position": "Должность 1"},
                        {"name": "Дмитриев Дмитрий", "position": "Должность 2"},
                    ]
                }
            }
        }
        fake_results = [
            {"action": "created", "worksheet_title": "Моушен", "worksheet_gid": 1399617264, "row": 280, "url": "https://docs.google.com/row280"},
            {"action": "updated", "worksheet_title": "Моушен", "worksheet_gid": 1399617264, "row": 281, "url": "https://docs.google.com/row281"},
        ]
        sent = []

        def fake_send(_args, chat_id, title, message, reply_markup=None):
            sent.append((str(chat_id), title, message, reply_markup))

        old_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        os.environ["TELEGRAM_CHAT_ID"] = "999"
        try:
            with mock.patch.object(monitor, "write_plaque_to_sheet", side_effect=fake_results), mock.patch.object(monitor, "send_plain_chat_message", side_effect=fake_send):
                monitor.confirm_plaque(self.args, state, "555")
        finally:
            if old_chat_id is None:
                os.environ.pop("TELEGRAM_CHAT_ID", None)
            else:
                os.environ["TELEGRAM_CHAT_ID"] = old_chat_id

        user_message = next(item for item in sent if item[0] == "555")[2]
        admin_message = next(item for item in sent if item[0] == "999")[2]
        self.assertNotIn("https://docs.google.com", user_message)
        self.assertIn("https://docs.google.com/row280", admin_message)
        self.assertIn("Итог: добавлено 1, обновлено 1.", admin_message)
        self.assertIn("1. Добавлена: Иванов Иван — Должность 1", admin_message)
        self.assertIn("Строка 280 · Моушен", admin_message)
        self.assertNotIn("Лист: Моушен (gid=1399617264)", admin_message)
        self.assertNotIn("ФИО:", admin_message)
        self.assertEqual(state["_plaque_sessions"], {})

    def test_plaque_access_is_limited_to_admins_user_mode_and_allowlist(self):
        state = {"_plaque_chat_ids": ["555"]}
        sheets = [self.sheet]
        old_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        old_admin_ids = os.environ.get("TELEGRAM_ADMIN_CHAT_IDS")
        os.environ["TELEGRAM_CHAT_ID"] = "111"
        os.environ["TELEGRAM_ADMIN_CHAT_IDS"] = "999"
        try:
            self.assertTrue(monitor.can_use_plaque_form(sheets, state, "999"))
            self.assertTrue(monitor.can_use_plaque_form(sheets, state, "555"))
            self.assertFalse(monitor.can_use_plaque_form(sheets, state, "777"))
            monitor.set_user_mode_chat(state, "777", True)
            self.assertTrue(monitor.can_use_plaque_form(sheets, state, "777"))
        finally:
            if old_chat_id is None:
                os.environ.pop("TELEGRAM_CHAT_ID", None)
            else:
                os.environ["TELEGRAM_CHAT_ID"] = old_chat_id
            if old_admin_ids is None:
                os.environ.pop("TELEGRAM_ADMIN_CHAT_IDS", None)
            else:
                os.environ["TELEGRAM_ADMIN_CHAT_IDS"] = old_admin_ids

    def test_start_screen_hides_plaque_button_without_access(self):
        sent = []

        def fake_send(_args, chat_id, title, message, reply_markup=None):
            sent.append((chat_id, title, message, reply_markup))

        with mock.patch.object(monitor, "send_plain_chat_message", side_effect=fake_send):
            monitor.send_start_screen(self.args, "777", state={}, is_content_recipient=False, can_use_plaque=False)

        self.assertEqual(sent[0][1], "TS26: старт")
        self.assertIn("Если вам нужен доступ к плашкам", sent[0][2])
        self.assertIsNone(sent[0][3])

    def test_start_screen_shows_user_actions_with_plaque_access(self):
        sent = []

        def fake_send(_args, chat_id, title, message, reply_markup=None):
            sent.append((chat_id, title, message, reply_markup))

        with mock.patch.object(monitor, "send_plain_chat_message", side_effect=fake_send):
            monitor.send_start_screen(self.args, "555", state={}, is_content_recipient=True, can_use_plaque=True)

        self.assertEqual(sent[0][1], "TS26: старт")
        self.assertIn("почасовые сводки", sent[0][2])
        self.assertIn("пакетная отправка", sent[0][2])
        keyboard = sent[0][3]["keyboard"]
        self.assertEqual(keyboard[0][0]["text"], monitor.PLAQUE_ADD_BUTTON_TEXT)
        self.assertEqual(keyboard[1][0]["text"], monitor.HELP_BUTTON_TEXT)

    def test_admin_panel_has_clear_sections(self):
        keyboard = monitor.admin_keyboard()
        flattened = [button["text"] for row in keyboard["inline_keyboard"] for button in row]

        self.assertIn("Мониторинг", flattened)
        self.assertIn("Доступы", flattened)
        self.assertIn("AE-ready", flattened)
        self.assertIn("Пользовательский вид", flattened)

    def test_admin_callback_opens_access_section(self):
        sent = []
        state = {}
        callback = {
            "id": "cb1",
            "data": "dbg:menu:access",
            "message": {"chat": {"id": "999"}},
        }
        old_admin_ids = os.environ.get("TELEGRAM_ADMIN_CHAT_IDS")
        os.environ["TELEGRAM_ADMIN_CHAT_IDS"] = "999"
        try:
            with mock.patch.object(monitor, "answer_callback"), mock.patch.object(monitor, "send_admin_message", side_effect=lambda _args, chat_id, title, message, reply_markup=None: sent.append((chat_id, title, message, reply_markup))):
                handled = monitor.handle_admin_callback(self.args, [self.sheet], state, callback)
        finally:
            if old_admin_ids is None:
                os.environ.pop("TELEGRAM_ADMIN_CHAT_IDS", None)
            else:
                os.environ["TELEGRAM_ADMIN_CHAT_IDS"] = old_admin_ids

        self.assertTrue(handled)
        self.assertEqual(sent[0][1], "TS26: доступы")
        self.assertIn("Контент-план", sent[0][2])
        buttons = [button["text"] for row in sent[0][3]["inline_keyboard"] for button in row]
        self.assertIn("Назад", buttons)

    def test_configure_bot_commands_sets_default_and_admin_scopes(self):
        calls = []
        old_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        old_admin_ids = os.environ.get("TELEGRAM_ADMIN_CHAT_IDS")
        os.environ["TELEGRAM_BOT_TOKEN"] = "token"
        os.environ["TELEGRAM_ADMIN_CHAT_IDS"] = "999"
        try:
            with mock.patch.object(monitor, "telegram_request", side_effect=lambda token, method, payload, timeout: calls.append((method, payload)) or {"ok": True}):
                monitor.configure_bot_commands(self.args)
        finally:
            if old_token is None:
                os.environ.pop("TELEGRAM_BOT_TOKEN", None)
            else:
                os.environ["TELEGRAM_BOT_TOKEN"] = old_token
            if old_admin_ids is None:
                os.environ.pop("TELEGRAM_ADMIN_CHAT_IDS", None)
            else:
                os.environ["TELEGRAM_ADMIN_CHAT_IDS"] = old_admin_ids

        self.assertEqual([item[0] for item in calls], ["setMyCommands", "setMyCommands"])
        default_commands = monitor.json.loads(calls[0][1]["commands"])
        admin_commands = monitor.json.loads(calls[1][1]["commands"])
        self.assertEqual(default_commands, [{"command": "start", "description": "Открыть бот"}])
        self.assertIn({"command": "ae_sync", "description": "Обновить AE-ready"}, admin_commands)
        self.assertEqual(monitor.json.loads(calls[1][1]["scope"]), {"type": "chat", "chat_id": "999"})

    def test_non_admin_slash_add_is_not_a_user_command(self):
        sent = []
        state = {"_plaque_chat_ids": ["555"]}
        message = {"chat": {"id": "555"}, "text": "/add"}

        def fake_send(_args, chat_id, title, message, reply_markup=None):
            sent.append((chat_id, title, message, reply_markup))

        with mock.patch.object(monitor, "send_plain_chat_message", side_effect=fake_send):
            handled = monitor.handle_plaque_message(self.args, [self.sheet], state, message)

        self.assertTrue(handled)
        self.assertEqual(sent[0][1], "TS26: команда не нужна")
        self.assertNotIn("_plaque_sessions", state)

    def test_plaque_access_report_marks_added_users(self):
        state = {
            "_plaque_chat_ids": ["555"],
            "_known_chats": {
                "555": {"title": "Иван Иванов", "username": "ivan", "type": "private", "seen_at": "2026-07-25 10:00:00"},
                "777": {"title": "Петр Петров", "username": "", "type": "private", "seen_at": "2026-07-25 09:00:00"},
            },
        }
        report = monitor.plaque_access_report(state)
        self.assertIn("Добавленные chat_id: 555", report)
        self.assertIn("555 - Иван Иванов (@ivan) - доступ есть", report)
        self.assertIn("777 - Петр Петров - нет доступа", report)


if __name__ == "__main__":
    unittest.main()
