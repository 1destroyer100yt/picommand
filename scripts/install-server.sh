#!/usr/bin/env bash
# ============================================================
# PiCommand Server Installer
# Tested: Ubuntu 22.04 / Debian 12 (Proxmox VM or bare metal)
# Run as root: bash install-server.sh
# ============================================================
set -euo pipefail

PICOMMAND_USER="picommand"
PICOMMAND_DIR="/opt/picommand"
PICOMMAND_DATA="/var/lib/picommand"
PICOMMAND_ETC="/etc/picommand"
PICOMMAND_LOG="/var/log/picommand"
DB_NAME="picommand"
DB_USER="picommand"
DB_PASS="$(openssl rand -hex 24)"
SECRET_KEY="$(openssl rand -hex 32)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Run as root"

info "=== PiCommand Server Installer ==="
info "Installing on: $(lsb_release -d | cut -f2)"

# ── System packages ──────────────────────────────────────────────────────────
info "Updating packages…"
apt-get update -qq
apt-get install -y -q \
  python3 python3-pip python3-venv \
  postgresql postgresql-contrib \
  redis-server \
  nginx \
  certbot python3-certbot-nginx \
  openssh-server \
  autossh \
  git curl wget \
  ufw fail2ban \
  libpq-dev \
  build-essential

# ── System user ───────────────────────────────────────────────────────────────
if ! id "$PICOMMAND_USER" &>/dev/null; then
  useradd --system --shell /bin/bash --home "$PICOMMAND_DIR" --create-home "$PICOMMAND_USER"
  info "Created user: $PICOMMAND_USER"
fi

# ── Directories ───────────────────────────────────────────────────────────────
mkdir -p "$PICOMMAND_DATA/uploads" "$PICOMMAND_ETC/ssh/authorized" "$PICOMMAND_LOG"
chown -R "$PICOMMAND_USER:$PICOMMAND_USER" "$PICOMMAND_DATA" "$PICOMMAND_ETC" "$PICOMMAND_LOG"
chmod 750 "$PICOMMAND_ETC/ssh"

# ── PostgreSQL ────────────────────────────────────────────────────────────────
info "Configuring PostgreSQL…"
systemctl enable --now postgresql

sudo -u postgres psql -c "
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
    CREATE ROLE ${DB_USER} WITH LOGIN PASSWORD '${DB_PASS}';
  ELSE
    ALTER ROLE ${DB_USER} WITH PASSWORD '${DB_PASS}';
  END IF;
END
\$\$;
" 2>/dev/null || true

sudo -u postgres createdb -O "$DB_USER" "$DB_NAME" 2>/dev/null || true
sudo -u postgres psql "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";" 2>/dev/null || true
sudo -u postgres psql "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS \"pgcrypto\";" 2>/dev/null || true

# ── Redis ─────────────────────────────────────────────────────────────────────
info "Configuring Redis…"
systemctl enable --now redis-server

# ── Python environment ────────────────────────────────────────────────────────
info "Installing Python dependencies…"
python3 -m venv "$PICOMMAND_DIR/venv"
"$PICOMMAND_DIR/venv/bin/pip" install -q --upgrade pip wheel
"$PICOMMAND_DIR/venv/bin/pip" install -q -r "$PICOMMAND_DIR/server/requirements.txt" || true

# ── Copy application ──────────────────────────────────────────────────────────
if [[ -d "./server" ]]; then
  cp -r ./server "$PICOMMAND_DIR/"
  cp -r ./agent "$PICOMMAND_DIR/"
  chown -R "$PICOMMAND_USER:$PICOMMAND_USER" "$PICOMMAND_DIR"
fi

# ── Environment file ──────────────────────────────────────────────────────────
info "Writing configuration…"
cat > "$PICOMMAND_DIR/.env" <<EOF
SECRET_KEY=${SECRET_KEY}
DATABASE_URL=postgresql+asyncpg://${DB_USER}:${DB_PASS}@localhost:5432/${DB_NAME}
REDIS_URL=redis://localhost:6379/0
DEBUG=false
HOST=127.0.0.1
PORT=8000
UPLOAD_DIR=${PICOMMAND_DATA}/uploads
SSH_AUTHORIZED_KEYS_DIR=${PICOMMAND_ETC}/ssh/authorized
TUNNEL_PORT_RANGE_START=12000
TUNNEL_PORT_RANGE_END=13000
EOF
chmod 600 "$PICOMMAND_DIR/.env"
chown "$PICOMMAND_USER:$PICOMMAND_USER" "$PICOMMAND_DIR/.env"

# ── Database schema ───────────────────────────────────────────────────────────
info "Applying database schema…"
PGPASSWORD="$DB_PASS" psql -h localhost -U "$DB_USER" -d "$DB_NAME" \
  -f "$PICOMMAND_DIR/server/db/schema.sql" || warn "Schema already applied or error (check manually)"

# ── Systemd service ───────────────────────────────────────────────────────────
info "Installing systemd service…"
cat > /etc/systemd/system/picommand.service <<EOF
[Unit]
Description=PiCommand Server
After=network.target postgresql.service redis.service
Requires=postgresql.service

[Service]
Type=simple
User=${PICOMMAND_USER}
WorkingDirectory=${PICOMMAND_DIR}
EnvironmentFile=${PICOMMAND_DIR}/.env
ExecStart=${PICOMMAND_DIR}/venv/bin/uvicorn server.main:app \\
  --host \${HOST} \\
  --port \${PORT} \\
  --workers 2 \\
  --log-level info
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=picommand

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
PrivateTmp=true
ReadWritePaths=${PICOMMAND_DATA} ${PICOMMAND_LOG}

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable picommand

# ── SSH tunnel user ───────────────────────────────────────────────────────────
info "Creating SSH tunnel user…"
if ! id "picommand-tunnel" &>/dev/null; then
  useradd --system --shell /usr/sbin/nologin \
    --home /var/lib/picommand-tunnel \
    --create-home picommand-tunnel
  mkdir -p /var/lib/picommand-tunnel/.ssh
  touch /var/lib/picommand-tunnel/.ssh/authorized_keys
  chmod 700 /var/lib/picommand-tunnel/.ssh
  chmod 600 /var/lib/picommand-tunnel/.ssh/authorized_keys
  chown -R picommand-tunnel:picommand-tunnel /var/lib/picommand-tunnel
fi

# ── SSHD config for tunnels ───────────────────────────────────────────────────
info "Configuring sshd for reverse tunnels…"
cat >> /etc/ssh/sshd_config <<'EOF'

# PiCommand tunnel user
Match User picommand-tunnel
  AllowTcpForwarding remote
  X11Forwarding no
  AllowAgentForwarding no
  PermitTTY no
  ForceCommand /bin/false
  GatewayPorts yes
EOF
systemctl reload sshd || true

# ── Nginx ─────────────────────────────────────────────────────────────────────
info "Configuring nginx reverse proxy…"
DOMAIN="${DOMAIN:-_}"  # set DOMAIN env var before running for HTTPS
cat > /etc/nginx/sites-available/picommand <<EOF
server {
    listen 80;
    server_name ${DOMAIN};

    # WebSocket support
    location /ws/ {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_read_timeout 86400;
    }

    # SSE needs no buffering
    location /api/events {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Connection '';
        proxy_buffering off;
        proxy_cache off;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        chunked_transfer_encoding on;
    }

    # Everything else
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        client_max_body_size 512M;
    }
}
EOF

ln -sf /etc/nginx/sites-available/picommand /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl enable --now nginx && systemctl reload nginx

# ── Firewall ──────────────────────────────────────────────────────────────────
info "Configuring firewall…"
ufw --force enable
ufw allow ssh
ufw allow http
ufw allow https
ufw allow 8000/tcp comment 'picommand-dev'

# ── Fail2ban ──────────────────────────────────────────────────────────────────
info "Configuring fail2ban…"
cat > /etc/fail2ban/jail.d/picommand.conf <<'EOF'
[sshd]
enabled = true
maxretry = 5
bantime = 1h
EOF
systemctl enable --now fail2ban

# ── Start service ─────────────────────────────────────────────────────────────
info "Starting PiCommand…"
systemctl start picommand
sleep 2
systemctl is-active picommand && info "PiCommand is running!" || warn "Check: journalctl -u picommand"

# ── Done ──────────────────────────────────────────────────────────────────────
SERVER_IP=$(hostname -I | awk '{print $1}')
echo ""
echo "═══════════════════════════════════════════════════════════"
echo " PiCommand Server installed successfully!"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo " Dashboard:    http://${SERVER_IP}"
echo " API Docs:     http://${SERVER_IP}/api/docs  (debug mode only)"
echo ""
echo " Default login: admin / changeme"
echo " ⚠  CHANGE THE PASSWORD IMMEDIATELY after first login!"
echo ""
echo " Database password: ${DB_PASS}"
echo " Secret key:        ${SECRET_KEY}"
echo " Config file:       ${PICOMMAND_DIR}/.env"
echo ""
echo " Next steps:"
echo "   1. Change admin password in the dashboard"
echo "   2. Set DOMAIN= and run: certbot --nginx -d your.domain.com"
echo "   3. Register Pi nodes in the dashboard"
echo "   4. Copy agent/ to each Pi and run install-agent.sh"
echo ""
echo "═══════════════════════════════════════════════════════════"
