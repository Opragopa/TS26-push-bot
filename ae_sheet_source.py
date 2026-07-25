#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read renderable plaque and session rows directly from Google Sheets."""

import hashlib
import json
import os
import re
from pathlib import Path

import gspread
from google.oauth2.credentials import Credentials as UserCredentials
from google.oauth2.service_account import Credentials as ServiceCredentials

import ae_render_queue


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


def enqueue_plaque_jobs(client, config):
    spreadsheet = client.open_by_key(config["plaque_spreadsheet_id"])
    worksheet = worksheet_by_gid(spreadsheet, config["plaque_worksheet_gid"])
    rows = worksheet.get_all_values()
    start = int(config.get("plaque_start_row", 280))
    name_col = int(config.get("plaque_name_col", 1))
    position_col = int(config.get("plaque_position_col", 2))
    note_col = int(config.get("plaque_note_col", 5))
    note_text = normalize(config.get("plaque_note_text", "<-- добавлено через ТГ бота"))
    created = 0
    for row_number, row in enumerate(rows[start - 1 :], start=start):
        name = normalize(value(row, name_col))
        position = normalize(value(row, position_col))
        note = normalize(value(row, note_col))
        if not name or not position or note_text and note != note_text:
            continue
        key = "sheet-plaque:{}:{}:{}".format(worksheet.id, row_number, fingerprint(name, position))
        _, was_created = ae_render_queue.enqueue(
            config["queue_path"],
            "plaque",
            {"name": name, "position": position, "sheet_row": row_number},
            source_key=key,
        )
        created += int(was_created)
    return created


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
    shifts = config.get("shift_by_day", {})
    created = 0
    for row_number, row in enumerate(rows[1:], start=2):
        data = {name: normalize(row[indexes[name]] if len(row) > indexes[name] else "") for name in required}
        day_match = re.search(r"\d+", data["ДЕНЬ"])
        day = day_match.group(0) if day_match else ""
        shift = normalize(shifts.get(day, ""))
        if not day or not shift or not data["ТЕМА"] or not data["ОПИСАНИЕ"]:
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
    return {
        "plaques": enqueue_plaque_jobs(client, config),
        "session_topics": enqueue_session_jobs(client, config) if config.get("ae_ready_spreadsheet_id") else 0,
    }
