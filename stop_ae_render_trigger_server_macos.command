#!/bin/zsh
set -e

LABEL="com.tg-pushes-ts26.ae-render-trigger"
PLIST_PATH="$HOME/Library/LaunchAgents/$LABEL.plist"

/bin/launchctl bootout "gui/$UID/$LABEL" >/dev/null 2>&1 || true
/usr/bin/pkill -f "ae_render_trigger_server.py" >/dev/null 2>&1 || true

echo "AE render trigger server остановлен."
echo "Plist оставлен на месте: $PLIST_PATH"
