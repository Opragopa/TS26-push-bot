#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Poll Google Sheets and send Telegram notifications on content changes."""

import argparse
import csv
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


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


APP_NAME = "tg-pushes-TS26"
APP_VERSION = "2026-07-22.14"
DEFAULT_DATA_DIR = Path(os.environ.get("SHEET_MONITOR_DATA_DIR") or os.environ.get("DATA_DIR") or "data").expanduser()
DEFAULT_STATE_PATH = DEFAULT_DATA_DIR / "sheet_state.json"
DEFAULT_SHEETS_PATH = Path(__file__).resolve().parent / "sheets.json"
DEFAULT_INTERVAL_SECONDS = int(os.environ.get("SHEET_MONITOR_INTERVAL", "120"))
DEFAULT_DURATION_SECONDS = int(os.environ.get("SHEET_MONITOR_DURATION_SECONDS", "0"))
DEFAULT_NOTIFY_INITIAL = env_bool("SHEET_MONITOR_NOTIFY_INITIAL", False)
DEFAULT_STARTUP_MESSAGE = env_bool("SHEET_MONITOR_STARTUP_MESSAGE", False)
DEFAULT_MACOS_NOTIFICATIONS = env_bool("SHEET_MONITOR_MACOS_NOTIFICATIONS", True)
DEFAULT_ADMIN_BUTTONS = env_bool("SHEET_MONITOR_ADMIN_BUTTONS", True)
DEFAULT_PLAQUE_FORM = env_bool("PLAQUE_FORM_ENABLED", True)
USER_AGENT = "tg-pushes-ts26-sheet-monitor/1.0"
MAX_CHANGE_MESSAGES = 12
MAX_MACOS_BODY_LENGTH = 220
TELEGRAM_PARSE_MODE = "HTML"
DIFF_BOUNDARY_CHARS = " \t,.;:!?-–—()[]{}«»\"'"
PLAQUE_SPREADSHEET_ID = os.environ.get("PLAQUE_SPREADSHEET_ID", "1J6nJHM4wXF66LJO7dDNT6QgrxlQ5VPb-3B-4o7Ff0js")
PLAQUE_WORKSHEET_GID = int(os.environ.get("PLAQUE_WORKSHEET_GID", "1399617264"))
PLAQUE_WORKSHEET_TITLE = os.environ.get("PLAQUE_WORKSHEET_TITLE", "МОУШЕН")
PLAQUE_START_ROW = int(os.environ.get("PLAQUE_START_ROW", "280"))
PLAQUE_NAME_COL = int(os.environ.get("PLAQUE_NAME_COL", "1"))
PLAQUE_POSITION_COL = int(os.environ.get("PLAQUE_POSITION_COL", "2"))
PLAQUE_NOTE_COL = int(os.environ.get("PLAQUE_NOTE_COL", "5"))
PLAQUE_NOTE_TEXT = os.environ.get("PLAQUE_NOTE_TEXT", "<-- добавлено через ТГ бота")
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


def normalize_header(value):
    return normalize_space(value).casefold()


def google_sheet_export_url(url):
    text = str(url).strip()
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
    return "https://docs.google.com/spreadsheets/d/{}/export?format=tsv&gid={}".format(match.group(1), gid)


def count_rows(text):
    rows = list(csv.reader(text.splitlines(), delimiter="\t"))
    return len([row for row in rows if any(str(cell).strip() for cell in row)])


def parse_tsv(text):
    return list(csv.reader(text.splitlines(), delimiter="\t"))


def fetch_sheet(url, timeout):
    export_url = google_sheet_export_url(url)
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
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(str(tmp_path), str(path))


def parse_sheet_arg(value):
    if "=" in value:
        label, url = value.split("=", 1)
        return {"label": label.strip() or url.strip(), "url": url.strip()}
    return {"label": value.strip(), "url": value.strip()}


def normalize_sheet_config(item, index):
    if not isinstance(item, dict) or not item.get("url"):
        raise ConfigError("В sheets.json запись #{} должна содержать url.".format(index))
    clean = {"label": item.get("label") or item["url"], "url": item["url"]}
    if "chat_ids" in item:
        clean["chat_ids"] = [str(chat_id).strip() for chat_id in item.get("chat_ids") or [] if str(chat_id).strip()]
    if "extra_chat_ids" in item:
        clean["extra_chat_ids"] = [str(chat_id).strip() for chat_id in item.get("extra_chat_ids") or [] if str(chat_id).strip()]
    return clean


def load_sheets(args):
    if args.sheet:
        return [parse_sheet_arg(item) for item in args.sheet]
    sheets = load_json(Path(args.sheets).expanduser(), [])
    if not isinstance(sheets, list):
        raise ConfigError("Файл таблиц должен быть JSON-массивом.")
    return [normalize_sheet_config(item, index) for index, item in enumerate(sheets, 1)]


def sheet_key(sheet):
    return google_sheet_export_url(sheet["url"])


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


def recipient_chat_ids(sheet=None):
    sheet = sheet or {}
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


def known_service_chat_ids(sheets):
    known = set(admin_chat_ids())
    known.update(default_chat_ids())
    for sheet in sheets:
        known.update(recipient_chat_ids(sheet))
    return {str(item).strip() for item in known if str(item).strip()}


def send_telegram(args, title, message, subtitle="", url="", sheet=None):
    if args.no_telegram:
        log("Telegram выключен: {} - {}".format(title, message))
        return
    token = get_required_telegram_token()
    chat_ids = recipient_chat_ids(sheet)
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


def notify(args, title, message, subtitle="", url="", sheet=None):
    send_macos_notification(args, title, message, subtitle=subtitle)
    return try_send_telegram(args, title, message, subtitle=subtitle, url=url, sheet=sheet)


def try_send_telegram(args, title, message, subtitle="", url="", sheet=None):
    try:
        send_telegram(args, title, message, subtitle=subtitle, url=url, sheet=sheet)
        return True
    except (MonitorError, ConfigError) as exc:
        log("Не удалось отправить Telegram-сообщение: {}".format(exc))
        return False


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
    for raw_line in str(message or "").splitlines():
        line = normalize_space(raw_line)
        if not line:
            continue
        rendered.append(render_telegram_change_line(line))
    return "\n\n".join(rendered)


def render_telegram_change_line(line):
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

    return "• {}".format(h(line))


def admin_keyboard():
    return {
        "inline_keyboard": [
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
            [
                {"text": "Превью формы", "callback_data": "dbg:preview_plaque"},
                {"text": "Режим пользователя", "callback_data": "dbg:user_mode"},
            ],
        ]
    }


def plaque_keyboard():
    rows = [[{"text": "Добавить новую плашку", "callback_data": "plq:start"}]]
    return {"inline_keyboard": rows}


def plaque_user_mode_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "Добавить новую плашку", "callback_data": "plq:start"}],
            [{"text": "Вернуться в админку", "callback_data": "plq:admin_panel"}],
        ]
    }


def plaque_confirm_keyboard():
    return {
        "inline_keyboard": [
            [{"text": "Отправить в таблицу", "callback_data": "plq:confirm"}],
            [
                {"text": "Изменить имя", "callback_data": "plq:edit_name"},
                {"text": "Изменить должность", "callback_data": "plq:edit_position"},
            ],
            [{"text": "Отменить", "callback_data": "plq:cancel"}],
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


def recipients_report(sheets):
    lines = []
    lines.append("Админы: {}".format(", ".join(admin_chat_ids()) or "не заданы"))
    lines.append("Основные получатели: {}".format(", ".join(default_chat_ids()) or "не заданы"))
    for sheet in sheets:
        lines.append("{}: {}".format(sheet["label"], ", ".join(recipient_chat_ids(sheet)) or "не заданы"))
    return "\n".join(lines)


def status_report(args, sheets, state):
    active_user_modes = len(user_mode_chats(state))
    lines = [
        "Версия: {}".format(APP_VERSION),
        "Интервал: {} сек.".format(args.interval),
        "Длительность: {} сек.".format(args.duration) if args.duration else "Длительность: без ограничения",
        "Админ-кнопки: {}".format("включены" if not args.no_admin_buttons else "выключены"),
        "Форма плашек: {}".format("включена" if not args.no_plaque_form else "выключена"),
        "Пользовательский режим админов: {}".format(active_user_modes),
    ]
    for sheet in sheets:
        saved = state.get(sheet_key(sheet), {})
        checked = saved.get("checked_at") or "еще не проверялась"
        rows = saved.get("rows", "н/д")
        error = saved.get("error") or "нет"
        lines.append("{}: строк {}, проверка {}, ошибка: {}".format(sheet["label"], rows, checked, error))
    return "\n".join(lines)


def send_debug_menu(args, chat_id, sheets, state):
    message = "{}\n\n{}".format(status_report(args, sheets, state), recipients_report(sheets))
    send_admin_message(args, chat_id, "TS26: debug-панель", message, reply_markup=admin_keyboard())


def send_test_to_sheet(args, chat_id, sheet):
    message = "Тестовая отправка из debug-панели.\nПолучатели: {}".format(", ".join(recipient_chat_ids(sheet)) or "не заданы")
    try:
        send_telegram(args, "TS26: тест уведомления", message, subtitle=sheet["label"], url=sheet["url"], sheet=sheet)
        send_admin_message(args, chat_id, "TS26: тест отправлен", "Таблица: {}\nПолучатели: {}".format(sheet["label"], ", ".join(recipient_chat_ids(sheet)) or "не заданы"), reply_markup=admin_keyboard())
    except (MonitorError, ConfigError) as exc:
        send_admin_message(args, chat_id, "TS26: ошибка теста", "Таблица: {}\n{}".format(sheet["label"], exc), reply_markup=admin_keyboard())


def send_plaque_preview(args, chat_id):
    send_admin_message(args, chat_id, "TS26: превью обычного пользователя", "Ниже бот покажет, как форму видит обычный пользователь. Это только превью: Google Sheet не изменится.")
    send_plain_chat_message(args, chat_id, "TS26: плашка", "Здесь можно добавить или обновить плашку для моушена.\n\nБот попросит «Фамилия Имя» и «Должность», затем покажет подтверждение перед записью в таблицу.", reply_markup=plaque_keyboard())
    send_plain_chat_message(args, chat_id, "TS26: новая плашка", "Введите имя в формате:\nФамилия Имя")
    send_plain_chat_message(args, chat_id, "TS26: новая плашка", "Введите должность для плашки.")
    preview_state = {"_plaque_sessions": {str(chat_id): {"name": "Иванов Иван", "position": "директор подразделения"}}}
    send_plaque_confirmation(args, preview_state, chat_id)
    send_plain_chat_message(args, chat_id, "TS26: готово", "После подтверждения пользователь увидит примерно так:\n\nПлашка добавлена.\nСтрока: 280\nФИО: Иванов Иван\nДолжность: директор подразделения", reply_markup=admin_keyboard())


def send_user_mode_start(args, state, chat_id):
    set_user_mode_chat(state, chat_id, True)
    clear_plaque_session(state, chat_id)
    send_admin_message(
        args,
        chat_id,
        "TS26: режим пользователя",
        "Теперь этот чат работает как обычный пользователь формы. Можно пройти сценарий полностью, включая запись в Google Sheet после подтверждения.\n\nЧтобы вернуться в debug-панель, нажмите кнопку или отправьте /debug.",
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
    chat_id = chat.get("id") or (callback.get("from") or {}).get("id")
    data = callback.get("data") or ""
    if not data.startswith("dbg:"):
        return False
    if not is_admin_chat_id(chat_id):
        answer_callback(args, callback_id, "Нет доступа")
        return False
    answer_callback(args, callback_id)
    if data == "dbg:status":
        send_admin_message(args, chat_id, "TS26: статус", status_report(args, sheets, state), reply_markup=admin_keyboard())
    elif data == "dbg:recipients":
        send_admin_message(args, chat_id, "TS26: получатели", recipients_report(sheets), reply_markup=admin_keyboard())
    elif data == "dbg:test:startup":
        send_startup_message(args, sheets)
        send_admin_message(args, chat_id, "TS26: тест старта", "Стартовое сообщение отправлено основным получателям.", reply_markup=admin_keyboard())
    elif data == "dbg:google_access":
        try:
            report = google_access_report()
            send_admin_message(args, chat_id, "TS26: Google-доступ", report, reply_markup=admin_keyboard())
        except (MonitorError, ConfigError) as exc:
            send_admin_message(args, chat_id, "TS26: ошибка Google-доступа", str(exc), reply_markup=admin_keyboard())
    elif data == "dbg:preview_plaque":
        send_plaque_preview(args, chat_id)
    elif data == "dbg:user_mode":
        send_user_mode_start(args, state, chat_id)
    elif data.startswith("dbg:test:"):
        label = data.split(":", 2)[2]
        sheet = find_sheet_by_label(sheets, label)
        if sheet:
            send_test_to_sheet(args, chat_id, sheet)
        else:
            send_admin_message(args, chat_id, "TS26: ошибка", "Не нашел таблицу: {}".format(label), reply_markup=admin_keyboard())
    else:
        send_debug_menu(args, chat_id, sheets, state)
    return True


def handle_admin_message(args, sheets, state, message):
    if args.no_admin_buttons:
        return False
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = normalize_space(message.get("text") or "")
    if not text or not text.startswith("/"):
        return False
    if not is_admin_chat_id(chat_id):
        log("Команда от не-админа: chat_id={}, text={}".format(chat_id, text))
        return False
    command = text.split()[0].split("@", 1)[0].lower()
    if is_user_mode_chat(state, chat_id) and command in {"/start", "/add", "/plaque", "/cancel"}:
        return False
    if command in {"/add", "/plaque"}:
        send_user_mode_start(args, state, chat_id)
        ask_plaque_name(args, state, chat_id)
        return True
    if command in {"/start", "/debug"}:
        if is_user_mode_chat(state, chat_id):
            set_user_mode_chat(state, chat_id, False)
            clear_plaque_session(state, chat_id)
        send_debug_menu(args, chat_id, sheets, state)
    elif command == "/status":
        send_admin_message(args, chat_id, "TS26: статус", status_report(args, sheets, state), reply_markup=admin_keyboard())
    elif command == "/recipients":
        send_admin_message(args, chat_id, "TS26: получатели", recipients_report(sheets), reply_markup=admin_keyboard())
    elif command == "/preview_user":
        send_plaque_preview(args, chat_id)
    elif command in {"/user", "/user_mode", "/plaque_mode"}:
        send_user_mode_start(args, state, chat_id)
    elif command == "/google_access":
        try:
            report = google_access_report()
            send_admin_message(args, chat_id, "TS26: Google-доступ", report, reply_markup=admin_keyboard())
        except (MonitorError, ConfigError) as exc:
            send_admin_message(args, chat_id, "TS26: ошибка Google-доступа", str(exc), reply_markup=admin_keyboard())
    elif command == "/test_content":
        sheet = find_sheet_by_label(sheets, "Контент-план")
        if sheet:
            send_test_to_sheet(args, chat_id, sheet)
    elif command == "/test_recording":
        sheet = find_sheet_by_label(sheets, "План записи")
        if sheet:
            send_test_to_sheet(args, chat_id, sheet)
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
    if is_user_mode_chat(state, chat_id):
        return True
    return not (is_admin_chat_id(chat_id) or str(chat_id) in known_service_chat_ids(sheets))


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


def send_plaque_start(args, chat_id, state=None):
    message = "Здесь можно добавить или обновить плашку для моушена.\n\nБот попросит «Фамилия Имя» и «Должность», затем покажет подтверждение перед записью в таблицу."
    keyboard = plaque_user_mode_keyboard() if state is not None and is_user_mode_chat(state, chat_id) else plaque_keyboard()
    send_plain_chat_message(args, chat_id, "TS26: плашка", message, reply_markup=keyboard)


def ask_plaque_name(args, state, chat_id):
    plaque_session(state, chat_id).update({"step": "name"})
    send_plain_chat_message(args, chat_id, "TS26: новая плашка", "Введите имя в формате:\nФамилия Имя")


def ask_plaque_position(args, state, chat_id):
    plaque_session(state, chat_id).update({"step": "position"})
    send_plain_chat_message(args, chat_id, "TS26: новая плашка", "Введите должность для плашки.")


def send_plaque_confirmation(args, state, chat_id):
    session = plaque_session(state, chat_id)
    name = session.get("name", "")
    position = session.get("position", "")
    message = "Проверьте перед отправкой:\n\nФИО: {}\nДолжность: {}\n\nПосле подтверждения бот добавит или обновит строку в листе «Моушен».".format(name, position)
    session["step"] = "confirm"
    keyboard = plaque_confirm_user_mode_keyboard() if is_user_mode_chat(state, chat_id) else plaque_confirm_keyboard()
    send_plain_chat_message(args, chat_id, "TS26: проверьте плашку", message, reply_markup=keyboard)


def get_google_client():
    service_account_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    service_account_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip()
    oauth_user_json = os.environ.get("GOOGLE_OAUTH_USER_JSON", "").strip()
    oauth_user_file = os.environ.get("GOOGLE_OAUTH_USER_FILE", "").strip()
    if not any([service_account_json, service_account_file, oauth_user_json, oauth_user_file]):
        raise ConfigError("Для записи в Google Sheets задайте GOOGLE_SERVICE_ACCOUNT_JSON/FILE или GOOGLE_OAUTH_USER_JSON/FILE.")
    try:
        import gspread
        from google.oauth2.credentials import Credentials as UserCredentials
        from google.oauth2.service_account import Credentials
    except ImportError as exc:
        raise ConfigError("Не установлены зависимости для Google Sheets. Проверьте requirements.txt на хостинге: {}".format(exc))
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
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


def get_plaque_worksheet():
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


def verify_plaque_row(worksheet, row_index, name, position):
    row = run_google_action("Не удалось проверить записанную строку", lambda: worksheet.row_values(row_index))
    actual_name = normalize_space(plaque_cell_from_row(row, PLAQUE_NAME_COL))
    actual_position = normalize_space(plaque_cell_from_row(row, PLAQUE_POSITION_COL))
    actual_note = normalize_space(plaque_cell_from_row(row, PLAQUE_NOTE_COL))
    expected_note = normalize_space(PLAQUE_NOTE_TEXT)
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
                PLAQUE_NOTE_TEXT,
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


def write_plaque_to_sheet(name, position):
    worksheet = get_plaque_worksheet()
    values = run_google_action("Не удалось прочитать строки листа для плашек", worksheet.get_all_values)
    row_index, action = find_plaque_row(values, name)
    updates = [
        {"range": "{}{}".format(column_letter(PLAQUE_NAME_COL), row_index), "values": [[name]]},
        {"range": "{}{}".format(column_letter(PLAQUE_POSITION_COL), row_index), "values": [[position]]},
        {"range": "{}{}".format(column_letter(PLAQUE_NOTE_COL), row_index), "values": [[PLAQUE_NOTE_TEXT]]},
    ]
    log("Запись плашки: spreadsheet={}, worksheet='{}' gid={}, row={}, action={}, name='{}'".format(
        PLAQUE_SPREADSHEET_ID,
        getattr(worksheet, "title", ""),
        getattr(worksheet, "id", ""),
        row_index,
        action,
        name,
    ))
    run_google_action("Не удалось записать плашку в Google Sheets", lambda: worksheet.batch_update(updates, value_input_option="USER_ENTERED"))
    verified = verify_plaque_row(worksheet, row_index, name, position)
    return {
        "row": row_index,
        "action": action,
        "worksheet_title": getattr(worksheet, "title", "") or PLAQUE_WORKSHEET_TITLE,
        "worksheet_gid": getattr(worksheet, "id", PLAQUE_WORKSHEET_GID),
        "url": plaque_row_url(row_index),
        "verified": verified,
    }


def confirm_plaque(args, state, chat_id):
    session = plaque_session(state, chat_id)
    name = session.get("name")
    position = session.get("position")
    if not name or not position:
        ask_plaque_name(args, state, chat_id)
        return
    result = write_plaque_to_sheet(name, position)
    clear_plaque_session(state, chat_id)
    action_text = "обновлена" if result["action"] == "updated" else "добавлена"
    message = "Плашка {}.\nЛист: {} (gid={})\nСтрока: {}\nФИО: {}\nДолжность: {}\n{}".format(
        action_text,
        result["worksheet_title"],
        result["worksheet_gid"],
        result["row"],
        name,
        position,
        result["url"],
    )
    send_plain_chat_message(args, chat_id, "TS26: готово", message)
    for admin_id in admin_chat_ids():
        if str(admin_id) != str(chat_id):
            try:
                send_plain_chat_message(args, admin_id, "TS26: плашка через бота", message)
            except (MonitorError, ConfigError) as exc:
                log("Не удалось уведомить админа о плашке: {}".format(exc))


def handle_plaque_callback(args, sheets, state, callback):
    if args.no_plaque_form:
        return False
    callback_id = callback.get("id")
    message = callback.get("message") or {}
    chat = message.get("chat") or {}
    chat_id = chat.get("id") or (callback.get("from") or {}).get("id")
    data = callback.get("data") or ""
    if not data.startswith("plq:"):
        return False
    if data == "plq:admin_panel" and is_admin_chat_id(chat_id):
        answer_callback(args, callback_id)
        set_user_mode_chat(state, chat_id, False)
        clear_plaque_session(state, chat_id)
        send_debug_menu(args, chat_id, sheets, state)
        return True
    if not can_use_plaque_form(sheets, state, chat_id):
        answer_callback(args, callback_id, "Форма доступна новым пользователям")
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
        ask_plaque_name(args, state, chat_id)
    elif data == "plq:edit_position":
        ask_plaque_position(args, state, chat_id)
    elif data == "plq:cancel":
        clear_plaque_session(state, chat_id)
        keyboard = plaque_user_mode_keyboard() if is_user_mode_chat(state, chat_id) else plaque_keyboard()
        send_plain_chat_message(args, chat_id, "TS26: отменено", "Плашка не отправлена в таблицу.", reply_markup=keyboard)
    return True


def handle_plaque_message(args, sheets, state, message):
    if args.no_plaque_form:
        return False
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    text = normalize_space(message.get("text") or "")
    if not chat_id or not text:
        return False
    if not can_use_plaque_form(sheets, state, chat_id):
        return False
    session = plaque_sessions(state).get(str(chat_id), {})
    command = text.split()[0].split("@", 1)[0].lower() if text.startswith("/") else ""
    if command in {"/start", "/add", "/plaque"}:
        clear_plaque_session(state, chat_id)
        send_plaque_start(args, chat_id, state=state)
        return True
    if command == "/cancel":
        clear_plaque_session(state, chat_id)
        keyboard = plaque_user_mode_keyboard() if is_user_mode_chat(state, chat_id) else plaque_keyboard()
        send_plain_chat_message(args, chat_id, "TS26: отменено", "Плашка не отправлена в таблицу.", reply_markup=keyboard)
        return True
    step = session.get("step")
    if step == "name":
        try:
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
                changed = handle_admin_callback(args, sheets, state, callback) or changed
                changed = handle_plaque_callback(args, sheets, state, callback) or changed
            elif "message" in update:
                message = update["message"]
                changed = handle_admin_message(args, sheets, state, message) or changed
                changed = handle_plaque_message(args, sheets, state, message) or changed
        except (MonitorError, ConfigError) as exc:
            log("Ошибка обработки Telegram-команды: {}".format(exc))
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


def build_change_messages(sheet_label, previous_rows, current_rows):
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
            if len(messages) >= MAX_CHANGE_MESSAGES:
                return messages

    for key in previous_by_key:
        if key not in current_by_key:
            messages.append("{}: удалена строка «{}».".format(sheet_label, row_identity(previous_rows, headers, previous_by_key[key], key_col)))
            if len(messages) >= MAX_CHANGE_MESSAGES:
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
            if len(messages) >= MAX_CHANGE_MESSAGES:
                return messages

    for row_index in current_fallback[paired_fallback:]:
        messages.append("{}: добавлена строка «{}».".format(sheet_label, row_identity(current_rows, headers, row_index, key_col)))
        if len(messages) >= MAX_CHANGE_MESSAGES:
            return messages
    for row_index in previous_fallback[paired_fallback:]:
        messages.append("{}: удалена строка «{}».".format(sheet_label, row_identity(previous_rows, headers, row_index, key_col)))
        if len(messages) >= MAX_CHANGE_MESSAGES:
            return messages

    return messages


def build_change_summary(sheet_label, previous, current):
    previous_rows = previous.get("cells") or []
    current_rows = current.get("cells") or []
    messages = build_change_messages(sheet_label, previous_rows, current_rows)
    if messages:
        hidden_count = max(0, estimate_changed_cells(previous_rows, current_rows) - len(messages))
        if hidden_count > 0:
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
    current = fetch_sheet(sheet["url"], args.timeout)
    current.update({
        "label": label,
        "url": sheet["url"],
        "checked_at": now_text(),
        "error": "",
    })

    old_hash = previous.get("hash")
    if old_hash and old_hash != current["hash"]:
        message = build_change_summary(label, previous, current)
        log("Обновление: {} ({})".format(label, message.splitlines()[0] if message else "есть изменения"))
        notify(args, "TS26: обновилась таблица", message, subtitle=label, url=sheet["url"], sheet=sheet)
    elif not old_hash:
        log("Первый снимок: {} (строк: {}, {} байт)".format(label, current["rows"], current["bytes"]))
        if args.notify_initial:
            notify(args, "TS26: монитор запущен", "Первый снимок сохранен; строк: {}".format(current["rows"]), subtitle=label, url=sheet["url"], sheet=sheet)
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
                notify(args, "TS26: ошибка монитора", message, subtitle=sheet["label"], url=sheet["url"], sheet=sheet)
            previous.update({
                "label": sheet["label"],
                "url": sheet["url"],
                "checked_at": now_text(),
                "error": message,
            })
            state[key] = previous
            changed_state = True
    return changed_state


def send_startup_message(args, sheets):
    labels = ", ".join([sheet["label"] for sheet in sheets])
    message = "Бот запущен. Отслеживается таблиц: {}. Интервал проверки: {} сек.".format(len(sheets), args.interval)
    if labels:
        message = "{}\n{}".format(message, labels)
    notify(args, "TS26: монитор активен", message)


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
    state = load_state(state_path)

    started_at = time.monotonic()
    duration_text = ", длительность {} сек.".format(args.duration) if args.duration else ""
    log("Старт монитора v{}: {} таблиц, интервал {} сек.{}".format(APP_VERSION, len(sheets), args.interval, duration_text))
    log("Файл состояния: {}".format(state_path))
    log("Основные Telegram chat_id: {}".format(", ".join(default_chat_ids()) or "не заданы"))
    for sheet in sheets:
        log("Получатели для {}: {}".format(sheet["label"], ", ".join(recipient_chat_ids(sheet)) or "не заданы"))
    if args.startup_message:
        send_startup_message(args, sheets)
    if poll_admin_updates(args, sheets, state):
        save_state(state_path, state)
    while True:
        if check_all(sheets, state, args):
            save_state(state_path, state)
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
            if poll_admin_updates(args, sheets, state):
                save_state(state_path, state)
            time.sleep(min(5, remaining))


if __name__ == "__main__":
    try:
        main()
    except ConfigError as exc:
        raise SystemExit(str(exc))
