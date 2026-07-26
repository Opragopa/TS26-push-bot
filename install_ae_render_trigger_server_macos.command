#!/bin/zsh
set -e

if [[ "$EUID" -eq 0 ]]; then
  echo "Не запускайте установщик через sudo."
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$HOME/Documents/tg_sheet_monitor"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/com.tg-pushes-ts26.ae-render-trigger.plist"
PYTHON_BIN="$(command -v python3)"
HOST="${AE_RENDER_TRIGGER_HOST:-127.0.0.1}"
PORT="${AE_RENDER_TRIGGER_PORT:-8765}"
TOKEN="${AE_RENDER_TRIGGER_TOKEN:-}"

mkdir -p "$DATA_DIR" "$PLIST_DIR"

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.tg-pushes-ts26.ae-render-trigger</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>$SCRIPT_DIR/ae_render_trigger_server.py</string>
    <string>--host</string>
    <string>$HOST</string>
    <string>--port</string>
    <string>$PORT</string>
    <string>--token</string>
    <string>$TOKEN</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$SCRIPT_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$DATA_DIR/ae_render_trigger.log</string>
  <key>StandardErrorPath</key>
  <string>$DATA_DIR/ae_render_trigger.err.log</string>
</dict>
</plist>
PLIST

chmod 644 "$PLIST_PATH"
/bin/launchctl bootout "gui/$UID/com.tg-pushes-ts26.ae-render-trigger" >/dev/null 2>&1 || true
/bin/launchctl bootstrap "gui/$UID" "$PLIST_PATH"
/bin/launchctl enable "gui/$UID/com.tg-pushes-ts26.ae-render-trigger"
/bin/launchctl kickstart -k "gui/$UID/com.tg-pushes-ts26.ae-render-trigger"

echo "AE render trigger server установлен и запущен."
echo "URL локально: http://$HOST:$PORT/render"
echo "Лог: $DATA_DIR/ae_render_trigger.log"
echo "Ошибки: $DATA_DIR/ae_render_trigger.err.log"
