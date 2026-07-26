#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""File-backed registry of rendered AE outputs owned by the local worker."""

import datetime as _dt
import fcntl
import json
import os
import stat
import tempfile
from pathlib import Path


REGISTRY_VERSION = 1


class RenderRegistryError(Exception):
    pass


def now_text():
    return _dt.datetime.now().isoformat(timespec="seconds")


def today_text():
    return _dt.date.today().isoformat()


def default_registry_path():
    return Path(__file__).resolve().parent / "data" / "ae_render_registry.json"


def load_registry_unlocked(registry_path):
    if not registry_path.exists():
        return {"version": REGISTRY_VERSION, "items": {}}
    try:
        data = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RenderRegistryError("Не удалось прочитать реестр {}: {}".format(registry_path, exc))
    if not isinstance(data, dict) or not isinstance(data.get("items"), dict):
        raise RenderRegistryError("Некорректный формат реестра {}".format(registry_path))
    data.setdefault("version", REGISTRY_VERSION)
    return data


def save_registry_unlocked(registry_path, data):
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    previous_stat = registry_path.stat() if registry_path.exists() else None
    descriptor, temporary_name = tempfile.mkstemp(prefix=".ae_render_registry_", suffix=".json", dir=str(registry_path.parent))
    try:
        if previous_stat is not None:
            os.fchmod(descriptor, stat.S_IMODE(previous_stat.st_mode))
            try:
                os.fchown(descriptor, previous_stat.st_uid, previous_stat.st_gid)
            except PermissionError:
                pass
        else:
            os.fchmod(descriptor, 0o664)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary_name, registry_path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def locked_registry(registry_path):
    registry_path = Path(registry_path).expanduser()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = registry_path.with_suffix(registry_path.suffix + ".lock")
    lock_stream = lock_path.open("a+", encoding="utf-8")
    fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX)
    return registry_path, lock_stream


def active_plaque_ids(registry_path):
    registry_path, lock_stream = locked_registry(registry_path)
    try:
        data = load_registry_unlocked(registry_path)
        return {
            key
            for key, item in data["items"].items()
            if item.get("kind") == "plaque" and item.get("status") == "active"
        }
    finally:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
        lock_stream.close()


def unique_archive_path(output_path, archive_root_name):
    archive_dir = output_path.parent / archive_root_name / today_text()
    archive_path = archive_dir / output_path.name
    counter = 2
    while archive_path.exists():
        archive_path = archive_dir / "{} {}{}".format(output_path.stem, counter, output_path.suffix)
        counter += 1
    return archive_path


def move_to_archive(output_path, archive_root_name):
    archive_path = unique_archive_path(output_path, archive_root_name)
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.rename(archive_path)
    return archive_path


def mark_rendered(registry_path, job, output_path, stale_archive_root_name="_Устаревшие AE"):
    registry_path, lock_stream = locked_registry(registry_path)
    try:
        data = load_registry_unlocked(registry_path)
        payload = dict(job.get("payload") or {})
        item_id = str(payload.get("ae_id") or job.get("source_key") or job.get("id"))
        previous = dict(data["items"].get(item_id) or {})
        previous_output_text = str(previous.get("output_path", "")).strip()
        previous_output = Path(previous_output_text).expanduser() if previous_output_text else None
        current_output = Path(str(output_path)).expanduser()
        if previous_output is not None and previous_output.exists() and previous_output != current_output:
            previous["stale_output_path"] = str(move_to_archive(previous_output, stale_archive_root_name))
        previous.update({
            "id": item_id,
            "kind": job.get("kind"),
            "status": "active",
            "name": payload.get("name", ""),
            "position": payload.get("position", ""),
            "source_key": job.get("source_key", ""),
            "output_path": str(current_output),
            "updated_at": now_text(),
        })
        if not previous.get("created_at"):
            previous["created_at"] = now_text()
        data["items"][item_id] = previous
        save_registry_unlocked(registry_path, data)
        return previous
    finally:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
        lock_stream.close()


def archive_missing_plaques(registry_path, active_ids, archive_root_name="_Удаленные AE", dry_run=False):
    active_ids = {str(item) for item in active_ids}
    moved = []
    missing = []
    skipped = []
    registry_path, lock_stream = locked_registry(registry_path)
    try:
        data = load_registry_unlocked(registry_path)
        for item_id, item in sorted(data["items"].items()):
            if item.get("kind") != "plaque" or item.get("status") != "active" or item_id in active_ids:
                continue
            output_path = Path(str(item.get("output_path", ""))).expanduser()
            if not output_path.exists():
                item["status"] = "deleted"
                item["deleted_at"] = now_text()
                item["delete_reason"] = "missing_from_sheet_and_file_absent"
                missing.append(str(output_path))
                continue
            if dry_run:
                archive_path = unique_archive_path(output_path, archive_root_name)
                skipped.append({"from": str(output_path), "to": str(archive_path)})
                continue
            archive_path = move_to_archive(output_path, archive_root_name)
            item["status"] = "deleted"
            item["deleted_at"] = now_text()
            item["delete_reason"] = "missing_from_sheet"
            item["archived_output_path"] = str(archive_path)
            moved.append({"from": str(output_path), "to": str(archive_path)})
        if not dry_run:
            save_registry_unlocked(registry_path, data)
        return {"moved": moved, "missing": missing, "dry_run": skipped}
    finally:
        fcntl.flock(lock_stream.fileno(), fcntl.LOCK_UN)
        lock_stream.close()
