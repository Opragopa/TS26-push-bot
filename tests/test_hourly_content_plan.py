import datetime as dt
import os
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
        self.assertEqual(len(sent), 1)
        self.assertIn("Добавлено открытие.", sent[0][2])
        self.assertIn("Полный diff", sent[0][2])

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

        self.assertIn("AI-сводка недоступна", messages[0])
        self.assertIn(original_diff, messages[0])

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


if __name__ == "__main__":
    unittest.main()
