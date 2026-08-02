# TS26 Telegram bot и AE render pipeline

Бот следит за Google Sheets, присылает изменения в Telegram, собирает AE-ready таблицу для моушена и умеет запускать локальный рендер After Effects на Mac через защищенный HTTP-trigger.

Главная идея рендера: бот может жить на стороннем хостинге, а After Effects остается на рабочем Mac. Хостинг не пишет в локальные файлы Mac напрямую. Вместо этого бот отправляет HTTPS-запрос на локальный trigger-сервер, опубликованный через tunnel. Trigger кладет задание в локальную очередь, worker готовит композицию в уже открытом проекте After Effects и рендерит файл в настроенную output-папку.

## Что умеет система

- Уведомляет Telegram о сменах в Google Sheets.
- Для `Контент-план` копит изменения и отправляет почасовую AI-сводку плюс полный diff.
- Для `План записи` отправляет изменения сразу.
- Дает пользователям форму добавления плашек прямо в Telegram.
- Записывает плашки в Google Sheet `МОУШЕН`.
- Автоматически ставит плашку в очередь рендера.
- Показывает пользователю статус рендера: запускается сейчас, ждет очередь, After Effects занят, проект не открыт или рендер отключен.
- Отправляет Telegram-уведомление, когда локальный worker завершил рендер или поймал ошибку.
- Создает AE-ready Google Sheet с листами для плашек, тем сессий, визиток и предупреждений.
- Поддерживает Figma-плагин для визиток из AE-ready таблицы.
- Архивирует уже отрендеренные плашки, если они исчезли из таблицы.

## Архитектура

```text
Пользователь Telegram
        |
        v
Hosted bot
        |
        |  HTTPS POST /render
        |  Authorization: Bearer AE_RENDER_TRIGGER_TOKEN
        v
ngrok / Cloudflare Tunnel / custom tunnel
        |
        v
Mac: ae_render_trigger_server.py
        |
        v
data/ae_render_queue.json
        |
        v
Mac: ae_render_worker.py
        |
        v
Открытый проект After Effects
        |
        v
Output folders
```

Важно: локальный After Effects проект должен быть открыт заранее. Worker не создает временные `.aep`, не открывает проект сам и не прерывает чужой рендер. Если After Effects уже рендерит, задание возвращается в очередь и будет обработано позже.

## Быстрый старт бота

1. Создайте Telegram-бота через `@BotFather`.
2. Получите `chat_id`: напишите боту любое сообщение и выполните:

```bash
python3 tg_sheet_monitor.py --print-chat-ids
```

3. Скопируйте настройки:

```bash
cp .env.example .env
cp sheets.example.json sheets.json
```

4. Заполните минимум:

```text
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789
```

Для нескольких получателей:

```text
TELEGRAM_CHAT_IDS=123456789,987654321
```

5. Заполните реальные Google Sheets URL в локальном `sheets.json`. Этот файл добавлен в `.gitignore` и не должен уходить в публичный Git.
6. Локально запустите один цикл:

```bash
python3 tg_sheet_monitor.py --notify-initial
```

На хостинге бот должен запускаться через `python main.py`. В репозитории есть `Dockerfile`; AE JSX-файлы в корне оставлены как совместимые лаунчеры, чтобы старые настройки хостинга не запускали JSX вместо Python.

## Переменные окружения бота

Минимум для Telegram:

```text
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_CHAT_IDS=
TELEGRAM_ADMIN_CHAT_IDS=
```

Для render-trigger с хостинга:

```text
AE_RENDER_ENABLED=true
AE_RENDER_TRIGGER_URL=https://your-ngrok-or-domain/render
AE_RENDER_TRIGGER_TOKEN=тот_же_токен_что_на_Mac
```

Если `AE_RENDER_TRIGGER_URL` не задан, бот попытается писать в локальный `data/ae_render_queue.json`. Это подходит только когда бот и After Effects работают на одной машине. Для внешнего хостинга почти всегда нужен trigger URL.

Для уведомлений о результате рендера от локального worker:

```text
AE_RENDER_NOTIFY_TELEGRAM=true
```

Для ссылок на готовые плашки в Яндекс.Диске:

```text
YANDEX_DISK_PLATES_ENABLED=true
YANDEX_DISK_TOKEN=ваш_oauth_токен_яндекс_диска
YANDEX_DISK_OUTPUT_ROOT=disk:/path/to/rendered/plates
```

Токены не коммитятся в Git. Если токен попал на скриншот или в чат, его лучше перевыпустить.

## AE-ready Контент-план

AE-ready таблица создается отдельно и не меняет оригинальный `Контент-план`. Бот обновляет ее раз в час при изменении источника или вручную командой `/ae_sync`.

Основные листы:

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
AE_READY_SOURCE_URL=https://docs.google.com/spreadsheets/d/.../edit
AE_POSITION_REFERENCE_URL=https://docs.google.com/spreadsheets/d/.../edit
AE_READY_SPREADSHEET_ID=
AE_READY_SPREADSHEET_TITLE=TS26 AE-ready Content Plan
AE_READY_SHARE_EMAILS=email@example.com
AE_READY_PLAQUE_SYNC_ENABLED=true
AE_READY_PLAQUE_CONFIDENCE_THRESHOLD=0.9
AE_READY_PLAQUE_NOTE_TEXT=<-- добавлено из AE-ready
```

`AE_READY_SPREADSHEET_ID` можно не задавать: при первом `/ae_sync` бот создаст таблицу и сохранит ID в `sheet_state.json`. Ссылку можно получить командой `/ae_link`.

AI-коррекция ФИО и должностей:

```text
AI_CORRECTION_PROVIDER=deepseek
AI_CORRECTION_FALLBACK_PROVIDER=groq
AI_CORRECTION_ENABLED=true
AI_CORRECTION_MAX_CALLS_PER_SYNC=16
AI_CORRECTION_CONFIDENCE_THRESHOLD=0.82
AI_CORRECTION_MAX_OUTPUT_TOKENS=800
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-v4-pro
GROQ_API_KEY=
GROQ_CORRECTION_MODEL=llama-3.3-70b-versatile
```

## Почасовые AI-сводки

`Контент-план` отправляется пакетами в начале часа. Если изменений не было, сообщение не отправляется. Если AI недоступен, бот все равно отправит полный diff.

```text
AI_SUMMARY_PROVIDER=groq
GROQ_API_KEY=
GROQ_SUMMARY_MODEL=llama-3.3-70b-versatile
OPENAI_API_KEY=
OPENAI_SUMMARY_MODEL=gpt-5-mini
OPENAI_SUMMARY_MAX_INPUT_CHARS=60000
CONTENT_PLAN_TIME_ZONE=Europe/Amsterdam
CONTENT_PLAN_DELIVERY_RETRY_SECONDS=300
```

## Рендер плашек

Плашки создаются из AE-шаблона:

```text
Композиция: MASTER-COMP
Слой имени: ФИО спикера
Слой должности: Должность
Output Module: High Quality with Alpha
```

Worker запускает `person_plates_from_sheet.jsx` в ручном режиме. Скрипт дублирует `MASTER-COMP`, заполняет текст, называет новую композицию в формате `Фамилия Имя` без префиксов и добавляет созданную композицию в Render Queue. После этого worker рендерит именно созданную композицию, а не шаблон.

Вывод:

```text
<LOCAL_SYNC_ROOT>/plates
```

Файл получается как:

```text
Фамилия Имя.mov
```

## Рендер тем сессий

Темы сессий читаются из AE-ready листа `content_plan_sessions`. Автоматический рендер тем по умолчанию выключен, чтобы сначала проверить данные вручную.

Шаблон композиции:

```text
{shift}_.*Заставка с темами_.*альт
```

Примеры shift: `ПРАВДА`, `РОДИНА`.

Слои:

```text
ТЕМА
ОПИСАНИЕ
```

Output Module:

```text
DVX 3 no audio
```

Вывод:

```text
<LOCAL_SYNC_ROOT>/session_topics/<SHIFT>/Day <N>
```

Включение:

```text
AE_RENDER_SESSION_TOPICS_ENABLED=true
AE_ACTIVE_SHIFT=ПРАВДА
```

Также в `ae_render_config.json` должен быть указан `ae_ready_spreadsheet_id` или переменная `AE_READY_SPREADSHEET_ID`.

## Огонь смыслов

Маршрут уже зарезервирован:

```text
<LOCAL_SYNC_ROOT>/fire_of_meanings
```

В `ae_render_config.json` тип `fire_of_meanings` пока не имеет заполненного Output Module и AE-шаблона. Worker остановит такое задание с понятной ошибкой, пока шаблон не будет подключен.

## Настройка Mac для рендера

1. Откройте проект в After Effects:

```text
<ABSOLUTE_PATH_TO_AFTER_EFFECTS_PROJECT>.aep
```

2. Проверьте `ae_render_config.json`:

```bash
cp ae_render_config.example.json ae_render_config.json
```

В локальном `ae_render_config.json` заполните:

```text
project_path
afterfx_bin
aerender_bin
person_plates_script_path
session_topics_script_path
routes
output_module_templates
```

`ae_render_config.json` содержит локальные пути, ID таблиц и output-директории, поэтому он добавлен в `.gitignore`. В Git хранится только `ae_render_config.example.json`.

3. Установите trigger-сервер:

```bash
AE_RENDER_TRIGGER_TOKEN="ваш_секретный_токен" ./install_ae_render_trigger_server_macos.command
```

Если токен не передать, установщик сгенерирует его и сохранит здесь:

```text
~/Documents/tg_sheet_monitor/ae_render_trigger.token
```

4. Запустите tunnel. Для ngrok пример:

```bash
ngrok http 8765
```

В переменных окружения хостинга нужно прописать URL из строки `Forwarding`, например:

```text
AE_RENDER_TRIGGER_URL=https://example.ngrok-free.dev/render
AE_RENDER_TRIGGER_TOKEN=тот_же_токен_из_ae_render_trigger.token
```

5. Установите worker:

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

Остановить:

```bash
./stop_ae_render_trigger_server_macos.command
./stop_ae_render_worker_macos.command
```

## Два режима обработки очереди

Рекомендуемый режим для хостинга: бот вызывает trigger, trigger кладет задание в очередь, worker забирает очередь на Mac.

Локальный worker, установленный через `install_ae_render_worker_macos.command`, запускается с `--poll-sheets`. Это значит, что он дополнительно читает Google Sheets и может сам добавлять задания из таблицы. Если OAuth Google истек, в `ae_render_worker.err.log` появится `invalid_grant`; trigger-рендер при этом может продолжать работать, но логи будут шумными.

Ручная обработка одного задания без постоянного цикла:

```bash
python3 ae_render_worker.py --once
```

Ручное чтение Google Sheets и обработка одного задания:

```bash
python3 ae_render_worker.py --once --poll-sheets
```

Только проверить Google Sheets без запуска After Effects:

```bash
python3 ae_render_worker.py --poll-sheets --poll-only
```

Проверить, что trigger жив:

```bash
curl http://127.0.0.1:8765/health
```

Тестовая постановка в очередь:

```bash
curl -X POST http://127.0.0.1:8765/render   -H 'Content-Type: application/json'   -H 'Authorization: Bearer TOKEN'   -d '{"kind":"plaque","name":"Тестовый Иван","position":"ТЕСТ","source_key":"manual-test"}'
```

## Команды Telegram

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
/ae_status
/ae_sync
/ae_rebuild
/ae_link
/ae_warnings
/ae_source <url>
/plaque_users
/add_plaque_user <chat_id>
/remove_plaque_user <chat_id>
/render_retry
/figma
```

`/render_retry` возвращает ошибочные render jobs в очередь. Используйте его после того, как открыли правильный проект After Effects или исправили Output Module/папку вывода.

## Диагностика рендера

Главная команда диагностики на Mac:

```bash
python3 ae_render_doctor.py
```

Что смотреть при проблемах:

```bash
tail -f ~/Documents/tg_sheet_monitor/ae_render_trigger.log
tail -f ~/Documents/tg_sheet_monitor/ae_render_worker.log
tail -f ~/Documents/tg_sheet_monitor/ae_render_worker.err.log
```

Частые статусы trigger:

```text
200 queued      бот дошел до Mac, задание добавлено
200 existing    такое задание уже есть в очереди или выполнено
400             не хватает name/position или некорректный JSON
401             не передан или не совпал токен
409             открыт не тот проект After Effects
500             внутренняя ошибка trigger-сервера
```

Если пользователь видит “Рендер запускается сейчас”, файл обычно появится быстро. Если видит “перед ним N заданий” или “After Effects занят”, плашка не потерялась: она ждет очередь.

## Очередь и файлы состояния

```text
data/ae_render_queue.json       очередь заданий
data/ae_render_registry.json    индекс отрендеренных плашек для архивации
data/sheet_state.json           состояние мониторинга и AE-ready sync
```

Локальные конфиги, которые не коммитятся:

```text
.env
sheets.json
ae_render_config.json
google_oauth_user.json
client_secret*.json
```

Статусы jobs:

```text
queued
preparing
rendering
done
error
cancelled
```

Worker восстанавливает зависшие jobs после перезапуска, если lease истек.

## Архивация удаленных плашек

Если плашка исчезла из активных строк Google Sheets, worker не удаляет файл безвозвратно. При настройке:

```json
"delete_missing_plaques": "archive"
```

файл переносится в архивную папку, например:

```text
_Удаленные AE
```

Проверка без переноса файлов:

```bash
python3 ae_render_worker.py --poll-sheets --poll-only --sync-dry-run
```

## Временные файлы

Worker использует `/private/tmp/ts26-ae-render` только для per-job JSX, params и staged `.mov`. После задания папка job очищается автоматически. Старые временные проекты от прошлой версии можно убрать командой:

```bash
./cleanup_ae_temp_projects_macos.command
```

Команда переносит `/private/tmp/ts26-ae-render` в Корзину, если внутри найдены `.aep`.

## Google OAuth

Для локального polling Google Sheets нужен OAuth user JSON:

```text
<PROJECT_DIR>/google_oauth_user.json
```

Если в логах worker появляется:

```text
invalid_grant: Token has been expired or revoked
```

нужно заново пройти OAuth и заменить user JSON. Это влияет на режим `--poll-sheets`, но не мешает trigger принимать задания от хостингового бота.

## Figma-визитки

Плагин лежит в `figma_plugin/`. Он читает `content_plan_cards` из AE-ready таблицы и создает или обновляет визитки в Figma.

Шаблон в Figma:

```text
TS26/VIZITKA_TEMPLATE
```

Слои внутри шаблона:

```text
FIO
POSITION
PHOTO
```

Команда `/figma` показывает краткую инструкцию для оператора.

## Проверки перед пушем

Быстрая проверка Python:

```bash
python3 -m py_compile tg_sheet_monitor.py ae_render_worker.py ae_render_trigger_server.py ae_render_queue.py ae_sheet_source.py ae_render_notify.py ae_render_doctor.py
```

Юнит-тесты:

```bash
python3 -m unittest discover -s tests
```

Важно: успешные unit tests не доказывают, что After Effects реально отрендерит проект. Для runtime smoke test нужен открытый проект AE и тестовая плашка через `/render` или `curl`.
