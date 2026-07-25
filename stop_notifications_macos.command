#!/bin/zsh
set -e

LABEL="com.tg-pushes-ts26.sheet-monitor"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

echo "Останавливаю уведомления TS26..."

if [ -f "$PLIST_PATH" ]; then
  launchctl unload "$PLIST_PATH" >/dev/null 2>&1 || true
fi

pkill -f "tg_sheet_monitor.py" >/dev/null 2>&1 || true

echo "Готово. Автозапуск выгружен, текущий монитор остановлен."
echo "Включить снова: ./install_launch_agent_macos.command"
