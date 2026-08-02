#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Check the whole plaque pipeline: Telegram bot -> queue -> worker -> After Effects.

Run this on the Mac that renders:

    python3 ae_render_doctor.py

Every check prints OK / ВНИМАНИЕ / ОШИБКА plus what to do about it, so a broken
render can be diagnosed without reading logs from three different services.
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import ae_render_notify
import ae_render_queue


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = SCRIPT_DIR / "ae_render_config.json"

OK = "OK"
WARN = "ВНИМАНИЕ"
FAIL = "ОШИБКА"

_results = []


def report(level, title, detail="", fix=""):
    _results.append(level)
    print("[{}] {}".format(level, title))
    if detail:
        for line in str(detail).splitlines():
            print("        {}".format(line))
    if fix:
        print("        -> {}".format(fix))
    print()


def process_exists(name):
    try:
        return subprocess.run(["pgrep", "-qx", name], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, check=False).returncode == 0
    except OSError:
        return False


def launchagent_loaded(label):
    try:
        result = subprocess.run(["launchctl", "list"], capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return label in (result.stdout or "")


def check_config(config_path):
    if not config_path.exists():
        report(FAIL, "Конфиг рендера не найден", str(config_path),
               "Создайте ae_render_config.json рядом с ae_render_worker.py.")
        return None
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        report(FAIL, "Конфиг рендера — невалидный JSON", str(exc), "Проверьте синтаксис файла.")
        return None
    report(OK, "Конфиг рендера прочитан", str(config_path))
    return config


def check_paths(config):
    checks = [
        ("Проект After Effects", config.get("project_path"), True),
        ("Бинарь After Effects", config.get("afterfx_bin"), True),
        ("aerender", config.get("aerender_bin"), False),
        ("JSX плашек", config.get("person_plates_script_path"), True),
        ("JSX тем сессий", config.get("session_topics_script_path"), False),
        ("Google OAuth файл", config.get("google_oauth_user_file"), True),
    ]
    for label, value, critical in checks:
        text = str(value or "").strip()
        if not text:
            report(WARN, "{}: путь не задан".format(label), fix="Заполните поле в ae_render_config.json.")
            continue
        if Path(text).expanduser().exists():
            report(OK, label, text)
        else:
            report(FAIL if critical else WARN, "{}: файл не найден".format(label), text,
                   "Проверьте путь — возможно, проект переместили или переименовали.")

    output_dir = ((config.get("routes") or {}).get("plaque_output_dir") or "").strip()
    if output_dir:
        path = Path(output_dir).expanduser()
        if not path.exists():
            report(FAIL, "Папка вывода плашек не существует", str(path),
                   "Создайте её или поправьте routes.plaque_output_dir.")
        elif not os.access(str(path), os.W_OK):
            report(FAIL, "Папка вывода плашек недоступна для записи", str(path),
                   "Проверьте права или что Яндекс.Диск смонтирован.")
        else:
            report(OK, "Папка вывода плашек доступна для записи", str(path))


def check_templates():
    for name in ("ae_prepare_project.jsx.template", "ae_render_open_queue.jsx.template"):
        path = SCRIPT_DIR / "ae_templates" / name
        if path.exists():
            report(OK, "Шаблон {}".format(name))
        else:
            report(FAIL, "Нет шаблона {}".format(name), str(path),
                   "Восстановите каталог ae_templates из репозитория.")


def check_after_effects(config):
    if not process_exists("After Effects"):
        report(FAIL, "After Effects не запущен", fix="Запустите AE и откройте проект для плашек.")
        return
    report(OK, "After Effects запущен")
    try:
        import ae_render_worker
        current = ae_render_worker.active_project_path(config)
    except Exception as exc:  # noqa: BLE001 - diagnostics must not crash
        report(WARN, "Не удалось спросить у AE открытый проект", repr(exc),
               "Проверьте разрешение на управление AE в «Системные настройки → Конфиденциальность → Автоматизация».")
        return
    expected = str(config.get("project_path") or "")
    if not current:
        report(FAIL, "В After Effects не открыт проект",
               "Ожидается: {}".format(expected), "Откройте этот проект в AE.")
    elif os.path.normpath(current) == os.path.normpath(os.path.expanduser(expected)):
        report(OK, "Открыт нужный проект", current)
    else:
        report(FAIL, "В After Effects открыт другой проект",
               "Открыт:    {}\nОжидается: {}".format(current, expected),
               "Откройте ожидаемый проект — иначе все задания будут падать.")


def check_services():
    for label, human in (
        ("com.tg-pushes-ts26.ae-render-worker", "AE render worker"),
        ("com.tg-pushes-ts26.sheet-monitor", "Telegram-бот"),
    ):
        loaded = launchagent_loaded(label)
        if loaded is None:
            report(WARN, "{}: не удалось проверить launchctl".format(human))
        elif loaded:
            report(OK, "{} загружен в launchd".format(human))
        else:
            report(FAIL, "{} не запущен".format(human),
                   fix="Запустите соответствующий install_*.command.")


def check_queue(config):
    queue_path = Path(config.get("queue_path") or ae_render_queue.default_queue_path()).expanduser()
    if not queue_path.is_absolute():
        queue_path = SCRIPT_DIR / queue_path
    try:
        counts, failures = ae_render_queue.queue_counts(queue_path)
    except Exception as exc:  # noqa: BLE001
        report(FAIL, "Очередь не читается", repr(exc), "Проверьте права на файл очереди.")
        return
    summary = " · ".join("{}: {}".format(key, value) for key, value in sorted(counts.items())) or "пусто"
    report(OK, "Очередь прочитана", "{}\n{}".format(queue_path, summary))
    if counts.get("error"):
        newest = failures[0]
        report(WARN, "Есть неудачные задания: {}".format(counts["error"]),
               "Последнее: {} — {}".format((newest.get("payload") or {}).get("name", ""), str(newest.get("error", ""))[:200]),
               "После устранения причины отправьте боту /render_retry.")


def check_bot_wiring():
    ae_render_notify.load_env_file(SCRIPT_DIR / ".env")
    if str(os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip():
        report(OK, "TELEGRAM_BOT_TOKEN задан")
    else:
        report(FAIL, "TELEGRAM_BOT_TOKEN не задан", fix="Заполните .env — иначе бот и уведомления не работают.")

    if ae_render_notify.admin_chat_ids():
        report(OK, "Получатели уведомлений", ", ".join(ae_render_notify.admin_chat_ids()))
    else:
        report(FAIL, "Не задан ни один chat_id", fix="Заполните TELEGRAM_CHAT_ID в .env.")

    trigger_url = str(os.environ.get("AE_RENDER_TRIGGER_URL", "")).strip()
    if trigger_url:
        report(OK, "Бот использует HTTP-триггер", trigger_url)
        if not str(os.environ.get("AE_RENDER_TRIGGER_TOKEN", "")).strip():
            report(WARN, "AE_RENDER_TRIGGER_TOKEN пуст",
                   fix="Задайте токен из ~/Documents/tg_sheet_monitor/ae_render_trigger.token.")
    else:
        report(WARN, "AE_RENDER_TRIGGER_URL не задан",
               "Бот пишет задание прямо в файл очереди. Это работает, только если бот и\n"
               "After Effects на одной машине, и бот не может сразу сказать, что в AE\n"
               "открыт не тот проект.",
               "Рекомендуется: AE_RENDER_TRIGGER_URL=http://127.0.0.1:8765/render")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Диагностика конвейера рендера плашек.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    args = parser.parse_args(argv)

    print("=" * 68)
    print("Диагностика конвейера рендера TS26")
    print("=" * 68)
    print()

    config = check_config(Path(args.config).expanduser())
    if config is None:
        return 2

    check_bot_wiring()
    check_templates()
    check_paths(config)
    check_services()
    check_queue(config)
    if sys.platform == "darwin":
        check_after_effects(config)
    else:
        report(WARN, "Проверка After Effects пропущена", "Не macOS.")

    failures = _results.count(FAIL)
    warnings = _results.count(WARN)
    print("=" * 68)
    print("Итог: OK {} · ВНИМАНИЕ {} · ОШИБКА {}".format(_results.count(OK), warnings, failures))
    if failures:
        print("Рендер не заработает, пока не устранены пункты [ОШИБКА].")
    print("=" * 68)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
