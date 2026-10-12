#!/usr/bin/env bash
# Щоденна резервна копія SQLite (безпечно під час роботи бота — через backup API).
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/usousdt-bot}"
DB="${DB:-$APP_DIR/data/bot.db}"
DEST="${DEST:-$APP_DIR/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"
PY="${PY:-$APP_DIR/.venv/bin/python}"

[ -f "$DB" ] || { echo "Бази $DB ще немає — пропускаю"; exit 0; }
mkdir -p "$DEST"
OUT="$DEST/bot-$(date -u +%Y%m%d-%H%M).db"

"$PY" - "$DB" "$OUT" <<'PYEOF'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
dst.close()
src.close()
PYEOF

gzip -f "$OUT"
find "$DEST" -name 'bot-*.db.gz' -mtime +"$KEEP_DAYS" -delete
echo "Резервна копія: $OUT.gz"
