#!/bin/zsh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$HOME/Documents/tg_sheet_monitor"
PLIST_PATH="$HOME/Library/LaunchAgents/com.tg-pushes-ts26.ae-render-worker.plist"
LABEL="com.tg-pushes-ts26.ae-render-worker"

/bin/launchctl bootout "gui/$UID/$LABEL" >/dev/null 2>&1 || true
/usr/bin/pkill -u "$UID" -f "$SCRIPT_DIR/ae_render_worker.py" >/dev/null 2>&1 || true

needs_repair=false
if /usr/bin/pgrep -u root -f "$SCRIPT_DIR/ae_render_worker.py" >/dev/null 2>&1; then
  needs_repair=true
fi
for path in "$SCRIPT_DIR/data/ae_render_queue.json" "$LOG_DIR/ae_render_worker.log" "$LOG_DIR/ae_render_worker.err.log" "$PLIST_PATH"; do
  if [[ -e "$path" && ! -w "$path" ]]; then
    needs_repair=true
  fi
done

if [[ "$needs_repair" == true ]]; then
  echo "Найден старый воркер, ошибочно запущенный от root."
  echo "macOS запросит пароль администратора, чтобы остановить его и вернуть права файлам."
  /usr/bin/osascript - "$PLIST_PATH" "$SCRIPT_DIR" "$LOG_DIR" <<'APPLESCRIPT'
on run argv
    set plistPath to item 1 of argv
    set scriptDir to item 2 of argv
    set logDir to item 3 of argv
    set userName to short user name of (system info)
    set shellCommand to "/bin/launchctl unload " & quoted form of plistPath & " >/dev/null 2>&1 || true; " & ¬
        "/usr/bin/pkill -f " & quoted form of (scriptDir & "/ae_render_worker.py") & " >/dev/null 2>&1 || true; " & ¬
        "/usr/sbin/chown -R " & quoted form of userName & ":staff " & ¬
        quoted form of (scriptDir & "/data") & " /private/tmp/ts26-ae-render " & quoted form of logDir & " " & quoted form of plistPath
    do shell script shellCommand with administrator privileges
end run
APPLESCRIPT
fi

echo "AE render worker остановлен."
