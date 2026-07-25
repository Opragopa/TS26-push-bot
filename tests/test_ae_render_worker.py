import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ae_render_worker


class RenderWorkerPayloadTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "project_path": "/tmp/source.aep",
            "person_plates_script_path": "/tmp/person_plates_from_sheet.jsx",
            "reuse_open_project": True,
            "output_module_templates": {
                "plaque": "High Quality with Alpha",
                "session_topic": "DVX 3 no audio",
            },
            "render_settings_template": "",
            "routes": {
                "plaque_output_dir": "/tmp/plaques",
                "session_topics_root": "/tmp/sessions",
            },
            "templates": {
                "plaque": {
                    "comp_name": "MASTER-COMP",
                    "name_layer": "ФИО спикера",
                    "position_layer": "Должность",
                    "target_folder_path": "!_COMPS/Запись",
                },
                "session_topic": {
                    "comp_pattern": "{shift}_.*",
                    "topic_layer": "ТЕМА",
                    "description_layer": "ОПИСАНИЕ",
                },
            },
        }

    def test_plaque_payload_uses_manual_generator(self):
        job = {
            "kind": "plaque",
            "payload": {"name": "Садчикова Дарья", "position": "Ведущая"},
        }
        payload = ae_render_worker.build_prepare_payload(
            self.config,
            job,
            Path("/tmp/staged.mov"),
            Path("/tmp/render.aep"),
        )

        self.assertEqual("MASTER-COMP", payload["comp_name"])
        self.assertEqual("Садчикова Дарья", payload["plaque_name"])
        self.assertEqual("Ведущая", payload["plaque_position"])
        self.assertEqual(
            "/tmp/person_plates_from_sheet.jsx",
            payload["person_plates_script_path"],
        )
        self.assertNotIn("text_layers", payload)

    def test_load_config_requires_person_plate_generator(self):
        config = {
            "project_path": "/tmp/source.aep",
            "afterfx_bin": "/tmp/afterfx",
            "aerender_bin": "/tmp/aerender",
            "queue_path": "/tmp/queue.json",
            "temp_project_dir": "/tmp/jobs",
            "output_module_templates": {"plaque": "High Quality with Alpha"},
            "routes": {"plaque_output_dir": "/tmp/plaques"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(
                ae_render_worker.RenderWorkerError,
                "person_plates_script_path",
            ):
                ae_render_worker.load_config(config_path)

    def test_open_queue_fallback_script_is_available(self):
        self.assertTrue(ae_render_worker.OPEN_QUEUE_RENDER_SCRIPT.exists())
        script = ae_render_worker.OPEN_QUEUE_RENDER_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("app.project.renderQueue.render()", script)


if __name__ == "__main__":
    unittest.main()
