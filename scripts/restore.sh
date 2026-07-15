#!/usr/bin/env bash
# Postgres restore — restores aigraph from a backup file produced by backup.sh
#
# Usage:
#   ./scripts/restore.sh data/backups/aigraph_20260529_143000.sql.gz
#   ./scripts/restore.sh latest     # restore the most recent backup
#
# WARNING: this drops and re-creates objects in the aigraph database.
# All current data in aigraph is replaced by the backup.

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"

BACKUP_DIR="data/backups"

if [ $# -lt 1 ]; then
  echo "Usage: $0 <backup-file.sql.gz | latest>" >&2
  exit 1
fi

if [ "$1" = "latest" ]; then
  BACKUP_FILE=$(ls -1t "$BACKUP_DIR"/aigraph_*.sql.gz 2>/dev/null | head -1 || true)
  if [ -z "$BACKUP_FILE" ]; then
    echo "ERROR: no backups found in $BACKUP_DIR" >&2
    exit 1
  fi
  echo "Latest backup: $BACKUP_FILE"
else
  BACKUP_FILE="$1"
fi

if [ ! -f "$BACKUP_FILE" ]; then
  echo "ERROR: backup file not found: $BACKUP_FILE" >&2
  exit 1
fi

# Confirm with the user — restore is destructive
echo ""
echo "About to restore $BACKUP_FILE → aigraph"
echo "This will REPLACE all current data. Type 'yes' to continue:"
read -r CONFIRM
if [ "$CONFIRM" != "yes" ]; then
  echo "Aborted."
  exit 0
fi

# Verify db service is up
if ! docker compose ps db --status running --quiet | grep -q .; then
  echo "ERROR: db service is not running. Run 'docker compose up -d db' first." >&2
  exit 1
fi

# Stop the api to prevent writes during restore
echo "Stopping api service during restore..."
docker compose stop api

echo "Restoring..."
gunzip -c "$BACKUP_FILE" | docker compose exec -T db psql -U postgres -d aigraph
echo "  ✓ Restore complete"

echo "Restarting api service..."
docker compose start api

echo ""
echo "Done. Verify with: curl localhost:8000/graph/current"
