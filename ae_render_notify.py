#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Send render outcomes from the local AE worker back to Telegram.

The worker runs detached from the bot (separate launchd agent, no shared process),
so without this module a failed render is only ever visible in a log file on the
Mac. The person who asked for the plaque sees "поставлено в очередь" and then
silence, which is indistinguishable from the bot being broken.

Deliberately dependency-free and best-effort: a notification failure must never
turn into a render failure.
"""

import html
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


TELEGRAM_API = "https://api.telegram.org/bot{}/{}"
MAX_MESSAGE_CHARS = 3500
DEFAULT_TIMEOUT = 15


def load_env_file(path):
    """Load .env values without overriding real environment variables."""
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name and name not in os.environ:
            os.environ[name] = value.strip().strip('"').strip("'")


def split_ids(value):
    return [item.strip() for item in str(value or "").replace(";", ",").replace(" ", ",").split(",") if item.strip()]


def admin_chat_ids():
    configured = split_ids(os.environ.get("TELEGRAM_ADMIN_CHAT_IDS", ""))
    if configured:
        return configured
    ids = split_ids(os.environ.get("TELEGRAM_CHAT_IDS", ""))
    single = str(os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
    if single and single not in ids:
        ids.insert(0, single)
    return ids


def notify_enabled():
    value = str(os.environ.get("AE_RENDER_NOTIFY_TELEGRAM", "true")).strip().lower()
    return value in {"1", "true", "yes", "y", "on"}


def send_message(chat_id, text, timeout=DEFAULT_TIMEOUT):
    token = str(os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
    if not token:
        return False
    payload = urllib.parse.urlencode({
        "chat_id": str(chat_id),
        "text": text[:MAX_MESSAGE_CHARS],
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    request = urllib.request.Request(TELEGRAM_API.format(token, "sendMessage"), data=payload)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8")).get("ok") is True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def recipients_for(job):
    """Notify whoever requested the render, plus the admins.

    Jobs created by the sheet poller have no requester, so they go to admins only.
    """
    ids = []
    requested_by = str((job.get("payload") or {}).get("requested_by") or "").strip()
    if requested_by:
        ids.append(requested_by)
    for chat_id in admin_chat_ids():
        if chat_id not in ids:
            ids.append(chat_id)
    return ids


def h(value):
    return html.escape(str(value or ""), quote=False)


def describe_job(job):
    payload = job.get("payload") or {}
    kind = job.get("kind")
    if kind == "plaque":
        return "{} — {}".format(payload.get("name", ""), payload.get("position", ""))
    if kind == "session_topic":
        return "День {} · {} · {}".format(payload.get("day", ""), payload.get("shift", ""), payload.get("topic", ""))
    return str(payload.get("name") or job.get("id", ""))


def hint_for_error(error_text):
    """Turn the worker's internal error into an actionable instruction."""
    text = str(error_text or "")
    if "требует открытый проект" in text or "Откройте в After Effects проект" in text:
        return (
            "Откройте в After Effects нужный проект (путь указан выше) и повторите — "
            "или отправьте /render_retry, когда проект будет открыт."
        )
    if "unexpected error occurred while exporting" in text:
        return (
            "After Effects не смог экспортировать композицию. Проверьте Output Module "
            "и что папка назначения доступна для записи."
        )
    if "рендерит другое задание" in text:
        return "After Effects занят другим рендером — задание вернётся в очередь автоматически."
    return "Повторить постановку в очередь можно командой /render_retry."


def notify_job_finished(job, output_path=""):
    """Announce a successful render."""
    if not notify_enabled():
        return
    text = "<b>TS26: рендер готов</b>\n\n{}".format(h(describe_job(job)))
    if output_path:
        text += "\n\nФайл:\n<code>{}</code>".format(h(output_path))
    for chat_id in recipients_for(job):
        send_message(chat_id, text)


def notify_job_failed(job, error_text):
    """Announce a failed render together with what to do about it."""
    if not notify_enabled():
        return
    text = "<b>TS26: рендер не удался</b>\n\n{}\n\n<b>Причина:</b>\n{}\n\n{}".format(
        h(describe_job(job)),
        h(str(error_text)[:900]),
        h(hint_for_error(error_text)),
    )
    for chat_id in recipients_for(job):
        send_message(chat_id, text)


def safe_notify(action, *args, **kwargs):
    """Run a notifier without ever letting it break the render loop."""
    try:
        action(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - notification must stay best effort
        print("[worker] не удалось отправить уведомление в Telegram: {!r}".format(exc), flush=True)
