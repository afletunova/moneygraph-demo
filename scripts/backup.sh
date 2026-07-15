#!/usr/bin/env bash
# Postgres backup — dumps the aigraph database to ./data/backups/aigraph_YYYYMMDD_HHMMSS.sql.gz
# Rotates: keeps the most recent KEEP_LAST backups (default 14), deletes older ones.
#
# Usage:
#   ./scripts/backup.sh
#   KEEP_LAST=30 ./scripts/backup.sh
#
# Restore: ./scripts/restore.sh data/backups/aigraph_<timestamp>.sql.gz

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

BACKUP_DIR="data/backups"
KEEP_LAST="${KEEP_LAST:-14}"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
BACKUP_FILE="$BACKUP_DIR/aigraph_${TIMESTAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"

# Verify db service is up
if ! docker compose ps db --status running --quiet | grep -q .; then
  echo "ERROR: db service is not running. Run 'docker compose up -d db' first." >&2
  exit 1
fi

echo "Dumping aigraph → $BACKUP_FILE ..."
docker compose exec -T db pg_dump -U postgres -d aigraph --no-owner --clean --if-exists \
  | gzip > "$BACKUP_FILE"

SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "  ✓ Backup complete ($SIZE)"

# Rotate — keep last $KEEP_LAST
TO_DELETE=$(ls -1t "$BACKUP_DIR"/aigraph_*.sql.gz 2>/dev/null | tail -n +$((KEEP_LAST + 1)) || true)
if [ -n "$TO_DELETE" ]; then
  echo "Rotating: removing $(echo "$TO_DELETE" | wc -l | tr -d ' ') old backup(s) beyond KEEP_LAST=$KEEP_LAST"
  echo "$TO_DELETE" | xargs rm -f
fi

# Summary
COUNT=$(ls -1 "$BACKUP_DIR"/aigraph_*.sql.gz 2>/dev/null | wc -l | tr -d ' ')
echo "  Total backups on disk: $COUNT"
