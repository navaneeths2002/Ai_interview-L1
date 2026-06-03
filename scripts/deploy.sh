#!/usr/bin/env bash
# =============================================================================
# deploy.sh — Deploy a code update to the running Ubuntu server
#
# Usage (from project root on the server):
#   bash scripts/deploy.sh
#
# What this does:
#   1. Pull latest code from git
#   2. Install any new Python dependencies
#   3. Run Alembic migrations
#   4. Restart both services
#   5. Verify services came back up
# =============================================================================

set -euo pipefail

APP_DIR="/opt/interview-agent"
APP_USER="interview"

echo "=== Interview Agent Deploy — $(date '+%Y-%m-%d %H:%M:%S') ==="

# Must run as root (needs systemctl restart)
if [[ $EUID -ne 0 ]]; then
    echo "ERROR: run as root or with sudo"
    exit 1
fi

cd "$APP_DIR"

# ── 1. Pull latest code ────────────────────────────────────────────────────────
echo "[1/5] Pulling latest code..."
sudo -u "$APP_USER" git pull --ff-only
echo "  Done"

# ── 2. Install dependencies (no-op if nothing changed) ────────────────────────
echo "[2/5] Installing/updating Python dependencies..."
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -r requirements.txt -q
echo "  Done"

# ── 3. Run migrations ──────────────────────────────────────────────────────────
echo "[3/5] Running Alembic migrations..."
sudo -u "$APP_USER" "$APP_DIR/venv/bin/python" -m alembic upgrade head
echo "  Done"

# ── 4. Restart services ────────────────────────────────────────────────────────
echo "[4/5] Restarting services..."
systemctl restart interview-api.service
systemctl restart interview-worker.service
echo "  Done"

# ── 5. Health check ────────────────────────────────────────────────────────────
echo "[5/5] Health check..."
sleep 3   # give uvicorn time to start

if curl -sf http://localhost:8000/health > /dev/null; then
    echo "  API: UP"
else
    echo "  ERROR: API health check failed"
    journalctl -u interview-api.service --since "1 minute ago" --no-pager | tail -20
    exit 1
fi

if systemctl is-active --quiet interview-worker.service; then
    echo "  Worker: UP"
else
    echo "  ERROR: Worker is not running"
    journalctl -u interview-worker.service --since "1 minute ago" --no-pager | tail -20
    exit 1
fi

echo ""
echo "=== Deploy complete ==="
