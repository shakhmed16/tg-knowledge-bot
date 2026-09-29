#!/usr/bin/env bash
# Снимок базы знаний. Копировать knowledge.db обычным cp нельзя: рядом лежит
# WAL-журнал, и копия может оказаться битой. VACUUM INTO делает целостный
# снимок на работающем боте, останавливать его не нужно.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/kb-bot}"
BACKUP_DIR="${BACKUP_DIR:-$APP_DIR/backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%Y-%m-%d_%H%M)"
DEST="$BACKUP_DIR/knowledge_$STAMP.db"

sqlite3 "$APP_DIR/knowledge.db" "VACUUM INTO '$DEST'"
gzip -f "$DEST"

# Чистим старые снимки
find "$BACKUP_DIR" -name 'knowledge_*.db.gz' -mtime "+$KEEP_DAYS" -delete

echo "Бэкап готов: $DEST.gz ($(du -h "$DEST.gz" | cut -f1))"
