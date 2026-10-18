#!/bin/bash
# Копии базы с персональными данными — только для root
umask 077
# Ежедневный бэкап базы Ossetia Work. Запускается из cron.
DB="/srv/ossetia-work/database.db"
DIR="/root/backups"
LOG="$DIR/backup.log"
OUT="$DIR/db-$(date +%F).db"
TS="$(date '+%F %T')"

mkdir -p "$DIR"

# .backup — штатный механизм SQLite: снимает целостную копию на работающей базе.
if ! sqlite3 "$DB" ".backup '$OUT'" 2>>"$LOG"; then
  echo "$TS FAIL: .backup не выполнился" >> "$LOG"
  exit 1
fi

CHECK="$(sqlite3 "$OUT" 'PRAGMA integrity_check;' 2>>"$LOG" | head -1)"
if [ "$CHECK" != "ok" ]; then
  echo "$TS FAIL: integrity_check = $CHECK" >> "$LOG"
  exit 1
fi

find "$DIR" -maxdepth 1 -name 'db-*.db' -mtime +14 -delete

echo "$TS OK: $OUT ($(stat -c%s "$OUT") байт), копий всего: $(ls -1 "$DIR"/db-*.db 2>/dev/null | wc -l)" >> "$LOG"
