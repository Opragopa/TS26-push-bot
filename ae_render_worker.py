#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Prepare the open AE project with JSX and render it through After Effects."""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import ae_render_notify
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


def secure_workdir(path):
    """Create/validate a scratch directory that only the current user can write.

    After Effects executes JSX from this directory, so a world-writable location
    (the default ``/private/tmp/...``) would let any local user swap in their own
    script and get it run inside AE with this user's privileges. We create it 0700
    and refuse to use one that is owned by somebody else or is group/world-writable.
    """
    directory = Path(path).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        info = directory.lstat()
    except OSError as exc:
        raise RenderWorkerError("Не удалось проверить рабочую папку {}: {}".format(directory, exc))
    if stat.S_ISLNK(info.st_mode):
        raise RenderWorkerError("Рабочая папка {} — символическая ссылка; это небезопасно.".format(directory))
    if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
        raise RenderWorkerError(
            "Рабочая папка {} принадлежит другому пользователю (uid={}). "
            "Удалите её или укажите другой temp_project_dir.".format(directory, info.st_uid)
        )
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        try:
            directory.chmod(0o700)
        except OSError as exc:
            raise RenderWorkerError("Рабочая папка {} доступна на запись другим и не чинится: {}".format(directory, exc))
    return directory


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
    data.setdefault("require_open_project", True)
    data.setdefault("respect_existing_render", True)
    data.setdefault("busy_check_timeout_seconds", 5)
    data.setdefault("job_lease_seconds", 4 * 60 * 60)
    return data


def job_matches_active_shift(config, job):
    """Prevent stale session jobs from another shift reaching After Effects."""
    if job.get("kind") != "session_topic":
        return True
    auto_render = config.get("session_topics_auto_render")
    if auto_render is None:
        auto_render = os.environ.get("AE_RENDER_SESSION_TOPICS_ENABLED", "false")
    if str(auto_render).strip().lower() not in {"1", "true", "yes", "y", "on"}:
        return False
    configured = str(config.get("active_shift") or os.environ.get("AE_ACTIVE_SHIFT", "")).strip()
    if not configured:
        return True
    actual = str((job.get("payload") or {}).get("shift") or "").strip()
    return actual == configured


def safe_file_name(value):
    text = re.sub(r"[\\/:*?\"<>|]+", "_", str(value or "").strip())
    text = re.sub(r"\s+", " ", text).strip(". ")
    return text[:140] or "render"


def required_text(payload, name):
    value = str(payload.get(name, "")).strip()
    if not value:
        raise RenderWorkerError("Для {} не заполнено поле '{}'".format(payload.get("kind", "задания"), name))
    return value


def optional_text(payload, name):
    return str(payload.get(name, "")).strip()


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
        optional_text(payload, "description"),
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
        "temporary_project_path": "",
        "output_path": str(output_path),
        "use_open_project": config.get("reuse_open_project", True) is True,
        "require_open_project": config.get("require_open_project", True) is True,
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
            "description": optional_text(payload, "description"),
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
    check_dir = secure_workdir(Path(config["temp_project_dir"]).expanduser() / "_busy_check")
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


def project_paths_match(expected, actual):
    if not expected or not actual:
        return False
    return os.path.normcase(os.path.normpath(os.path.expanduser(str(expected)))) == os.path.normcase(
        os.path.normpath(os.path.expanduser(str(actual)))
    )


def active_project_path(config):
    """Read the currently open AE project without changing it."""
    if not process_exists("After Effects"):
        return ""
    check_dir = secure_workdir(Path(config["temp_project_dir"]).expanduser() / "_project_check")
    script_path = check_dir / "active_project.jsx"
    result_path = check_dir / "active_project.txt"
    error_path = check_dir / "active_project.error"
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
        if (file.open("w")) { file.write(String(text)); file.close(); }
    }
    try {
        var path = app.project && app.project.file ? app.project.file.fsName : "";
        write(resultFile, path);
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
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result_path.read_text(encoding="utf-8").strip() if result_path.exists() else ""


def expected_project_error(config):
    expected = str(config.get("project_path") or "").strip()
    actual = active_project_path(config)
    if project_paths_match(expected, actual):
        return ""
    if actual:
        return "Откройте в After Effects проект '{}'. Сейчас открыт другой проект: '{}'.".format(expected, actual)
    return "Откройте в After Effects проект '{}' перед запуском рендера плашки.".format(expected)


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


def cleanup_job_dir(config, job):
    """Remove per-job scripts and staged media, leaving only the shared busy check."""
    job_id = str(job.get("id") or "")
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", job_id):
        # Never hand an unvalidated component to rmtree.
        return
    root = Path(config["temp_project_dir"]).expanduser().resolve()
    job_dir = (root / job_id).resolve()
    if job_dir == root or root not in job_dir.parents:
        return
    if job_dir.is_dir() and not job_dir.is_symlink():
        shutil.rmtree(str(job_dir))


def process_job(config, job):
    ensure_renderer_available(config)
    secure_workdir(config["temp_project_dir"])
    # job["id"] is a server-generated uuid4 hex, but validate anyway so a hand-edited
    # queue file can never turn this into a path outside temp_project_dir.
    job_id = str(job.get("id") or "")
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", job_id):
        raise RenderWorkerError("Некорректный id задания: {!r}".format(job_id))
    project_dir = secure_workdir(Path(config["temp_project_dir"]).expanduser() / job_id)
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
    if prepared_mode != "open":
        details = error_path.read_text(encoding="utf-8").strip() if error_path.exists() else ""
        # Say what is actually open, otherwise the operator only learns which project
        # was expected and has to guess why AE refused.
        currently_open = active_project_path(config)
        if currently_open:
            situation = "Сейчас в After Effects открыт: '{}'.".format(currently_open)
        elif process_exists("After Effects"):
            situation = "After Effects запущен, но проект не открыт."
        else:
            situation = "After Effects не запущен."
        raise RenderWorkerError(
            "Задание требует открытый проект '{}'. {} {}".format(
                config["project_path"], situation, details or ""
            ).strip()
        )
    output_path = resolve_output(config, job)
    prepared_comp_name = project_dir / "prepared_comp_name.txt"
    if job["kind"] == "plaque" and prepared_comp_name.exists():
        comp_name = prepared_comp_name.read_text(encoding="utf-8").strip()
        if comp_name:
            output_path = Path(config["routes"]["plaque_output_dir"]).expanduser() / (safe_file_name(comp_name) + ".mov")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ae_render_queue.update_job(config["queue_path"], job["id"], status="rendering", output_path=str(output_path))
    ensure_renderer_available(config)
    print("[worker] Проект открыт: запускаю подготовленную Render Queue внутри After Effects.", flush=True)
    render_open_queue(params_path, project_dir)
    if not staged_output_path.exists():
        raise RenderWorkerError("Рендер завершился без файла {}".format(staged_output_path))
    shutil.copy2(str(staged_output_path), str(output_path))
    return output_path


def run_once(config):
    job = ae_render_queue.claim_next(config["queue_path"], config.get("job_lease_seconds", ae_render_queue.DEFAULT_LEASE_SECONDS))
    if not job:
        return False
    if not job_matches_active_shift(config, job):
        ae_render_queue.update_job(
            config["queue_path"],
            job["id"],
            status="cancelled",
            error="Смена задания не совпадает с active_shift; рендер пропущен.",
        )
        print("Пропущено задание {}: неактивная смена {}.".format(job["id"], (job.get("payload") or {}).get("shift", "")), flush=True)
        return True
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
        # Close the loop back to Telegram: the requester is not watching this log.
        ae_render_notify.safe_notify(ae_render_notify.notify_job_finished, job, str(output_path))
    except Exception as exc:
        if isinstance(exc, RenderWorkerBusy):
            ae_render_queue.update_job(config["queue_path"], job["id"], status="queued", error="")
            print("[worker] {} Задание {} возвращено в очередь.".format(exc, job["id"]), flush=True)
            return False
        ae_render_queue.update_job(config["queue_path"], job["id"], status="error", error=str(exc))
        print("Ошибка задания {}: {}".format(job["id"], exc), file=sys.stderr, flush=True)
        ae_render_notify.safe_notify(ae_render_notify.notify_job_failed, job, str(exc))
    finally:
        try:
            cleanup_job_dir(config, job)
        except OSError as cleanup_error:
            print("[worker] Не удалось убрать временные файлы задания {}: {}".format(job["id"], cleanup_error), file=sys.stderr, flush=True)
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
    # Telegram credentials live in .env; load them up front so render outcomes can be
    # reported even on runs that never touch Google Sheets.
    ae_render_notify.load_env_file(config.get("env_file", ".env"))
    recovered = ae_render_queue.recover_expired_jobs(config["queue_path"])
    if recovered:
        print("В очередь возвращено зависших заданий: {}.".format(len(recovered)), flush=True)
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
