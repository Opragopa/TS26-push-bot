#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prepare a temporary AE project with JSX and render it through aerender."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import ae_render_queue
import ae_sheet_source


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "ae_render_config.json"
PREPARE_SCRIPT = SCRIPT_DIR / "ae_prepare_project.jsx"
OPEN_QUEUE_RENDER_SCRIPT = SCRIPT_DIR / "ae_render_open_queue.jsx"
APPLE_SCRIPT = SCRIPT_DIR / "ae_run_script.applescript"


class RenderWorkerError(Exception):
    pass


def load_config(path):
    config_path = Path(path).expanduser().resolve()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RenderWorkerError("Не удалось прочитать конфиг рендера: {}".format(exc))
    required = (
        "project_path",
        "afterfx_bin",
        "aerender_bin",
        "queue_path",
        "temp_project_dir",
        "person_plates_script_path",
        "output_module_templates",
        "routes",
    )
    missing = [name for name in required if not str(data.get(name, "")).strip()]
    if missing:
        raise RenderWorkerError("В ae_render_config.json не заполнены: {}".format(", ".join(missing)))
    for key in ("queue_path", "temp_project_dir"):
        value = Path(data[key]).expanduser()
        if not value.is_absolute():
            value = config_path.parent / value
        data[key] = str(value)
    data.setdefault("env_file", str(config_path.parent / ".env"))
    return data


def safe_file_name(value):
    text = re.sub(r"[\\/:*?\"<>|]+", "_", str(value or "").strip())
    text = re.sub(r"\s+", " ", text).strip(". ")
    return text[:140] or "render"


def required_text(payload, name):
    value = str(payload.get(name, "")).strip()
    if not value:
        raise RenderWorkerError("Для {} не заполнено поле '{}'".format(payload.get("kind", "задания"), name))
    return value


def output_module_template(config, kind):
    template = str(config.get("output_module_templates", {}).get(kind, "")).strip()
    if not template:
        raise RenderWorkerError("Для '{}' не задан output module template".format(kind))
    return template


def resolve_output(config, job):
    payload = dict(job.get("payload") or {})
    kind = job.get("kind")
    routes = config["routes"]
    if kind == "plaque":
        root = Path(routes["plaque_output_dir"]).expanduser()
        return root / (safe_file_name(required_text(payload, "name")) + ".mov")
    if kind == "session_topic":
        day = required_text(payload, "day")
        shift = required_text(payload, "shift")
        root = Path(routes["session_topics_root"]).expanduser() / safe_file_name(shift) / ("День " + safe_file_name(day))
        return root / (safe_file_name(required_text(payload, "topic")) + ".mov")
    if kind == "fire_of_meanings":
        root = Path(routes["fire_of_meanings_output_dir"]).expanduser()
        return root / (safe_file_name(required_text(payload, "name")) + ".mov")
    raise RenderWorkerError("Неизвестный тип задания '{}'".format(kind))


def build_prepare_payload(config, job, output_path, project_path):
    payload = dict(job.get("payload") or {})
    kind = job["kind"]
    result = {
        "kind": kind,
        "source_project_path": config["project_path"],
        "temporary_project_path": str(project_path),
        "output_path": str(output_path),
        "use_open_project": config.get("reuse_open_project", True) is True,
        "prepared_marker_path": str(project_path.parent / "prepared.mode"),
        "prepared_comp_name_path": str(project_path.parent / "prepared_comp_name.txt"),
        "output_module_template": output_module_template(config, kind),
        "render_settings_template": config.get("render_settings_template", ""),
    }
    if kind == "plaque":
        plaque_template = config["templates"]["plaque"]
        result.update({
            "comp_name": plaque_template["comp_name"],
            "name_layer": plaque_template["name_layer"],
            "position_layer": plaque_template["position_layer"],
            "person_plates_script_path": config["person_plates_script_path"],
            "plaque_target_folder_path": plaque_template.get("target_folder_path", ""),
            "plaque_name": required_text(payload, "name"),
            "plaque_position": required_text(payload, "position"),
        })
    elif kind == "session_topic":
        result.update({
            "session_shift": required_text(payload, "shift"),
            "session_comp_pattern": config["templates"]["session_topic"]["comp_pattern"],
            "text_layers": {
                config["templates"]["session_topic"]["topic_layer"]: required_text(payload, "topic"),
                config["templates"]["session_topic"]["description_layer"]: required_text(payload, "description"),
            },
        })
    else:
        raise RenderWorkerError("Для '{}' пока не настроен AE-шаблон".format(kind))
    return result


def run_command(command, label, cwd=None):
    try:
        process = subprocess.Popen(command, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=1)
    except OSError as exc:
        raise RenderWorkerError("Не удалось запустить {}: {}".format(label, exc))
    output_lines = []
    if process.stdout is not None:
        for line in process.stdout:
            line = line.rstrip()
            output_lines.append(line)
            print("[{}] {}".format(label, line), flush=True)
    return_code = process.wait()
    if return_code != 0:
        output = "\n".join(output_lines)[-4000:]
        raise RenderWorkerError("{} завершился с кодом {}.\n{}".format(label, return_code, output))
    return "\n".join(output_lines)


def prepare_project(config, job_script, temporary_project, error_path, prepared_marker):
    bundle_id = str(config.get("afterfx_bundle_id", "")).strip()
    if config.get("afterfx_launch_method") == "osascript_doscript":
        command = ["osascript", str(APPLE_SCRIPT), str(job_script)]
    elif bundle_id:
        command = ["open", "-n", "-b", bundle_id, "--args", "-r", str(job_script)]
    else:
        command = [config["afterfx_bin"], "-r", str(job_script)]
    try:
        subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    except OSError as exc:
        raise RenderWorkerError("Не удалось запустить After Effects: {}".format(exc))
    deadline = time.time() + int(config.get("prepare_timeout_seconds", 180))
    while time.time() < deadline:
        if temporary_project.exists() or prepared_marker.exists() or error_path.exists():
            return prepared_marker.read_text(encoding="utf-8").strip() if prepared_marker.exists() else "temp"
        time.sleep(1)
    raise RenderWorkerError("After Effects не завершил JSX за {} секунд".format(config.get("prepare_timeout_seconds", 180)))


def render_open_queue(params_path, project_dir):
    error_path = Path(str(params_path) + ".render.error")
    done_path = Path(str(params_path) + ".render.done")
    for marker in (error_path, done_path):
        if marker.exists():
            marker.unlink()
    job_script = project_dir / "render_open_queue.jsx"
    template = OPEN_QUEUE_RENDER_SCRIPT.read_text(encoding="utf-8")
    job_script.write_text(template.replace("__PARAMS_PATH__", json.dumps(str(params_path))), encoding="utf-8")
    run_command(["osascript", str(APPLE_SCRIPT), str(job_script)], "afterfx-fallback")
    if error_path.exists():
        raise RenderWorkerError(error_path.read_text(encoding="utf-8").strip())
    if not done_path.exists():
        raise RenderWorkerError("After Effects не подтвердил завершение резервного рендера.")


def process_job(config, job):
    project_dir = Path(config["temp_project_dir"]).expanduser() / job["id"]
    project_dir.mkdir(parents=True, exist_ok=True)
    temporary_project = project_dir / "render.aep"
    staged_output_path = project_dir / "output.mov"
    params_path = project_dir / "params.json"
    stale_paths = (
        temporary_project,
        staged_output_path,
        project_dir / "prepared.mode",
        project_dir / "prepared_comp_name.txt",
        Path(str(params_path) + ".error"),
    )
    for stale_path in stale_paths:
        if stale_path.exists():
            stale_path.unlink()
    params_path.write_text(json.dumps(build_prepare_payload(config, job, staged_output_path, temporary_project), ensure_ascii=False, indent=2), encoding="utf-8")
    job_script = project_dir / "prepare.jsx"
    template = PREPARE_SCRIPT.read_text(encoding="utf-8")
    job_script.write_text(template.replace("__PARAMS_PATH__", json.dumps(str(params_path))), encoding="utf-8")
    error_path = Path(str(params_path) + ".error")
    prepared_mode = prepare_project(config, job_script, temporary_project, error_path, project_dir / "prepared.mode")
    if prepared_mode == "open":
        render_project = Path(config["project_path"])
        reuse = True
    else:
        render_project = temporary_project
        reuse = False
    if not render_project.exists():
        details = error_path.read_text(encoding="utf-8").strip() if error_path.exists() else "нет отчета об ошибке JSX"
        raise RenderWorkerError("JSX не создал временный проект {}. {}".format(temporary_project, details))
    output_path = resolve_output(config, job)
    prepared_comp_name = project_dir / "prepared_comp_name.txt"
    if job["kind"] == "plaque" and prepared_comp_name.exists():
        comp_name = prepared_comp_name.read_text(encoding="utf-8").strip()
        if comp_name:
            output_path = Path(config["routes"]["plaque_output_dir"]).expanduser() / (safe_file_name(comp_name) + ".mov")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ae_render_queue.update_job(config["queue_path"], job["id"], status="rendering", output_path=str(output_path))
    aerender_path = Path(config["aerender_bin"])
    if config.get("aerender_working_dir"):
        aerender_command = ["./" + aerender_path.name]
    else:
        aerender_command = [str(aerender_path)]
    if reuse:
        print("[worker] Проект открыт: запускаю подготовленную Render Queue внутри After Effects.", flush=True)
        render_open_queue(params_path, project_dir)
    else:
        aerender_command.extend(["-project", str(render_project)])
        run_command(aerender_command, "aerender", cwd=config.get("aerender_working_dir"))
    if not staged_output_path.exists():
        raise RenderWorkerError("Рендер завершился без файла {}".format(staged_output_path))
    shutil.copy2(str(staged_output_path), str(output_path))
    return output_path


def run_once(config):
    job = ae_render_queue.claim_next(config["queue_path"])
    if not job:
        return False
    try:
        output_path = process_job(config, job)
        ae_render_queue.update_job(config["queue_path"], job["id"], status="done", output_path=str(output_path), error="")
        print("Готово: {}".format(output_path), flush=True)
    except Exception as exc:
        ae_render_queue.update_job(config["queue_path"], job["id"], status="error", error=str(exc))
        print("Ошибка задания {}: {}".format(job["id"], exc), file=sys.stderr, flush=True)
    return True


def poll_sheets(config):
    try:
        result = ae_sheet_source.poll(config)
        if result["plaques"] or result["session_topics"]:
            print("Из Google Sheets добавлено в очередь: плашек {}, тем {}.".format(result["plaques"], result["session_topics"]), flush=True)
    except Exception as exc:
        print("Ошибка чтения Google Sheets: {}".format(exc), file=sys.stderr, flush=True)


def main(argv=None):
    if sys.platform == "darwin" and os.geteuid() == 0:
        raise RenderWorkerError(
            "AE render worker нельзя запускать через sudo/root: "
            "AppleScript не сможет обратиться к After Effects в пользовательской сессии."
        )
    parser = argparse.ArgumentParser(description="Рендерит задания очереди через After Effects и aerender.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--once", action="store_true", help="Обработать максимум одно задание.")
    parser.add_argument("--interval", type=int, default=5, help="Пауза между проверками очереди.")
    parser.add_argument("--poll-sheets", action="store_true", help="Читать Google Sheets перед обработкой очереди.")
    parser.add_argument("--poll-only", action="store_true", help="Прочитать Google Sheets и завершить без запуска рендера.")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    while True:
        if args.poll_sheets:
            poll_sheets(config)
        if args.poll_only:
            return
        found = run_once(config)
        if args.once:
            return
        if not found:
            time.sleep(max(1, args.interval))


if __name__ == "__main__":
    main()
