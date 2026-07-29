#!/bin/zsh
set -euo pipefail

TEMP_ROOT="/private/tmp/ts26-ae-render"

if [[ ! -d "$TEMP_ROOT" ]]; then
  echo "Временная папка уже отсутствует: $TEMP_ROOT"
  exit 0
fi

count=$(/usr/bin/find "$TEMP_ROOT" -type f -name '*.aep' | /usr/bin/wc -l | /usr/bin/tr -d ' ')
if [[ "$count" == "0" ]]; then
  echo "Временных проектов не найдено: $TEMP_ROOT"
  exit 0
fi

echo "Найдено временных проектов: $count"
echo "Перемещаю папку в Корзину: $TEMP_ROOT"
osascript -e 'tell application "Finder" to delete POSIX file "/private/tmp/ts26-ae-render"'
echo "Готово. Временные проекты перемещены в Корзину."
