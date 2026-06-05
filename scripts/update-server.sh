#!/usr/bin/env bash
# PiCommand Server Update Script
# Run from the repo root: bash scripts/update-server.sh
set -euo pipefail

PICOMMAND_DIR="/opt/picommand"
GREEN='\033[0;32m'; NC='\033[0m'
info() { echo -e "${GREEN}[UPDATE]${NC} $*"; }

info "Pulling latest changes..."
git pull

info "Copying server files..."
cp -r server/ "$PICOMMAND_DIR/"
chown -R picommand:picommand "$PICOMMAND_DIR/server"

info "Installing any new dependencies..."
"$PICOMMAND_DIR/venv/bin/pip" install -q -r "$PICOMMAND_DIR/server/requirements.txt"

info "Restarting service..."
systemctl restart picommand

sleep 2
systemctl is-active picommand && info "PiCommand updated and running!" || echo "Check: journalctl -u picommand"
