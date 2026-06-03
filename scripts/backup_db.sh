#!/usr/bin/env bash
# =============================================================================
# backup_db.sh — PostgreSQL backup for Interview Agent
#
# Set up as a daily cron job:
#   sudo crontab -e
#   0 2 * * * /opt/interview-agent/scripts/backup_db.sh >> /var/log/interview-agent/backup.log 2>&1
#
# Keeps 14 days of daily backups in /var/backups/interview-agent/
# =============================================================================

set -euo pipefail

# ── Config ─────────────────────────────────────────────────────────────────────
DB_NAME="${PGDATABASE:-interview_agent}"
DB_USER="${PGUSER:-postgres}"
BACKUP_DIR="/var/backups/interview-agent"
RETENTION_DAYS=14
TIMESTAMP="$(date '+%Y%m%d_%H%M%S')"
BACKUP_FILE="$BACKUP_DIR/${DB_NAME}_${TIMESTAMP}.sql.gz"

# ── Setup ──────────────────────────────────────────────────────────────────────
mkdir -p "$BACKUP_DIR"
chmod 750 "$BACKUP_DIR"

# ── Run backup ─────────────────────────────────────────────────────────────────
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting backup: $BACKUP_FILE"

pg_dump -U "$DB_USER" "$DB_NAME" | gzip > "$BACKUP_FILE"

SIZE="$(du -h "$BACKUP_FILE" | cut -f1)"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Backup complete: $BACKUP_FILE ($SIZE)"

# ── Prune old backups ──────────────────────────────────────────────────────────
DELETED=$(find "$BACKUP_DIR" -name "${DB_NAME}_*.sql.gz" -mtime +"$RETENTION_DAYS" -print -delete | wc -l)
if [[ "$DELETED" -gt 0 ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Pruned $DELETED backup(s) older than ${RETENTION_DAYS} days"
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Done"
