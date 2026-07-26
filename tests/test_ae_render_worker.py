import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import ae_render_worker
import ae_render_registry
import ae_render_queue


class RenderWorkerPayloadTests(unittest.TestCase):
    def test_expired_claim_is_recovered_after_worker_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            queue_path = Path(temp_dir) / "queue.json"
            created, was_created = ae_render_queue.enqueue(queue_path, "plaque", {"name": "Иванов Иван"}, source_key="plaque:1")
            self.assertTrue(was_created)
            claimed = ae_render_queue.claim_next(queue_path, lease_seconds=1)
            self.assertEqual(created["id"], claimed["id"])
            ae_render_queue.update_job(queue_path, claimed["id"], lease_expires_at="2000-01-01T00:00:00")

            recovered = ae_render_queue.recover_expired_jobs(queue_path)

            self.assertEqual([claimed["id"]], [job["id"] for job in recovered])
            self.assertEqual("queued", ae_render_queue.load_queue_unlocked(queue_path)["jobs"][0]["status"])

    def setUp(self):
        self.config = {
            "project_path": "/tmp/source.aep",
            "person_plates_script_path": "/tmp/person_plates_from_sheet.jsx",
            "session_topics_script_path": "/tmp/session_topics_from_sheet.jsx",
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
            "registry_path": "/tmp/registry.json",
            "temp_project_dir": "/tmp/jobs",
            "session_topics_script_path": "/tmp/session_topics_from_sheet.jsx",
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

    def test_session_topic_payload_uses_generator_tsv(self):
        job = {
            "kind": "session_topic",
            "payload": {
                "day": "1",
                "shift": "ПРАВДА",
                "topic": "Большая тема",
                "description": "Описание сессии",
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = ae_render_worker.build_prepare_payload(
                self.config,
                job,
                Path(temp_dir) / "staged.mov",
                Path(temp_dir) / "render.aep",
            )

            self.assertEqual("/tmp/session_topics_from_sheet.jsx", payload["session_topics_script_path"])
            self.assertEqual("DVX 3 no audio", payload["output_module_template"])
            tsv_path = Path(payload["session_topic_tsv_path"])
            self.assertTrue(tsv_path.exists())
            self.assertIn("Большая тема", tsv_path.read_text(encoding="utf-8"))

    def test_open_queue_fallback_script_is_available(self):
        self.assertTrue(ae_render_worker.OPEN_QUEUE_RENDER_SCRIPT.exists())
        script = ae_render_worker.OPEN_QUEUE_RENDER_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("app.project.renderQueue.render()", script)

    def test_root_jsx_files_are_host_launchers(self):
        for script_name in ("ae_prepare_project.jsx", "ae_render_open_queue.jsx"):
            script = (ROOT / script_name).read_text(encoding="utf-8")
            self.assertIn("python", script)
            self.assertIn("main.py", script)
            self.assertNotIn("__PARAMS_PATH__", script)
            self.assertNotIn("app.project", script)

    def test_ae_scripts_live_as_templates(self):
        self.assertEqual(".template", ae_render_worker.PREPARE_SCRIPT.suffix)
        self.assertEqual(".template", ae_render_worker.OPEN_QUEUE_RENDER_SCRIPT.suffix)
        self.assertIn("__PARAMS_PATH__", ae_render_worker.PREPARE_SCRIPT.read_text(encoding="utf-8"))

    def test_run_once_does_not_check_renderer_when_queue_is_empty(self):
        calls = []
        original_busy = ae_render_worker.renderer_busy
        original_claim = ae_render_worker.ae_render_queue.claim_next
        try:
            ae_render_worker.renderer_busy = lambda config: calls.append("busy-check")
            ae_render_worker.ae_render_queue.claim_next = lambda *args: None

            self.assertFalse(ae_render_worker.run_once({"queue_path": "/tmp/queue.json"}))
            self.assertEqual([], calls)
        finally:
            ae_render_worker.renderer_busy = original_busy
            ae_render_worker.ae_render_queue.claim_next = original_claim

    def test_jsx_templates_refuse_to_touch_active_render_queue(self):
        prepare_script = ae_render_worker.PREPARE_SCRIPT.read_text(encoding="utf-8")
        open_queue_script = ae_render_worker.OPEN_QUEUE_RENDER_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("renderQueue.rendering === true", prepare_script)
        self.assertIn("renderQueue.rendering === true", open_queue_script)


class RenderRegistryTests(unittest.TestCase):
    def test_archive_missing_plaque_moves_owned_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output = root / "Плашка.mov"
            output.write_text("mov", encoding="utf-8")
            registry = root / "registry.json"
            ae_render_registry.mark_rendered(
                registry,
                {"id": "job-1", "kind": "plaque", "payload": {"ae_id": "ae-1", "name": "Плашка"}},
                output,
            )

            result = ae_render_registry.archive_missing_plaques(registry, set(), "_Удаленные AE")

            self.assertEqual(1, len(result["moved"]))
            self.assertFalse(output.exists())
            self.assertTrue(Path(result["moved"][0]["to"]).exists())


if __name__ == "__main__":
    unittest.main()
