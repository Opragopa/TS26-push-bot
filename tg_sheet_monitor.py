#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Poll Google Sheets and send Telegram notifications on content changes."""

import argparse
import csv
import datetime as _dt
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


APP_NAME = "tg-pushes-TS26"
DEFAULT_DATA_DIR = Path.home() / "Documents" / "tg_sheet_monitor"
DEFAULT_STATE_PATH = DEFAULT_DATA_DIR / "sheet_state.json"
DEFAULT_SHEETS_PATH = Path(__file__).resolve().parent / "sheets.json"
DEFAULT_INTERVAL_SECONDS = 120
USER_AGENT = "tg-pushes-ts26-sheet-monitor/1.0"
MAX_CHANGE_MESSAGES = 12
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


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


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


def send_telegram(args, title, message, subtitle="", url="", sheet=None):
    if args.no_telegram:
        log("Telegram выключен: {} - {}".format(title, message))
        return
    token = get_required_telegram_token()
    chat_ids = recipient_chat_ids(sheet)
    if not chat_ids:
        raise ConfigError("Заполните TELEGRAM_CHAT_ID или TELEGRAM_CHAT_IDS в .env/окружении. chat_id можно узнать через --print-chat-ids.")
    lines = ["*{}*".format(telegram_escape(title))]
    if subtitle:
        lines.append(telegram_escape(subtitle))
    lines.append(telegram_escape(message))
    if url:
        lines.append(telegram_escape(url))
    errors = []
    for chat_id in chat_ids:
        payload = {
            "chat_id": chat_id,
            "text": "\n".join(lines),
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": "true" if env_bool("TELEGRAM_DISABLE_WEB_PAGE_PREVIEW", True) else "false",
        }
        try:
            telegram_request(token, "sendMessage", payload, args.timeout)
        except MonitorError as exc:
            errors.append("{}: {}".format(chat_id, exc))
    if errors:
        raise MonitorError("; ".join(errors))


def try_send_telegram(args, title, message, subtitle="", url="", sheet=None):
    try:
        send_telegram(args, title, message, subtitle=subtitle, url=url, sheet=sheet)
        return True
    except (MonitorError, ConfigError) as exc:
        log("Не удалось отправить Telegram-сообщение: {}".format(exc))
        return False


def telegram_escape(value):
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", str(value))


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


def describe_grid_change(sheet_label, row_name, header, old_value, new_value):
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
        for col_index, header in enumerate(headers):
            old_value = cell(previous_rows, old_index, col_index)
            new_value = cell(current_rows, new_index, col_index)
            if old_value == new_value:
                continue
            if people_table:
                messages.append(describe_cell_change(sheet_label, row_name, header, old_value, new_value))
            else:
                messages.append(describe_grid_change(sheet_label, row_name, header, old_value, new_value))
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
        for col_index, header in enumerate(headers):
            old_value = cell(previous_rows, old_index, col_index)
            new_value = cell(current_rows, new_index, col_index)
            if old_value == new_value:
                continue
            messages.append(describe_grid_change(sheet_label, row_name, header, old_value, new_value))
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
        try_send_telegram(args, "Обновилась Google Sheet", message, subtitle=label, url=sheet["url"], sheet=sheet)
    elif not old_hash:
        log("Первый снимок: {} (строк: {}, {} байт)".format(label, current["rows"], current["bytes"]))
        if args.notify_initial:
            try_send_telegram(args, "Монитор Google Sheets запущен", "Первый снимок сохранен; строк: {}".format(current["rows"]), subtitle=label, url=sheet["url"], sheet=sheet)
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
                try_send_telegram(args, "Ошибка монитора Google Sheets", message, subtitle=sheet["label"], url=sheet["url"], sheet=sheet)
            previous.update({
                "label": sheet["label"],
                "url": sheet["url"],
                "checked_at": now_text(),
                "error": message,
            })
            state[key] = previous
            changed_state = True
    return changed_state


def build_parser():
    parser = argparse.ArgumentParser(
        description="Фоново проверяет Google Sheets и отправляет Telegram-уведомления при изменениях."
    )
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL_SECONDS, help="Интервал проверки в секундах. По умолчанию: %(default)s.")
    parser.add_argument("--timeout", type=int, default=30, help="Таймаут HTTP-запросов в секундах.")
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH), help="JSON-файл состояния. По умолчанию: %(default)s.")
    parser.add_argument("--sheets", default=str(DEFAULT_SHEETS_PATH), help="JSON-файл со списком таблиц.")
    parser.add_argument("--sheet", action="append", default=[], help='Таблица: "Название=https://docs.google.com/...". Если задано, заменяет sheets.json.')
    parser.add_argument("--env", default=".env", help="Файл с TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID.")
    parser.add_argument("--once", action="store_true", help="Проверить один раз и выйти.")
    parser.add_argument("--notify-initial", action="store_true", help="Отправить Telegram-сообщение при первом сохранении снимка.")
    parser.add_argument("--print-chat-ids", action="store_true", help="Показать chat_id из последних сообщений боту и выйти.")
    parser.add_argument("--no-telegram", action="store_true", help="Не отправлять Telegram-сообщения, только писать лог.")
    parser.add_argument("--quiet", action="store_true", help="Не писать в лог проверки без изменений.")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.interval < 15:
        raise SystemExit("Интервал меньше 15 секунд слишком агрессивен для Google Sheets.")
    load_dotenv(args.env)
    if args.print_chat_ids:
        print_chat_ids(args)
        return
    sheets = load_sheets(args)
    if not sheets:
        raise SystemExit("Добавьте хотя бы одну таблицу в sheets.json или через --sheet.")
    state_path = Path(args.state).expanduser()
    state = load_state(state_path)

    log("Старт монитора: {} таблиц, интервал {} сек.".format(len(sheets), args.interval))
    while True:
        if check_all(sheets, state, args):
            save_state(state_path, state)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except ConfigError as exc:
        raise SystemExit(str(exc))
