import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tg_sheet_monitor


class RenderStatusMessageTests(unittest.TestCase):
    def test_queued_render_starts_now_message(self):
        self.assertIn(
            "запускается сейчас",
            tg_sheet_monitor.plaque_render_message({"status": "queued", "queue_ahead": 0}),
        )

    def test_queued_render_waits_behind_other_jobs(self):
        message = tg_sheet_monitor.plaque_render_message({"status": "queued", "queue_ahead": 2})

        self.assertIn("перед ним 2 задания", message)

    def test_queued_render_waits_for_busy_after_effects(self):
        message = tg_sheet_monitor.plaque_render_message({"status": "queued", "renderer_busy": True})

        self.assertIn("After Effects занят", message)

    def test_existing_done_render_message(self):
        message = tg_sheet_monitor.plaque_render_message({"status": "existing", "job": {"status": "done"}})

        self.assertIn("уже был выполнен", message)


if __name__ == "__main__":
    unittest.main()
