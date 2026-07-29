#!/bin/zsh
set -e

if [[ "$EUID" -eq 0 ]]; then
  echo "Не запускайте установщик через sudo: After Effects доступен только из пользовательской сессии."
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$HOME/Documents/tg_sheet_monitor"
PLIST_DIR="$HOME/Library/LaunchAgents"
PLIST_PATH="$PLIST_DIR/com.tg-pushes-ts26.ae-render-worker.plist"
PYTHON_BIN="$(command -v python3)"
POLL_INTERVAL="${AE_RENDER_WORKER_INTERVAL:-60}"

mkdir -p "$DATA_DIR" "$PLIST_DIR"

if [[ -e "$SCRIPT_DIR/data/ae_render_queue.json" && ! -r "$SCRIPT_DIR/data/ae_render_queue.json" ]]; then
  echo "Нет доступа к очереди рендера: $SCRIPT_DIR/data/ae_render_queue.json"
  echo "Сначала запустите stop_ae_render_worker_macos.command для исправления старого root-запуска."
  exit 1
fi
for log_file in "$DATA_DIR/ae_render_worker.log" "$DATA_DIR/ae_render_worker.err.log"; do
  if [[ -e "$log_file" && ! -w "$log_file" ]]; then
    echo "Нет доступа к логу воркера: $log_file"
    echo "Сначала запустите stop_ae_render_worker_macos.command для исправления старого root-запуска."
    exit 1
  fi
done

cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.tg-pushes-ts26.ae-render-worker</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>$SCRIPT_DIR/ae_render_worker.py</string>
    <string>--poll-sheets</string>
    <string>--interval</string>
    <string>$POLL_INTERVAL</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$SCRIPT_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$DATA_DIR/ae_render_worker.log</string>
  <key>StandardErrorPath</key>
  <string>$DATA_DIR/ae_render_worker.err.log</string>
</dict>
</plist>
PLIST

chmod 644 "$PLIST_PATH"
/bin/launchctl bootout "gui/$UID/com.tg-pushes-ts26.ae-render-worker" >/dev/null 2>&1 || true
/bin/launchctl bootstrap "gui/$UID" "$PLIST_PATH"
/bin/launchctl enable "gui/$UID/com.tg-pushes-ts26.ae-render-worker"
/bin/launchctl kickstart -k "gui/$UID/com.tg-pushes-ts26.ae-render-worker"

echo "AE render worker установлен и запущен."
echo "Лог: $DATA_DIR/ae_render_worker.log"
echo "Ошибки: $DATA_DIR/ae_render_worker.err.log"
