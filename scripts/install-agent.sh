#!/usr/bin/env bash
# ============================================================
# PiCommand Agent Installer — runs on each Raspberry Pi
# Tested: Raspberry Pi OS Lite (Debian 12) / Ubuntu 22.04
# Run as root: bash install-agent.sh
#
# Required env vars (or edit config below):
#   SERVER_URL=wss://your-server.example.com
#   NODE_ID=your-unique-node-id
# ============================================================
set -euo pipefail

AGENT_DIR="/opt/picommand-agent"
AGENT_ETC="/etc/picommand"
AGENT_LOG="/var/log"
VENV="$AGENT_DIR/venv"

SERVER_URL="${SERVER_URL:-wss://CHANGE_ME}"
NODE_ID="${NODE_ID:-$(hostname | tr '[:upper:]' '[:lower:]' | sed 's/[^a-z0-9-]/-/g')}"
SERVER_HOST="${SERVER_HOST:-CHANGE_ME}"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

[[ $EUID -ne 0 ]] && error "Run as root"

info "=== PiCommand Agent Installer ==="
info "Node ID: $NODE_ID"
info "Server:  $SERVER_URL"

# ── Packages ──────────────────────────────────────────────────────────────────
apt-get update -qq
apt-get install -y -q python3 python3-pip python3-venv autossh openssh-client

# ── Directories ───────────────────────────────────────────────────────────────
mkdir -p "$AGENT_DIR" "$AGENT_ETC/keys"
chmod 700 "$AGENT_ETC/keys"

# ── Copy agent ────────────────────────────────────────────────────────────────
if [[ -f "./agent/agent.py" ]]; then
  cp ./agent/agent.py "$AGENT_DIR/"
  cp ./agent/requirements.txt "$AGENT_DIR/"
fi

# ── Python env ────────────────────────────────────────────────────────────────
info "Installing Python dependencies…"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$AGENT_DIR/requirements.txt"

# ── Generate keys ─────────────────────────────────────────────────────────────
KEY_PATH="$AGENT_ETC/keys/node_key"
if [[ ! -f "$KEY_PATH" ]]; then
  info "Generating Ed25519 keypair…"
  "$VENV/bin/python3" "$AGENT_DIR/agent.py" --generate-keys
fi

# ── Config file ───────────────────────────────────────────────────────────────
info "Writing agent config…"
cat > "$AGENT_ETC/agent.conf" <<EOF
[server]
url = ${SERVER_URL}
verify_ssl = true

[node]
node_id = ${NODE_ID}
display_name = $(hostname)

[keys]
private_key_path = ${KEY_PATH}

[tunnel]
enabled = true
server_host = ${SERVER_HOST}
server_ssh_port = 22
server_user = picommand-tunnel
tunnel_port = 0
local_port = 22
autossh_flags = -M 0 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes

[metrics]
interval_seconds = 30
services_to_monitor = ssh,cron

[reconnect]
initial_delay = 5
max_delay = 300
backoff_factor = 2
EOF
chmod 640 "$AGENT_ETC/agent.conf"

# ── systemd service ───────────────────────────────────────────────────────────
info "Installing systemd service…"
cat > /etc/systemd/system/picommand-agent.service <<EOF
[Unit]
Description=PiCommand Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=${VENV}/bin/python3 ${AGENT_DIR}/agent.py --config ${AGENT_ETC}/agent.conf
Restart=always
RestartSec=10
RestartPreventExitStatus=0
StartLimitIntervalSec=600
StartLimitBurst=20
StandardOutput=journal
StandardError=journal
SyslogIdentifier=picommand-agent

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable picommand-agent

# ── Show public key ───────────────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════════════════════"
echo " PiCommand Agent installed on $(hostname)"
echo "═══════════════════════════════════════════════════════════"
echo ""
echo " Node ID: ${NODE_ID}"
echo ""
echo " PUBLIC KEY (register this on the server dashboard):"
echo " ──────────────────────────────────────────────────"
"$VENV/bin/python3" "$AGENT_DIR/agent.py" --show-pubkey
echo " ──────────────────────────────────────────────────"
echo ""
echo " Next steps:"
echo "   1. Copy the public key above"
echo "   2. Register this node on the server dashboard:"
echo "      Dashboard → Register Node → paste the public key"
echo "   3. Start the agent:"
echo "      systemctl start picommand-agent"
echo "   4. Check status:"
echo "      systemctl status picommand-agent"
echo "      journalctl -u picommand-agent -f"
echo ""
echo " Config: ${AGENT_ETC}/agent.conf"
echo "═══════════════════════════════════════════════════════════"
