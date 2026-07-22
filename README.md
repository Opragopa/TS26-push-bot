# Telegram-уведомления об изменении Google Sheets

Небольшой бот-поллер: периодически скачивает выбранные листы Google Sheets как TSV, считает SHA-256 и отправляет уведомление в Telegram, если содержимое изменилось.

## Быстрый старт

1. Создайте бота через `@BotFather` и получите токен.
2. Узнайте `chat_id`: напишите боту любое сообщение, затем откройте:

   ```bash
   curl "https://api.telegram.org/bot<ТОКЕН>/getUpdates"
   ```

3. Скопируйте настройки:

   ```bash
   cp .env.example .env
   ```

4. Заполните `.env`:

   ```bash
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=123456789
   ```

5. Отредактируйте `sheets.json`, если нужны другие таблицы.
6. Запустите:

   ```bash
   python3 tg_sheet_monitor.py --notify-initial
   ```

Первый запуск по умолчанию сохраняет базовый снимок без тревоги. `--notify-initial` отправит сообщение и при первом снимке.

## Проверить один раз

```bash
python3 tg_sheet_monitor.py --once --notify-initial
```

## Параметры

```bash
python3 tg_sheet_monitor.py --help
```

Основные флаги:

- `--interval 120` - интервал проверки в секундах.
- `--sheet "Название=https://docs.google.com/..."` - задать таблицы через CLI вместо `sheets.json`.
- `--state state/sheet_state.json` - путь к файлу состояния.
- `--quiet` - не печатать проверки без изменений.
- `--no-telegram` - только логировать, не отправлять сообщения.

## Автозапуск на macOS

```bash
./install_launch_agent_macos.command
```

Логи будут лежать в `~/Documents/tg_sheet_monitor/tg_sheet_monitor.log` и `~/Documents/tg_sheet_monitor/tg_sheet_monitor.err.log`.

Остановить автозапуск:

```bash
launchctl unload "$HOME/Library/LaunchAgents/com.tg-pushes-ts26.sheet-monitor.plist"
```

## Важно про доступ к таблицам

Таблица должна открываться по ссылке хотя бы на чтение. Если Google возвращает HTML вместо TSV, бот сообщит ошибку доступа.
