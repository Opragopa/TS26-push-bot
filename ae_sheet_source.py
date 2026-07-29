#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read renderable plaque and session rows directly from Google Sheets."""

import hashlib
import json
import os
import re
import urllib.parse
import uuid
from pathlib import Path

import gspread
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as ServiceCredentials

import ae_render_queue
import ae_render_registry


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


def load_env_file(path):
    path = Path(path).expanduser()
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        if name.strip() and name.strip() not in os.environ:
            os.environ[name.strip()] = value.strip().strip('"').strip("'")


def normalize(value):
    return re.sub(r"\s+", " ", str(value or "").replace("\r", " ").replace("\n", " ")).strip()


def auth_client(config):
    load_env_file(config.get("env_file", ".env"))
    oauth_file = str(config.get("google_oauth_user_file") or os.environ.get("GOOGLE_OAUTH_USER_FILE", "")).strip()
    service_file = str(config.get("google_service_account_file") or os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "")).strip()
    if oauth_file:
        credentials = UserCredentials.from_authorized_user_file(str(Path(oauth_file).expanduser()), SCOPES)
    elif service_file:
        credentials = ServiceCredentials.from_service_account_file(str(Path(service_file).expanduser()), scopes=SCOPES)
    else:
        raise RuntimeError("Не задан google_oauth_user_file или GOOGLE_OAUTH_USER_FILE")
    return gspread.authorize(credentials)


def ae_ready_spreadsheet_id(config):
    explicit = str(config.get("ae_ready_spreadsheet_id") or os.environ.get("AE_READY_SPREADSHEET_ID", "")).strip()
    if explicit:
        match = re.search(r"/spreadsheets/d/([^/]+)", urllib.parse.urlparse(explicit).path)
        return match.group(1) if match else explicit
    state_path = str(config.get("ae_ready_state_path", "")).strip()
    if not state_path:
        return ""
    path = Path(state_path).expanduser()
    if not path.is_absolute():
        path = Path(__file__).resolve().parent / path
    if not path.exists():
        return ""
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    data = state.get("_ae_ready_content_plan") or {}
    saved = str(data.get("spreadsheet_id") or "").strip()
    match = re.search(r"/spreadsheets/d/([^/]+)", urllib.parse.urlparse(saved).path)
    return match.group(1) if match else saved


def worksheet_by_gid(spreadsheet, gid):
    for worksheet in spreadsheet.worksheets():
        if worksheet.id == int(gid):
            return worksheet
    raise RuntimeError("В таблице {} не найден лист gid={}".format(spreadsheet.id, gid))


def value(row, index):
    return row[index - 1] if len(row) >= index else ""


def fingerprint(*parts):
    raw = "\x1f".join(normalize(part) for part in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def configured_shift_by_day(config):
    candidates = (
        config.get("shift_by_day") or {},
        config.get("templates", {}).get("session_topic", {}).get("shift_by_day", {}) or {},
    )
    for shifts in candidates:
        if any(normalize(value) for value in shifts.values()):
            active_shift = normalize(config.get("active_shift") or os.environ.get("AE_ACTIVE_SHIFT", ""))
            if active_shift:
                return {str(day): active_shift for day in shifts if normalize(day)}
            return shifts
    return {}


def active_shift(config):
    return normalize(config.get("active_shift") or os.environ.get("AE_ACTIVE_SHIFT", ""))


def session_topics_auto_render_enabled(config):
    value = config.get("session_topics_auto_render")
    if value is None:
        value = os.environ.get("AE_RENDER_SESSION_TOPICS_ENABLED", "false")
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def stable_fallback_plaque_id(name, position):
    return "content-" + fingerprint(name, position)


def plaque_row_id(worksheet, row_number, row, ae_id_col, name, position):
    if ae_id_col <= 0:
        return stable_fallback_plaque_id(name, position)
    existing = normalize(value(row, ae_id_col))
    if existing:
        return existing
    new_id = "ae-" + uuid.uuid4().hex[:16]
    worksheet.update_cell(row_number, ae_id_col, new_id)
    return new_id


def enqueue_plaque_jobs(client, config):
    spreadsheet = client.open_by_key(config["plaque_spreadsheet_id"])
    worksheet = worksheet_by_gid(spreadsheet, config["plaque_worksheet_gid"])
    rows = worksheet.get_all_values()
    start = int(config.get("plaque_start_row", 280))
    name_col = int(config.get("plaque_name_col", 1))
    position_col = int(config.get("plaque_position_col", 2))
    note_col = int(config.get("plaque_note_col", 5))
    ae_id_col = int(config.get("plaque_ae_id_col", 0) or 0)
    note_text = normalize(config.get("plaque_note_text", "<-- добавлено через ТГ бота"))
    created = 0
    active_ids = set()
    for row_number, row in enumerate(rows[start - 1 :], start=start):
        name = normalize(value(row, name_col))
        position = normalize(value(row, position_col))
        note = normalize(value(row, note_col))
        if not name or not position or note_text and note != note_text:
            continue
        ae_id = plaque_row_id(worksheet, row_number, row, ae_id_col, name, position)
        active_ids.add(ae_id)
        key = "sheet-plaque:{}:{}:{}".format(worksheet.id, ae_id, fingerprint(name, position))
        _, was_created = ae_render_queue.enqueue(
            config["queue_path"],
            "plaque",
            {"name": name, "position": position, "sheet_row": row_number, "ae_id": ae_id},
            source_key=key,
        )
        created += int(was_created)
    return {"created": created, "active_ids": active_ids}


def sync_missing_plaques(config, active_ids):
    mode = str(config.get("delete_missing_plaques", "archive")).strip().lower()
    if mode in {"", "off", "false", "0", "none"}:
        return {"active": len(active_ids), "archived": 0, "missing": 0}
    if mode not in {"archive"}:
        raise RuntimeError("delete_missing_plaques поддерживает только 'off' или 'archive'")
    if not active_ids and config.get("allow_archive_all_plaques") is not True:
        return {"active": 0, "archived": 0, "missing": 0, "skipped": "empty_active_set"}
    result = ae_render_registry.archive_missing_plaques(
        config["registry_path"],
        active_ids,
        config.get("deleted_plaque_archive_dir", "_Удаленные AE"),
        dry_run=config.get("sync_dry_run") is True,
    )
    return {
        "active": len(active_ids),
        "archived": len(result["dry_run"] if result.get("dry_run") else result["moved"]),
        "missing": len(result["missing"]),
        "dry_run": bool(config.get("sync_dry_run") is True),
    }


def enqueue_session_jobs(client, config):
    spreadsheet = client.open_by_key(config["ae_ready_spreadsheet_id"])
    worksheet = spreadsheet.worksheet(config.get("ae_ready_sessions_worksheet", "content_plan_sessions"))
    rows = worksheet.get_all_values()
    if not rows:
        return 0
    headers = [normalize(item) for item in rows[0]]
    indexes = {name: index for index, name in enumerate(headers)}
    required = ("ДЕНЬ", "ТЕМА", "ОПИСАНИЕ", "ПЛОЩАДКА", "ИСХОДНАЯ_ЯЧЕЙКА")
    if any(name not in indexes for name in required):
        raise RuntimeError("В AE-ready не хватает колонок: {}".format(", ".join(name for name in required if name not in indexes)))
    shifts = configured_shift_by_day(config)
    selected_shift = active_shift(config)
    created = 0
    for row_number, row in enumerate(rows[1:], start=2):
        data = {name: normalize(row[indexes[name]] if len(row) > indexes[name] else "") for name in required}
        day_match = re.search(r"\d+", data["ДЕНЬ"])
        day = day_match.group(0) if day_match else ""
        shift = normalize(shifts.get(day, ""))
        if selected_shift and shift != selected_shift:
            continue
        if not day or not shift or not data["ТЕМА"]:
            continue
        key = "sheet-session:{}:{}:{}".format(row_number, shift, fingerprint(data["ТЕМА"], data["ОПИСАНИЕ"], data["ПЛОЩАДКА"]))
        _, was_created = ae_render_queue.enqueue(
            config["queue_path"],
            "session_topic",
            {"day": day, "shift": shift, "topic": data["ТЕМА"], "description": data["ОПИСАНИЕ"], "venue": data["ПЛОЩАДКА"]},
            source_key=key,
        )
        created += int(was_created)
    return created


def poll(config):
    client = auth_client(config)
    plaque_result = enqueue_plaque_jobs(client, config)
    session_topics = 0
    session_error = ""
    if not session_topics_auto_render_enabled(config):
        return {
            "plaques": plaque_result["created"],
            "session_topics": 0,
            "session_error": "Автоматический рендер тем сессий отключен для ручной проверки.",
            "plaque_sync": sync_missing_plaques(config, plaque_result["active_ids"]),
        }
    spreadsheet_id = ae_ready_spreadsheet_id(config)
    if spreadsheet_id:
        session_config = dict(config)
        session_config["ae_ready_spreadsheet_id"] = spreadsheet_id
        try:
            session_topics = enqueue_session_jobs(client, session_config)
        except Exception as exc:
            session_error = str(exc)
    else:
        session_error = "Не задан ae_ready_spreadsheet_id: темы сессий не могут попасть в очередь. Получите ссылку командой /ae_link и внесите ID в ae_render_config.json."
    return {
        "plaques": plaque_result["created"],
        "session_topics": session_topics,
        "session_error": session_error,
        "plaque_sync": sync_missing_plaques(config, plaque_result["active_ids"]),
    }
