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

import ae_render_registry
import ae_render_queue
import ae_sheet_source


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "ae_render_config.json"
AE_TEMPLATE_DIR = SCRIPT_DIR / "ae_templates"
PREPARE_SCRIPT = AE_TEMPLATE_DIR / "ae_prepare_project.jsx.template"
OPEN_QUEUE_RENDER_SCRIPT = AE_TEMPLATE_DIR / "ae_render_open_queue.jsx.template"
APPLE_SCRIPT = SCRIPT_DIR / "ae_run_script.applescript"


class RenderWorkerError(Exception):
    pass


class RenderWorkerBusy(Exception):
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
        "registry_path",
        "temp_project_dir",
        "person_plates_script_path",
        "session_topics_script_path",
        "output_module_templates",
        "routes",
    )
    missing = [name for name in required if not str(data.get(name, "")).strip()]
    if missing:
        raise RenderWorkerError("В ae_render_config.json не заполнены: {}".format(", ".join(missing)))
    for key in ("queue_path", "registry_path", "temp_project_dir"):
        value = Path(data[key]).expanduser()
        if not value.is_absolute():
            value = config_path.parent / value
        data[key] = str(value)
    data.setdefault("env_file", str(config_path.parent / ".env"))
    data.setdefault("respect_existing_render", True)
    data.setdefault("busy_check_timeout_seconds", 5)
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


def tsv_escape(value):
    text = str(value or "")
    if any(char in text for char in ('"', "\t", "\n", "\r")):
        text = '"' + text.replace('"', '""') + '"'
    return text


def write_session_topic_tsv(job, path):
    payload = dict(job.get("payload") or {})
    headers = ["ТЕМА", "ОПИСАНИЕ", "ИМЯ_КОМПОЗИЦИИ", "ДЕНЬ", "Смена"]
    values = [
        required_text(payload, "topic"),
        required_text(payload, "description"),
        safe_file_name(required_text(payload, "topic")),
        required_text(payload, "day"),
        required_text(payload, "shift"),
    ]
    path.write_text(
        "\t".join(headers) + "\n" + "\t".join(tsv_escape(value) for value in values) + "\n",
        encoding="utf-8",
    )


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
        session_template = config["templates"]["session_topic"]
        topic_tsv_path = Path(str(project_path.parent / "session_topic.tsv"))
        write_session_topic_tsv(job, topic_tsv_path)
        result.update({
            "session_shift": required_text(payload, "shift"),
            "session_comp_pattern": config["templates"]["session_topic"]["comp_pattern"],
            "session_topics_script_path": config["session_topics_script_path"],
            "session_topic_tsv_path": str(topic_tsv_path),
            "session_topic_target_folder_path": session_template.get("target_folder_path", ""),
            "topic_layer": session_template["topic_layer"],
            "description_layer": session_template["description_layer"],
            "topic": required_text(payload, "topic"),
            "description": required_text(payload, "description"),
            "day": required_text(payload, "day"),
            "shift": required_text(payload, "shift"),
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


def process_exists(process_name):
    try:
        return subprocess.run(
            ["pgrep", "-qx", process_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode == 0
    except OSError:
        return False


def after_effects_render_queue_busy(config):
    bundle_id = str(config.get("afterfx_bundle_id", "com.adobe.AfterEffects.application")).strip()
    if not bundle_id:
        return False
    check_dir = Path(config["temp_project_dir"]).expanduser() / "_busy_check"
    check_dir.mkdir(parents=True, exist_ok=True)
    script_path = check_dir / "check_render_busy.jsx"
    result_path = check_dir / "render_busy.txt"
    error_path = check_dir / "render_busy.error"
    for path in (result_path, error_path):
        if path.exists():
            path.unlink()
    script_path.write_text(
        """
(function () {
    var resultFile = new File(%s);
    var errorFile = new File(%s);
    function write(file, text) {
        file.encoding = "UTF-8";
        if (file.open("w")) {
            file.write(String(text));
            file.close();
        }
    }
    try {
        var busy = app.project && app.project.renderQueue && app.project.renderQueue.rendering === true;
        write(resultFile, busy ? "busy" : "idle");
    } catch (error) {
        write(errorFile, error && error.toString ? error.toString() : error);
    }
}());
""".strip() % (json.dumps(str(result_path)), json.dumps(str(error_path))),
        encoding="utf-8",
    )
    try:
        subprocess.run(
            ["osascript", str(APPLE_SCRIPT), str(script_path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=int(config.get("busy_check_timeout_seconds", 5)),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return True
    except OSError:
        return False
    if result_path.exists():
        return result_path.read_text(encoding="utf-8").strip() == "busy"
    return False


def renderer_busy(config):
    if config.get("respect_existing_render") is not True:
        return False
    if process_exists("aerender"):
        return True
    if not process_exists("After Effects"):
        return False
    return after_effects_render_queue_busy(config)


def ensure_renderer_available(config):
    if renderer_busy(config):
        raise RenderWorkerBusy("After Effects сейчас рендерит другое задание; воркер подождет.")


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
    ensure_renderer_available(config)
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
        ensure_renderer_available(config)
        print("[worker] Проект открыт: запускаю подготовленную Render Queue внутри After Effects.", flush=True)
        render_open_queue(params_path, project_dir)
    else:
        ensure_renderer_available(config)
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
        if job.get("kind") == "plaque":
            ae_render_registry.mark_rendered(
                config["registry_path"],
                job,
                output_path,
                config.get("stale_plaque_archive_dir", "_Устаревшие AE"),
            )
        print("Готово: {}".format(output_path), flush=True)
    except Exception as exc:
        if isinstance(exc, RenderWorkerBusy):
            ae_render_queue.update_job(config["queue_path"], job["id"], status="queued", error="")
            print("[worker] {} Задание {} возвращено в очередь.".format(exc, job["id"]), flush=True)
            return False
        ae_render_queue.update_job(config["queue_path"], job["id"], status="error", error=str(exc))
        print("Ошибка задания {}: {}".format(job["id"], exc), file=sys.stderr, flush=True)
    return True


def poll_sheets(config):
    try:
        result = ae_sheet_source.poll(config)
        if result["plaques"] or result["session_topics"]:
            print("Из Google Sheets добавлено в очередь: плашек {}, тем {}.".format(result["plaques"], result["session_topics"]), flush=True)
        if result.get("session_error"):
            print("Темы сессий не прочитаны: {}".format(result["session_error"]), file=sys.stderr, flush=True)
        plaque_sync = result.get("plaque_sync") or {}
        if plaque_sync.get("archived") or plaque_sync.get("missing") or plaque_sync.get("dry_run"):
            print("Синхронизация плашек: активных строк {}, перенесено в архив {}, файлов уже не было {}.".format(
                plaque_sync.get("active", 0),
                plaque_sync.get("archived", 0),
                plaque_sync.get("missing", 0),
            ), flush=True)
        if plaque_sync.get("skipped") == "empty_active_set":
            print("Синхронизация плашек пропущена: в таблице найдено 0 активных строк.", flush=True)
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
    parser.add_argument("--sync-dry-run", action="store_true", help="Показать, какие плашки были бы архивированы при исчезновении из таблицы.")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    if args.sync_dry_run:
        config["sync_dry_run"] = True
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
