#!/bin/bash
umask 077
# Еженедельная отправка свежей копии базы владельцу в Telegram.
DIR="/root/backups"
LOG="$DIR/backup.log"
TS="$(date '+%F %T')"
CHAT_ID="${ADMIN_CHAT_ID:?укажите ADMIN_CHAT_ID}"

# Токен берём из закрытого файла службы, чтобы не плодить его копии по файлам.
TOKEN="$(grep -oP '^BOT_TOKEN=\K.+' /etc/ossetia-work/secrets.env)"
if [ -z "$TOKEN" ]; then
  echo "$TS TG FAIL: не найден BOT_TOKEN в юните" >> "$LOG"; exit 1
fi

LAST="$(ls -1t "$DIR"/db-*.db 2>/dev/null | head -1)"
if [ -z "$LAST" ]; then
  echo "$TS TG FAIL: в $DIR нет ни одной копии" >> "$LOG"; exit 1
fi

RESP="$(curl -s --max-time 120 \
  -F "chat_id=$CHAT_ID" \
  -F "document=@$LAST" \
  -F "caption=Бэкап базы Ossetia Work от $(date +%F). Сохрани файл, если нужен откат." \
  "https://api.telegram.org/bot$TOKEN/sendDocument")"

if echo "$RESP" | grep -q '"ok":true'; then
  echo "$TS TG OK: отправлен $LAST ($(stat -c%s "$LAST") байт)" >> "$LOG"
else
  echo "$TS TG FAIL: $(echo "$RESP" | sed -E "s/$TOKEN/<HIDDEN>/g" | head -c 300)" >> "$LOG"
  exit 1
fi
