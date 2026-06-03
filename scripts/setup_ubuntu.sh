#!/usr/bin/env bash
# =============================================================================
# setup_ubuntu.sh — One-time server setup for Interview Agent on Ubuntu 22.04+
#
# Run as root (or with sudo):
#   sudo bash scripts/setup_ubuntu.sh
#
# What this does:
#   1. Install system dependencies (Python 3.12, PostgreSQL, Redis)
#   2. Create a dedicated 'interview' system user
#   3. Copy project to /opt/interview-agent
#   4. Create Python virtualenv and install requirements
#   5. Install and enable systemd services
#   6. Set up log rotation
# =============================================================================

set -euo pipefail

APP_DIR="/opt/interview-agent"
APP_USER="interview"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

echo "=== Interview Agent Ubuntu Setup ==="
echo "Project source : $PROJECT_DIR"
echo "Install target : $APP_DIR"
echo ""

# ── 1. System dependencies ─────────────────────────────────────────────────────
echo "[1/6] Installing system dependencies..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3.12 \
    python3.12-venv \
    python3.12-dev \
    python3-pip \
    postgresql \
    postgresql-contrib \
    redis-server \
    build-essential \
    libpq-dev \
    git \
    curl \
    logrotate

# ── 2. Dedicated system user ───────────────────────────────────────────────────
echo "[2/6] Creating system user '$APP_USER'..."
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --no-create-home --shell /bin/false "$APP_USER"
    echo "  Created user: $APP_USER"
else
    echo "  User '$APP_USER' already exists — skipping"
fi

# ── 3. Copy project files ──────────────────────────────────────────────────────
echo "[3/6] Copying project to $APP_DIR..."
mkdir -p "$APP_DIR"
rsync -a --exclude='.git' --exclude='venv' --exclude='__pycache__' \
    --exclude='*.pyc' --exclude='.env' \
    "$PROJECT_DIR/" "$APP_DIR/"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

echo "  NOTE: Copy your .env file manually:"
echo "    sudo cp /path/to/your/.env $APP_DIR/.env"
echo "    sudo chmod 600 $APP_DIR/.env"
echo "    sudo chown $APP_USER:$APP_USER $APP_DIR/.env"

# ── 4. Python virtualenv ───────────────────────────────────────────────────────
echo "[4/6] Creating virtualenv and installing requirements..."
sudo -u "$APP_USER" python3.12 -m venv "$APP_DIR/venv"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip -q
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" -q
echo "  Dependencies installed"

# ── 5. systemd services ────────────────────────────────────────────────────────
echo "[5/6] Installing systemd services..."
cp "$APP_DIR/systemd/interview-api.service"    /etc/systemd/system/
cp "$APP_DIR/systemd/interview-worker.service" /etc/systemd/system/

systemctl daemon-reload
systemctl enable interview-api.service interview-worker.service
echo "  Services enabled (will auto-start on reboot)"
echo "  Start now with: sudo systemctl start interview-api interview-worker"

# ── 6. Log rotation ────────────────────────────────────────────────────────────
echo "[6/6] Configuring log rotation..."
cat > /etc/logrotate.d/interview-agent << 'EOF'
/var/log/interview-agent/*.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 interview interview
    postrotate
        systemctl kill --signal=HUP interview-api.service || true
    endscript
}
EOF

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Copy your .env:    sudo cp .env $APP_DIR/.env && sudo chmod 600 $APP_DIR/.env"
echo "  2. Run migrations:    sudo -u $APP_USER $APP_DIR/venv/bin/python -m alembic upgrade head"
echo "  3. Start services:    sudo systemctl start interview-api interview-worker"
echo "  4. Check status:      sudo systemctl status interview-api interview-worker"
echo "  5. Watch logs:        sudo journalctl -fu interview-api"
