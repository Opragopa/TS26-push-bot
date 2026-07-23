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
from zoneinfo import ZoneInfo


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


APP_NAME = "tg-pushes-TS26"
APP_VERSION = "2026-07-23.02"
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
MAX_TELEGRAM_MESSAGE_CHARS = 3800
CONTENT_PLAN_DIGEST_STATE_KEY = "_content_plan_hourly_digest"
CONTENT_PLAN_TIME_ZONE = ZoneInfo("Europe/Moscow")
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
GROQ_CHAT_COMPLETIONS_URL = "https://api.groq.com/openai/v1/chat/completions"
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
    if sheet.get("chat_ids"):
        chat_ids = list(sheet["chat_ids"])
    else:
        chat_ids = default_chat_ids()
        chat_ids.extend(sheet.get("extra_chat_ids") or [])
        if state is not None and is_content_plan_sheet(sheet):
            chat_ids.extend(content_plan_chat_ids(state))
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
        message = choices[0].get("message") or {}
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    raise MonitorError("Groq не вернул текст сводки.")


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


def flush_content_plan_digest(args, sheets, state, moment=None):
    """Send one Content Plan package after an hour boundary, retaining failures."""
    digest = content_plan_digest_state(state)
    current_hour = content_plan_hour_key(moment)
    events = digest.get("events") or []
    last_flush_hour = digest.get("last_flush_hour")

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
    try:
        ai_summary = build_ai_content_plan_summary(full_diff, args.timeout)
        summary_block = "Коротко за час:\n{}".format(ai_summary)
        log("AI-сводка Контент-плана готова: событий {}, строк diff {}.".format(event_count, change_count))
    except (MonitorError, ConfigError) as exc:
        summary_block = "Коротко за час: AI-сводка недоступна, ниже полный diff."
        log("AI-сводка Контент-плана не получена: {}".format(exc))

    message = "{}\n\nПолный diff:\n{}".format(summary_block, full_diff)
    try:
        send_macos_notification(args, "TS26: обновления за час", summary_block, subtitle=content_sheet["label"])
        chunks = send_telegram_chunks_to_chat_ids(
            args,
            recipient_chat_ids(content_sheet, state=state),
            "TS26: обновления за час",
            message,
            subtitle=content_sheet["label"],
            url=content_sheet["url"],
        )
    except (MonitorError, ConfigError) as exc:
        log("Не удалось отправить почасовой пакет Контент-плана: {}".format(exc))
        return False

    digest["events"] = []
    digest["last_flush_hour"] = current_hour
    digest["last_sent_at"] = now_text()
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
                {"text": "Контент-доступ", "callback_data": "dbg:content_access"},
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
            [
                {"text": "Стартовый экран", "callback_data": "dbg:start_screen"},
            ],
        ]
    }


PLAQUE_ADD_BUTTON_TEXT = "Добавить новую плашку"


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
        "keyboard": [[{"text": PLAQUE_ADD_BUTTON_TEXT}]],
        "resize_keyboard": True,
        "is_persistent": True,
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


def recipients_report(sheets, state=None):
    lines = []
    lines.append("Админы: {}".format(", ".join(admin_chat_ids()) or "не заданы"))
    lines.append("Основные получатели: {}".format(", ".join(default_chat_ids()) or "не заданы"))
    if state is not None:
        lines.append("Контент-план через бота: {}".format(", ".join(content_plan_chat_ids(state)) or "не добавлены"))
    for sheet in sheets:
        lines.append("{}: {}".format(sheet["label"], ", ".join(recipient_chat_ids(sheet, state=state)) or "не заданы"))
    return "\n".join(lines)


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
        "/add_content_user 415835819\n\n"
        "Удалить:\n"
        "/remove_content_user 415835819\n\n"
        "Показать список:\n"
        "/content_users\n\n"
        "Человек должен хотя бы один раз написать боту, иначе Telegram может запретить отправку."
    ).format(", ".join(chat_ids) or "не добавлены", "\n".join(recent_lines) or "пока нет")


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
    message = "{}\n\n{}".format(status_report(args, sheets, state), recipients_report(sheets, state=state))
    send_admin_message(args, chat_id, "TS26: debug-панель", message, reply_markup=admin_keyboard())


def send_test_to_sheet(args, chat_id, sheet, state=None):
    message = "Тестовая отправка из debug-панели.\nПолучатели: {}".format(", ".join(recipient_chat_ids(sheet, state=state)) or "не заданы")
    try:
        send_telegram(args, "TS26: тест уведомления", message, subtitle=sheet["label"], url=sheet["url"], sheet=sheet, state=state)
        send_admin_message(args, chat_id, "TS26: тест отправлен", "Таблица: {}\nПолучатели: {}".format(sheet["label"], ", ".join(recipient_chat_ids(sheet, state=state)) or "не заданы"), reply_markup=admin_keyboard())
    except (MonitorError, ConfigError) as exc:
        send_admin_message(args, chat_id, "TS26: ошибка теста", "Таблица: {}\n{}".format(sheet["label"], exc), reply_markup=admin_keyboard())


def start_screen_text(is_content_recipient=False):
    lines = [
        "Я бот TS26.",
        "",
        "Что умею:",
        "• присылать уведомления об изменениях в Контент-плане;",
        "• помочь добавить или обновить плашку для моушена;",
        "• перед отправкой плашки показать проверку данных.",
        "",
        "Чтобы добавить плашку, нажмите кнопку ниже.",
    ]
    if is_content_recipient:
        lines.extend(["", "Вы добавлены в уведомления Контент-плана."])
    return "\n".join(lines)


def send_start_screen(args, chat_id, state=None, is_content_recipient=False):
    send_plain_chat_message(args, chat_id, "TS26: старт", start_screen_text(is_content_recipient=is_content_recipient), reply_markup=plaque_reply_keyboard())


def send_plaque_preview(args, chat_id):
    send_admin_message(args, chat_id, "TS26: превью обычного пользователя", "Ниже бот покажет, как форму видит обычный пользователь. Это только превью: Google Sheet не изменится.")
    send_start_screen(args, chat_id)
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
        send_admin_message(args, chat_id, "TS26: получатели", recipients_report(sheets, state=state), reply_markup=admin_keyboard())
    elif data == "dbg:content_access":
        send_admin_message(args, chat_id, "TS26: Контент-доступ", content_access_report(state), reply_markup=admin_keyboard())
    elif data == "dbg:test:startup":
        send_startup_message(args, sheets, state=state)
        send_admin_message(args, chat_id, "TS26: тест старта", "Стартовое сообщение отправлено основным получателям.", reply_markup=admin_keyboard())
    elif data == "dbg:google_access":
        try:
            report = google_access_report()
            send_admin_message(args, chat_id, "TS26: Google-доступ", report, reply_markup=admin_keyboard())
        except (MonitorError, ConfigError) as exc:
            send_admin_message(args, chat_id, "TS26: ошибка Google-доступа", str(exc), reply_markup=admin_keyboard())
    elif data == "dbg:preview_plaque":
        send_plaque_preview(args, chat_id)
    elif data == "dbg:start_screen":
        send_start_screen(args, chat_id, state=state, is_content_recipient=str(chat_id) in content_plan_chat_ids(state))
    elif data == "dbg:user_mode":
        send_user_mode_start(args, state, chat_id)
    elif data.startswith("dbg:test:"):
        label = data.split(":", 2)[2]
        sheet = find_sheet_by_label(sheets, label)
        if sheet:
            send_test_to_sheet(args, chat_id, sheet, state=state)
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
        send_admin_message(args, chat_id, "TS26: получатели", recipients_report(sheets, state=state), reply_markup=admin_keyboard())
    elif command == "/content_users":
        send_admin_message(args, chat_id, "TS26: Контент-доступ", content_access_report(state), reply_markup=admin_keyboard())
    elif command in {"/add_content_user", "/remove_content_user"}:
        parts = text.split()
        if len(parts) < 2:
            send_admin_message(args, chat_id, "TS26: Контент-доступ", "Укажите chat_id.\nНапример:\n{} 415835819".format(command), reply_markup=admin_keyboard())
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
    elif command == "/preview_user":
        send_plaque_preview(args, chat_id)
    elif command == "/start_screen":
        send_start_screen(args, chat_id, state=state, is_content_recipient=str(chat_id) in content_plan_chat_ids(state))
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
    if is_user_mode_chat(state, chat_id):
        return True
    return not (is_admin_chat_id(chat_id) or str(chat_id) in known_service_chat_ids(sheets, state=state))


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
    entries = session.get("entries")
    if isinstance(entries, list) and entries:
        results = []
        for entry in entries:
            result = write_plaque_to_sheet(entry["name"], entry["position"])
            results.append({"entry": entry, "result": result})
        clear_plaque_session(state, chat_id)
        created_count = sum(1 for item in results if item["result"]["action"] == "created")
        updated_count = sum(1 for item in results if item["result"]["action"] == "updated")
        public_lines = [
            "Плашки отправлены в таблицу.",
            "Добавлено: {}. Обновлено: {}.".format(created_count, updated_count),
            "",
        ]
        for index, item in enumerate(results, start=1):
            action_text = "обновлена" if item["result"]["action"] == "updated" else "добавлена"
            entry = item["entry"]
            public_lines.append("{}. {}: {} — {}".format(index, action_text.capitalize(), entry["name"], entry["position"]))
        admin_lines = [
            "Пакетная отправка плашек.",
            "Добавлено: {}. Обновлено: {}.".format(created_count, updated_count),
            "",
        ]
        for index, item in enumerate(results, start=1):
            action_text = "обновлена" if item["result"]["action"] == "updated" else "добавлена"
            entry = item["entry"]
            result = item["result"]
            admin_lines.append(
                "{}. Плашка {}.\nЛист: {} (gid={})\nСтрока: {}\nФИО: {}\nДолжность: {}\n{}".format(
                    index,
                    action_text,
                    result["worksheet_title"],
                    result["worksheet_gid"],
                    result["row"],
                    entry["name"],
                    entry["position"],
                    result["url"],
                )
            )
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
    clear_plaque_session(state, chat_id)
    action_text = "обновлена" if result["action"] == "updated" else "добавлена"
    public_message = "Плашка {}.\nФИО: {}\nДолжность: {}".format(action_text, name, position)
    admin_message = "Плашка {}.\nЛист: {} (gid={})\nСтрока: {}\nФИО: {}\nДолжность: {}\n{}".format(
        action_text,
        result["worksheet_title"],
        result["worksheet_gid"],
        result["row"],
        name,
        position,
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
    chat_id = chat.get("id")
    raw_text = (message.get("text") or "").strip()
    text = normalize_space(raw_text)
    if not chat_id or not text:
        return False
    if not can_use_plaque_form(sheets, state, chat_id):
        return False
    session = plaque_sessions(state).get(str(chat_id), {})
    command = text.split()[0].split("@", 1)[0].lower() if text.startswith("/") else ""
    if command == "/start":
        clear_plaque_session(state, chat_id)
        send_start_screen(args, chat_id, state=state, is_content_recipient=str(chat_id) in content_plan_chat_ids(state))
        return True
    if command in {"/add", "/plaque"} or text.casefold() == PLAQUE_ADD_BUTTON_TEXT.casefold():
        clear_plaque_session(state, chat_id)
        send_plaque_start(args, chat_id, state=state)
        ask_plaque_name(args, state, chat_id)
        return True
    if command == "/cancel":
        clear_plaque_session(state, chat_id)
        send_plain_chat_message(args, chat_id, "TS26: отменено", "Плашка не отправлена в таблицу.", reply_markup=plaque_reply_keyboard())
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
                changed = handle_admin_callback(args, sheets, state, callback) or changed
                changed = handle_plaque_callback(args, sheets, state, callback) or changed
            elif "message" in update:
                message = update["message"]
                changed = remember_chat(state, message.get("chat") or {}) or changed
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
    current = fetch_sheet(sheet["url"], args.timeout)
    current.update({
        "label": label,
        "url": sheet["url"],
        "checked_at": now_text(),
        "error": "",
    })

    old_hash = previous.get("hash")
    if old_hash and old_hash != current["hash"]:
        is_content_plan = is_content_plan_sheet(sheet)
        message = build_change_summary(label, previous, current, full_diff=is_content_plan)
        log("Обновление: {} ({})".format(label, message.splitlines()[0] if message else "есть изменения"))
        if is_content_plan:
            queue_size = queue_content_plan_change(state, message, captured_at=current["checked_at"])
            log("Изменение Контент-плана добавлено в почасовую очередь: событий {}.".format(queue_size))
        else:
            notify(args, "TS26: обновилась таблица", message, subtitle=label, url=sheet["url"], sheet=sheet, state=state)
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
    state = load_state(state_path)

    started_at = time.monotonic()
    duration_text = ", длительность {} сек.".format(args.duration) if args.duration else ""
    log("Старт монитора v{}: {} таблиц, интервал {} сек.{}".format(APP_VERSION, len(sheets), args.interval, duration_text))
    log("Файл состояния: {}".format(state_path))
    log("Основные Telegram chat_id: {}".format(", ".join(default_chat_ids()) or "не заданы"))
    for sheet in sheets:
        log("Получатели для {}: {}".format(sheet["label"], ", ".join(recipient_chat_ids(sheet, state=state)) or "не заданы"))
    if args.startup_message:
        send_startup_message(args, sheets, state=state)
    if poll_admin_updates(args, sheets, state):
        save_state(state_path, state)
    while True:
        if flush_content_plan_digest(args, sheets, state):
            save_state(state_path, state)
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
            if flush_content_plan_digest(args, sheets, state):
                save_state(state_path, state)
            if poll_admin_updates(args, sheets, state):
                save_state(state_path, state)
            time.sleep(min(5, remaining))


if __name__ == "__main__":
    try:
        main()
    except ConfigError as exc:
        raise SystemExit(str(exc))
