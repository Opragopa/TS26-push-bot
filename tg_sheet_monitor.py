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
    return {
        "hash": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "rows": count_rows(text),
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


def load_sheets(args):
    if args.sheet:
        return [parse_sheet_arg(item) for item in args.sheet]
    sheets = load_json(Path(args.sheets).expanduser(), [])
    if not isinstance(sheets, list):
        raise ConfigError("Файл таблиц должен быть JSON-массивом.")
    clean = []
    for index, item in enumerate(sheets, 1):
        if not isinstance(item, dict) or not item.get("url"):
            raise ConfigError("В sheets.json запись #{} должна содержать url.".format(index))
        clean.append({"label": item.get("label") or item["url"], "url": item["url"]})
    return clean


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


def send_telegram(args, title, message, subtitle="", url=""):
    if args.no_telegram:
        log("Telegram выключен: {} - {}".format(title, message))
        return
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        raise ConfigError("Заполните TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env или окружении.")
    lines = ["*{}*".format(telegram_escape(title))]
    if subtitle:
        lines.append(telegram_escape(subtitle))
    lines.append(telegram_escape(message))
    if url:
        lines.append(telegram_escape(url))
    payload = {
        "chat_id": chat_id,
        "text": "\n".join(lines),
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": "true" if env_bool("TELEGRAM_DISABLE_WEB_PAGE_PREVIEW", True) else "false",
    }
    telegram_request(token, "sendMessage", payload, args.timeout)


def telegram_escape(value):
    return re.sub(r"([_*\[\]()~`>#+\-=|{}.!])", r"\\\1", str(value))


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
        old_rows = previous.get("rows")
        row_text = "строк: {} -> {}".format(old_rows, current["rows"]) if old_rows is not None else "строк: {}".format(current["rows"])
        message = "{}; размер: {} байт".format(row_text, current["bytes"])
        log("Обновление: {} ({})".format(label, message))
        send_telegram(args, "Обновилась Google Sheet", message, subtitle=label, url=sheet["url"])
    elif not old_hash:
        log("Первый снимок: {} (строк: {}, {} байт)".format(label, current["rows"], current["bytes"]))
        if args.notify_initial:
            send_telegram(args, "Монитор Google Sheets запущен", "Первый снимок сохранен; строк: {}".format(current["rows"]), subtitle=label, url=sheet["url"])
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
                send_telegram(args, "Ошибка монитора Google Sheets", message, subtitle=sheet["label"], url=sheet["url"])
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
    parser.add_argument("--no-telegram", action="store_true", help="Не отправлять Telegram-сообщения, только писать лог.")
    parser.add_argument("--quiet", action="store_true", help="Не писать в лог проверки без изменений.")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.interval < 15:
        raise SystemExit("Интервал меньше 15 секунд слишком агрессивен для Google Sheets.")
    load_dotenv(args.env)
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
