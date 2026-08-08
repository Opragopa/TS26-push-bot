# TS26 Push Bot

Telegram-бот для мониторинга Google Sheets, подготовки AE-ready таблиц, добавления плашек через Telegram и запуска локального рендера After Effects через защищенный HTTP-trigger.

Система рассчитана на раздельную инфраструктуру:

- бот работает на внешнем хостинге;
- After Effects работает локально на Mac;
- хостинг не пишет напрямую в локальные файлы Mac;
- публичный tunnel пробрасывает запросы к локальному trigger-серверу;
- локальный worker готовит и рендерит задания в уже открытом проекте After Effects.

В репозиторий нельзя коммитить реальные токены, Google Sheet ID, локальные пути, домены и OAuth-файлы. Для этого есть локальные `.env`, `sheets.json`, `ae_render_config.json`; в Git лежат только безопасные `*.example` шаблоны.

## Возможности

- Мониторинг одной или нескольких Google Sheets.
- Мгновенные Telegram-уведомления по оперативным таблицам, например `План записи`.
- Почасовые AI-сводки для `Контент-плана` с сохранением полного diff.
- Форма добавления плашек прямо в Telegram.
- Пакетное добавление плашек несколькими строками.
- Запись плашек в настроенную Google Sheet.
- Автоматическая постановка плашек в очередь рендера.
- Понятный статус рендера пользователю: стартует сейчас, ждет очередь, After Effects занят, открыт не тот проект или рендер отключен.
- Telegram-уведомления от локального worker после успешного или неудачного рендера.
- AE-ready таблица с нормализованными данными для тем сессий, плашек, визиток, предупреждений и исходных ячеек.
- Figma-плагин для создания и обновления визиток из листа `content_plan_cards`.
- Архивация отрендеренных плашек, которые исчезли из активных строк таблицы.
- Восстановление зависших render jobs после перезапуска worker.

## Схема

```text
Пользователь Telegram
    |
    v
Хостинг: main.py
    |
    |  POST /render
    |  Authorization: Bearer AE_RENDER_TRIGGER_TOKEN
    v
Tunnel: ngrok / Cloudflare Tunnel / custom domain
    |
    v
Mac: ae_render_trigger_server.py
    |
    v
Очередь: data/ae_render_queue.json
    |
    v
Mac: ae_render_worker.py
    |
    v
Открытый проект After Effects
    |
    v
Настроенные output-папки
```

Worker ожидает, что правильный `.aep` уже открыт в After Effects. Он не создает временные проекты, не переключает проект сам и не прерывает активный Render Queue. Если After Effects занят, задание возвращается в `queued` и будет обработано позже.

## Файлы

Публичные файлы в Git:

- `README.md` - эта инструкция.
- `.env.example` - безопасный шаблон переменных окружения.
- `sheets.example.json` - безопасный шаблон списка Google Sheets.
- `ae_render_config.example.json` - безопасный шаблон локального AE-конфига.
- `tg_sheet_monitor.py` - основной монитор и Telegram-бот.
- `ae_render_trigger_server.py` - локальный HTTP-trigger для рендера.
- `ae_render_worker.py` - локальный обработчик очереди After Effects.
- `ae_render_queue.py` - работа с JSON-очередью.
- `ae_sheet_source.py` - опциональное локальное чтение Google Sheets для AE jobs.
- `ae_render_notify.py` - уведомления о результате рендера из локального worker.
- `ae_render_doctor.py` - диагностика локального render pipeline.
- `figma_plugin/` - ручной Figma-плагин для визиток.
- `tests/` - unit-тесты.

Локальные приватные файлы, которые игнорируются Git:

```text
.env
sheets.json
ae_render_config.json
google_oauth_user.json
client_secret*.json
data/
state/
```

## Быстрый старт

1. Установить зависимости:

```bash
python3 -m pip install -r requirements.txt
```

2. Создать локальные конфиги:

```bash
cp .env.example .env
cp sheets.example.json sheets.json
cp ae_render_config.example.json ae_render_config.json
```

3. Заполнить `.env`:

```text
TELEGRAM_BOT_TOKEN=<BOT_TOKEN>
TELEGRAM_CHAT_ID=<DEFAULT_CHAT_ID>
```

4. Заполнить реальные Google Sheets URL в `sheets.json`.

5. Если хостинг деплоит только Git-репозиторий и локальный `sheets.json` туда не попадает, задайте тот же список таблиц через `SHEETS_JSON`:

```json
[{"label":"Контент-план","url":"https://docs.google.com/spreadsheets/d/<CONTENT_PLAN_SPREADSHEET_ID>/edit?gid=<WORKSHEET_GID>","extra_chat_ids":[]},{"label":"План записи","url":"https://docs.google.com/spreadsheets/d/<RECORDING_PLAN_SPREADSHEET_ID>/edit?gid=<WORKSHEET_GID>","range":"U:AM"}]
```

6. Проверить один локальный запуск:

```bash
python3 tg_sheet_monitor.py --notify-initial
```

7. На хостинге запускать:

```bash
python main.py
```

## Telegram

Создайте бота через `@BotFather`, затем получите chat IDs:

```bash
python3 tg_sheet_monitor.py --print-chat-ids
```

Основные переменные:

```text
TELEGRAM_CHAT_ID=<PRIMARY_CHAT_ID>
TELEGRAM_CHAT_IDS=<CHAT_ID_1>,<CHAT_ID_2>
TELEGRAM_ADMIN_CHAT_IDS=<ADMIN_CHAT_ID_1>,<ADMIN_CHAT_ID_2>
```

`TELEGRAM_CHAT_ID` и `TELEGRAM_CHAT_IDS` получают обычные уведомления по таблицам. `TELEGRAM_ADMIN_CHAT_IDS` получают админские команды, отчеты по очереди рендера и служебные сообщения.

## Google Sheets

Для записи плашек и создания AE-ready таблицы нужен доступ на редактирование.

Поддерживаются варианты:

- `GOOGLE_SERVICE_ACCOUNT_JSON` или `GOOGLE_SERVICE_ACCOUNT_FILE`;
- `GOOGLE_OAUTH_USER_JSON` или `GOOGLE_OAUTH_USER_FILE`.

OAuth helper:

```bash
python3 make_google_oauth_token.py
```

Если Google возвращает `invalid_grant`, нужно заново получить OAuth user JSON и обновить переменную на хостинге или локально. Обычно это значит, что refresh token истек или был отозван.

## Мониторинг Таблиц

Таблицы задаются в `sheets.json` или через `SHEETS_JSON`.

Пример:

```json
[
  {
    "label": "Контент-план",
    "url": "https://docs.google.com/spreadsheets/d/<CONTENT_PLAN_SPREADSHEET_ID>/edit?gid=<WORKSHEET_GID>",
    "extra_chat_ids": []
  },
  {
    "label": "План записи",
    "url": "https://docs.google.com/spreadsheets/d/<RECORDING_PLAN_SPREADSHEET_ID>/edit?gid=<WORKSHEET_GID>",
    "range": "U:AM"
  }
]
```

Полезные переменные:

```text
SHEET_MONITOR_INTERVAL=120
SHEET_MONITOR_DURATION_SECONDS=0
SHEET_MONITOR_NOTIFY_INITIAL=false
SHEET_MONITOR_STARTUP_MESSAGE=false
TELEGRAM_DISABLE_WEB_PAGE_PREVIEW=true
```

`range` ограничивает мониторинг конкретной областью листа, например `U:AM`.

## Почасовые AI-Сводки

Изменения `Контент-плана` складываются в очередь и отправляются почасовым пакетом: сначала AI-сводка, затем полный diff. Если AI недоступен, полный diff все равно отправляется.

```text
AI_SUMMARY_PROVIDER=groq
GROQ_API_KEY=<GROQ_API_KEY>
GROQ_SUMMARY_MODEL=llama-3.3-70b-versatile
OPENAI_API_KEY=<OPENAI_API_KEY>
OPENAI_SUMMARY_MODEL=gpt-5-mini
OPENAI_SUMMARY_MAX_INPUT_CHARS=60000
CONTENT_PLAN_TIME_ZONE=Europe/Amsterdam
CONTENT_PLAN_DELIVERY_RETRY_SECONDS=300
```

Если доставка пакета временно сломалась, бот не долбит Telegram/API на каждом тике. Пакет остается в очереди, а `/status` показывает последнюю попытку и последнюю ошибку.

## AE-Ready

AE-ready sync создает отдельную Google Sheet для моушена и не меняет исходный `Контент-план`.

Листы, которые создает sync:

```text
content_plan_sessions
content_plan_plates
content_plan_cards
content_plan_all_people
content_plan_topics_model
content_plan_sessions_model
content_plan_session_people
import_report
warnings
source_cells
```

Переменные:

```text
AE_READY_SYNC_ENABLED=true
AE_READY_SOURCE_URL=https://docs.google.com/spreadsheets/d/<SOURCE_SPREADSHEET_ID>/edit
AE_POSITION_REFERENCE_URL=https://docs.google.com/spreadsheets/d/<REFERENCE_SPREADSHEET_ID>/edit
AE_READY_SPREADSHEET_ID=<OPTIONAL_EXISTING_SPREADSHEET_ID>
AE_READY_SPREADSHEET_TITLE=TS26 AE-ready Content Plan
AE_READY_SHARE_EMAILS=<EMAIL_1>,<EMAIL_2>
```

AI-коррекция:

```text
AI_CORRECTION_PROVIDER=deepseek
AI_CORRECTION_FALLBACK_PROVIDER=groq
AI_CORRECTION_ENABLED=true
AI_CORRECTION_MAX_CALLS_PER_SYNC=16
AI_CORRECTION_CONFIDENCE_THRESHOLD=0.82
AI_CORRECTION_MAX_OUTPUT_TOKENS=800
DEEPSEEK_API_KEY=<DEEPSEEK_API_KEY>
DEEPSEEK_MODEL=deepseek-v4-pro
GROQ_CORRECTION_MODEL=llama-3.3-70b-versatile
```

Перенос плашек из AE-ready в лист плашек:

```text
AE_READY_PLAQUE_SYNC_ENABLED=true
AE_READY_PLAQUE_CONFIDENCE_THRESHOLD=0.9
AE_READY_PLAQUE_NOTE_TEXT=<-- added from AE-ready
PLAQUE_SPREADSHEET_ID=<PLAQUE_SPREADSHEET_ID>
PLAQUE_WORKSHEET_GID=<WORKSHEET_GID>
```

## Плашки Через Telegram

Пользователи с доступом могут отправлять плашки прямо в Telegram.

Поддерживается:

- одна плашка: сначала ФИО, потом должность;
- пакет плашек: несколько строк одним сообщением;
- существующие ФИО обновляются на месте;
- новые ФИО пишутся в первую свободную строку начиная с `PLAQUE_START_ROW`.

Переменные:

```text
PLAQUE_FORM_ENABLED=true
PLAQUE_SPREADSHEET_ID=<PLAQUE_SPREADSHEET_ID>
PLAQUE_WORKSHEET_GID=<WORKSHEET_GID>
PLAQUE_START_ROW=280
PLAQUE_NAME_COL=1
PLAQUE_POSITION_COL=2
PLAQUE_NOTE_COL=5
PLAQUE_NOTE_TEXT=<-- added via Telegram bot
```

После подтверждения бот записывает плашку в Google Sheets и ставит render job, если `AE_RENDER_ENABLED=true`.

## AE Render Pipeline

Поток рендера:

```text
Подтверждение плашки в Telegram
    -> запись строки в Google Sheet
    -> постановка render job через trigger URL или локальную очередь
    -> worker готовит композицию в After Effects
    -> worker запускает текущий Render Queue
    -> worker копирует staged MOV в настроенную output-папку
    -> worker отправляет Telegram-уведомление
```

Переменные для хостинга:

```text
AE_RENDER_ENABLED=true
AE_RENDER_TRIGGER_URL=https://<PUBLIC_TUNNEL_OR_DOMAIN>/render
AE_RENDER_TRIGGER_TOKEN=<SHARED_SECRET>
AE_RENDER_NOTIFY_TELEGRAM=true
```

Если `AE_RENDER_TRIGGER_URL` пустой, бот пишет напрямую в `data/ae_render_queue.json`. Этот режим подходит только когда бот и worker работают на одной машине.

## Настройка Mac Для Рендера

1. Заполните локальный `ae_render_config.json`.

Ключевые поля:

```text
project_path
afterfx_bin
aerender_bin
person_plates_script_path
session_topics_script_path
routes.plaque_output_dir
routes.session_topics_root
routes.fire_of_meanings_output_dir
output_module_templates
```

2. Откройте настроенный `.aep` проект в After Effects.

3. Установите trigger-сервер:

```bash
AE_RENDER_TRIGGER_TOKEN="<SHARED_SECRET>" ./install_ae_render_trigger_server_macos.command
```

Если токен не передать, установщик создаст его:

```text
~/Documents/tg_sheet_monitor/ae_render_trigger.token
```

4. Пробросьте локальный порт `8765` через tunnel:

```bash
ngrok http 8765
```

5. В переменные хостинга добавьте публичный URL с `/render` и тот же токен.

6. Установите worker:

```bash
./install_ae_render_worker_macos.command
```

Логи:

```text
~/Documents/tg_sheet_monitor/ae_render_trigger.log
~/Documents/tg_sheet_monitor/ae_render_trigger.err.log
~/Documents/tg_sheet_monitor/ae_render_worker.log
~/Documents/tg_sheet_monitor/ae_render_worker.err.log
```

Остановить локальные сервисы:

```bash
./stop_ae_render_trigger_server_macos.command
./stop_ae_render_worker_macos.command
```

## Типы Рендера

Плашки:

```text
Template comp: MASTER-COMP
Name layer: templates.plaque.name_layer
Position layer: templates.plaque.position_layer
Output module: High Quality with Alpha
Output path: routes.plaque_output_dir
File name: generated comp name + .mov
```

Темы сессий:

```text
Template pattern: templates.session_topic.comp_pattern
Topic layer: templates.session_topic.topic_layer
Description layer: templates.session_topic.description_layer
Output module: DVX 3 no audio
Output path: routes.session_topics_root/<SHIFT>/Day <N>
```

Огонь смыслов:

```text
Output path: routes.fire_of_meanings_output_dir
Status: route reserved; render template must be configured before use
```

## Режимы Очереди

Рекомендуемый режим для хостинга:

```text
Hosted bot -> HTTP trigger -> local queue -> worker
```

Опциональный локальный polling Google Sheets:

```bash
python3 ae_render_worker.py --poll-sheets
```

Ручные команды worker:

```bash
python3 ae_render_worker.py --once
python3 ae_render_worker.py --once --poll-sheets
python3 ae_render_worker.py --poll-sheets --poll-only
python3 ae_render_worker.py --poll-sheets --poll-only --sync-dry-run
```

`--poll-sheets` требует рабочий Google OAuth или service account. Если credentials истекли, trigger-рендер может продолжать работать, но polling будет писать Google auth ошибки.

## Telegram-Команды

Пользовательские:

```text
/start
/add
/plaque
/cancel
```

Админские:

```text
/status
/recipients
/content_users
/add_content_user <chat_id>
/remove_content_user <chat_id>
/plaque_users
/add_plaque_user <chat_id>
/remove_plaque_user <chat_id>
/ae_status
/ae_sync
/ae_rebuild
/ae_link
/ae_warnings
/ae_source <url>
/figma
/render_status
/render_retry
/google_access
/test_content
/test_recording
/preview_user
/user_mode
```

Особенно полезно для рендера:

- `/render_status` показывает очередь и последние ошибки.
- `/render_retry` возвращает failed jobs в `queued`.
- `/ae_status` показывает состояние AE-ready sync.
- `/google_access` проверяет Google credentials.

## Диагностика

Полная локальная диагностика render pipeline:

```bash
python3 ae_render_doctor.py
```

Проверить trigger:

```bash
curl http://127.0.0.1:8765/health
```

Отправить тестовый render request:

```bash
curl -X POST http://127.0.0.1:8765/render \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <SHARED_SECRET>" \
  -d '{"kind":"plaque","name":"Test Person","position":"Test Position","source_key":"manual-test"}'
```

Смотреть логи:

```bash
tail -f ~/Documents/tg_sheet_monitor/ae_render_trigger.log
tail -f ~/Documents/tg_sheet_monitor/ae_render_worker.log
tail -f ~/Documents/tg_sheet_monitor/ae_render_worker.err.log
```

Коды trigger:

```text
200 queued      запрос принят, создан новый job
200 existing    похожий job уже есть
400             плохой JSON или нет обязательных полей
401             нет токена или токен не совпал
409             в After Effects открыт не тот проект
500             внутренняя ошибка trigger-сервера
```

Частые проблемы:

- `wrong project`: откройте нужный `.aep` в After Effects и повторите.
- `After Effects busy`: дождитесь текущего рендера, job останется в очереди.
- `invalid_grant`: обновите Google OAuth credentials.
- `Output Module missing`: создайте или исправьте Output Module preset в After Effects.
- `Directory does not exist`: проверьте `routes` в локальном `ae_render_config.json`.

## Figma-Визитки

Плагин в `figma_plugin/` читает `content_plan_cards` из AE-ready таблицы и создает или обновляет визитки в открытом Figma-файле.

Ожидаемый шаблон:

```text
Frame или component: TS26/VIZITKA_TEMPLATE
Layers: FIO, POSITION, PHOTO
```

Короткая инструкция для оператора доступна командой `/figma`.

## Безопасность

- Не коммитьте `.env`, `sheets.json`, `ae_render_config.json`, OAuth JSON и client secrets.
- Не публикуйте trigger server без `AE_RENDER_TRIGGER_TOKEN`.
- Локально держите trigger на `127.0.0.1:8765`, наружу отдавайте только через контролируемый tunnel.
- Перевыпускайте токены, если они попали в чат, скриншоты, логи или публичные панели.
- В README и example-файлах используйте только placeholders.

## Проверки

Syntax-check без записи `__pycache__`:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 - <<'PY'
from pathlib import Path
for name in [
    "tg_sheet_monitor.py",
    "ae_render_worker.py",
    "ae_render_trigger_server.py",
    "ae_render_queue.py",
    "ae_sheet_source.py",
    "ae_render_notify.py",
    "ae_render_doctor.py",
]:
    compile(Path(name).read_text(encoding="utf-8"), name, "exec")
    print("ok", name)
PY
```

Unit-тесты:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```

Проверка tracked-файлов на случайные приватные значения:

```bash
git grep -n -E '/Users/<name>|<real-domain>|<real-spreadsheet-id>' HEAD -- .
```

Unit-тесты и syntax-check не доказывают, что After Effects реально отрендерит проект. Для runtime-проверки нужно открыть настроенный AE-проект и отправить настоящий тест через `/render`.
