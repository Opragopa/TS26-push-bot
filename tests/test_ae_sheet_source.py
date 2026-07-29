import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ae_sheet_source


class _Worksheet:
    def get_all_values(self):
        return [
            ["ДЕНЬ", "ТЕМА", "ОПИСАНИЕ", "ПЛОЩАДКА", "ИСХОДНАЯ_ЯЧЕЙКА"],
            ["ДЕНЬ 1", "Тема без описания", "", "Амфитеатр", "C23"],
        ]


class _Spreadsheet:
    def worksheet(self, name):
        self.worksheet_name = name
        return _Worksheet()


class _Client:
    def open_by_key(self, key):
        self.key = key
        return _Spreadsheet()


class SessionTopicQueueTests(unittest.TestCase):
    def test_session_topic_auto_render_is_disabled_by_default(self):
        self.assertFalse(ae_sheet_source.session_topics_auto_render_enabled({}))
        self.assertFalse(ae_sheet_source.session_topics_auto_render_enabled({"session_topics_auto_render": False}))
        self.assertTrue(ae_sheet_source.session_topics_auto_render_enabled({"session_topics_auto_render": True}))

    def test_ae_ready_spreadsheet_id_accepts_google_url(self):
        config = {
            "ae_ready_spreadsheet_id": "https://docs.google.com/spreadsheets/d/ae-ready-id/edit?gid=0"
        }
        self.assertEqual("ae-ready-id", ae_sheet_source.ae_ready_spreadsheet_id(config))

    def test_empty_description_is_enqueued(self):
        config = {
            "ae_ready_spreadsheet_id": "ae-ready-id",
            "ae_ready_sessions_worksheet": "content_plan_sessions",
            "queue_path": "/tmp/ae-render-queue.json",
            "templates": {
                "session_topic": {"shift_by_day": {"1": "ПРАВДА"}},
            },
        }
        client = _Client()
        with mock.patch.object(ae_sheet_source.ae_render_queue, "enqueue", return_value=({}, True)) as enqueue:
            created = ae_sheet_source.enqueue_session_jobs(client, config)

        self.assertEqual(1, created)
        self.assertEqual("ae-ready-id", client.key)
        self.assertEqual("session_topic", enqueue.call_args.args[1])
        self.assertEqual("", enqueue.call_args.args[2]["description"])

    def test_active_shift_overrides_day_shift_mapping(self):
        config = {
            "ae_ready_spreadsheet_id": "ae-ready-id",
            "ae_ready_sessions_worksheet": "content_plan_sessions",
            "queue_path": "/tmp/ae-render-queue.json",
            "active_shift": "ПРАВДА",
            "templates": {
                "session_topic": {"shift_by_day": {"1": "ЕДИНСТВО", "2": "ПРАВДА", "3": "РОДИНА"}},
            },
        }
        client = _Client()
        with mock.patch.object(ae_sheet_source.ae_render_queue, "enqueue", return_value=({}, True)) as enqueue:
            created = ae_sheet_source.enqueue_session_jobs(client, config)

        self.assertEqual(1, created)
        self.assertEqual("ПРАВДА", enqueue.call_args.args[2]["shift"])


if __name__ == "__main__":
    unittest.main()
