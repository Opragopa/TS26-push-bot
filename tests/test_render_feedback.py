#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for reporting render outcomes back to Telegram and retrying failures.

Covers the gap where a plaque was queued successfully, failed inside After Effects,
and nobody was ever told.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ae_render_notify as notify
import ae_render_queue


class EnvIsolationMixin:
    ENV_KEYS = (
        "TELEGRAM_CHAT_ID",
        "TELEGRAM_CHAT_IDS",
        "TELEGRAM_ADMIN_CHAT_IDS",
        "AE_RENDER_NOTIFY_TELEGRAM",
    )

    def setUp(self):
        self._saved = {key: os.environ.get(key) for key in self.ENV_KEYS}
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class RecipientTests(EnvIsolationMixin, unittest.TestCase):
    def test_requester_is_notified_first(self):
        os.environ["TELEGRAM_ADMIN_CHAT_IDS"] = "111"
        job = {"payload": {"requested_by": "555"}}
        self.assertEqual(notify.recipients_for(job), ["555", "111"])

    def test_sheet_poller_job_falls_back_to_admins(self):
        os.environ["TELEGRAM_ADMIN_CHAT_IDS"] = "111,222"
        job = {"payload": {"name": "Иванов Иван"}}
        self.assertEqual(notify.recipients_for(job), ["111", "222"])

    def test_requester_is_not_duplicated_when_also_admin(self):
        os.environ["TELEGRAM_ADMIN_CHAT_IDS"] = "111"
        job = {"payload": {"requested_by": "111"}}
        self.assertEqual(notify.recipients_for(job), ["111"])

    def test_admin_ids_fall_back_to_the_main_chat_id(self):
        os.environ["TELEGRAM_CHAT_ID"] = "999"
        self.assertEqual(notify.admin_chat_ids(), ["999"])

    def test_notifications_can_be_switched_off(self):
        os.environ["AE_RENDER_NOTIFY_TELEGRAM"] = "false"
        self.assertFalse(notify.notify_enabled())
        os.environ["AE_RENDER_NOTIFY_TELEGRAM"] = "true"
        self.assertTrue(notify.notify_enabled())

    def test_notifications_are_on_by_default(self):
        self.assertTrue(notify.notify_enabled())


class JobDescriptionTests(unittest.TestCase):
    def test_plaque_is_described_by_name_and_position(self):
        job = {"kind": "plaque", "payload": {"name": "Иванов Иван", "position": "Директор"}}
        self.assertEqual(notify.describe_job(job), "Иванов Иван — Директор")

    def test_session_topic_is_described_by_day_and_topic(self):
        job = {"kind": "session_topic", "payload": {"day": "2", "shift": "ПРАВДА", "topic": "Тема"}}
        self.assertIn("ПРАВДА", notify.describe_job(job))
        self.assertIn("Тема", notify.describe_job(job))


class ErrorHintTests(unittest.TestCase):
    """The operator needs an instruction, not the worker's internal wording."""

    def test_wrong_project_error_tells_you_to_open_the_project(self):
        hint = notify.hint_for_error("Задание требует открытый проект '/x/y.aep'.")
        self.assertIn("Откройте", hint)
        self.assertIn("/render_retry", hint)

    def test_export_error_points_at_the_output_module(self):
        hint = notify.hint_for_error("After Effects error: An unexpected error occurred while exporting a composition.")
        self.assertIn("Output Module", hint)

    def test_busy_renderer_is_explained_as_temporary(self):
        self.assertIn("занят", notify.hint_for_error("After Effects сейчас рендерит другое задание"))

    def test_unknown_error_still_offers_a_next_step(self):
        self.assertIn("/render_retry", notify.hint_for_error("нечто неожиданное"))


class NotifyIsolationTests(unittest.TestCase):
    def test_a_failing_notifier_never_breaks_the_render(self):
        def boom():
            raise RuntimeError("telegram down")

        # safe_notify must swallow this: a notification problem is not a render problem.
        notify.safe_notify(boom)

    def test_send_message_without_a_token_returns_false(self):
        saved = os.environ.pop("TELEGRAM_BOT_TOKEN", None)
        try:
            self.assertFalse(notify.send_message("1", "text"))
        finally:
            if saved is not None:
                os.environ["TELEGRAM_BOT_TOKEN"] = saved


class RetryFailedJobsTests(unittest.TestCase):
    def build_queue(self, jobs):
        directory = tempfile.mkdtemp()
        path = Path(directory) / "queue.json"
        path.write_text(json.dumps({"version": 1, "jobs": jobs}, ensure_ascii=False), encoding="utf-8")
        return path

    def test_failed_jobs_return_to_the_queue(self):
        path = self.build_queue([
            {"id": "a", "kind": "plaque", "status": "error", "error": "boom", "payload": {"name": "A"}},
            {"id": "b", "kind": "plaque", "status": "done", "payload": {"name": "B"}},
        ])
        retried = ae_render_queue.retry_failed_jobs(path)
        self.assertEqual(len(retried), 1)
        counts, _ = ae_render_queue.queue_counts(path)
        self.assertEqual(counts.get("queued"), 1)
        self.assertEqual(counts.get("done"), 1)
        self.assertNotIn("error", counts)

    def test_retry_clears_the_previous_error_text(self):
        path = self.build_queue([{"id": "a", "kind": "plaque", "status": "error", "error": "boom", "payload": {}}])
        retried = ae_render_queue.retry_failed_jobs(path)
        self.assertEqual(retried[0]["error"], "")
        self.assertEqual(retried[0]["status"], "queued")

    def test_finished_and_cancelled_jobs_are_untouched(self):
        path = self.build_queue([
            {"id": "a", "kind": "plaque", "status": "done", "payload": {}},
            {"id": "b", "kind": "plaque", "status": "cancelled", "payload": {}},
        ])
        self.assertEqual(ae_render_queue.retry_failed_jobs(path), [])

    def test_retry_can_be_limited(self):
        jobs = [{"id": str(i), "kind": "plaque", "status": "error", "payload": {}} for i in range(5)]
        path = self.build_queue(jobs)
        self.assertEqual(len(ae_render_queue.retry_failed_jobs(path, limit=2)), 2)
        counts, _ = ae_render_queue.queue_counts(path)
        self.assertEqual(counts.get("error"), 3)

    def test_retry_can_be_filtered_by_kind(self):
        path = self.build_queue([
            {"id": "a", "kind": "plaque", "status": "error", "payload": {}},
            {"id": "b", "kind": "session_topic", "status": "error", "payload": {}},
        ])
        retried = ae_render_queue.retry_failed_jobs(path, kind="plaque")
        self.assertEqual(len(retried), 1)
        self.assertEqual(retried[0]["kind"], "plaque")


class QueueCountsTests(unittest.TestCase):
    def test_counts_and_newest_failure_first(self):
        directory = tempfile.mkdtemp()
        path = Path(directory) / "queue.json"
        path.write_text(json.dumps({"version": 1, "jobs": [
            {"id": "old", "status": "error", "updated_at": "2026-01-01T00:00:00", "payload": {}},
            {"id": "new", "status": "error", "updated_at": "2026-07-29T00:00:00", "payload": {}},
            {"id": "ok", "status": "done", "updated_at": "2026-07-29T00:00:00", "payload": {}},
        ]}), encoding="utf-8")

        counts, failures = ae_render_queue.queue_counts(path)

        self.assertEqual(counts, {"error": 2, "done": 1})
        self.assertEqual(failures[0]["id"], "new")

    def test_missing_queue_file_reports_empty(self):
        counts, failures = ae_render_queue.queue_counts(Path(tempfile.mkdtemp()) / "absent.json")
        self.assertEqual(counts, {})
        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
