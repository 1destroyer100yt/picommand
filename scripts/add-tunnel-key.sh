#!/usr/bin/env bash
# ============================================================
# Add a Pi's SSH key to the tunnel user's authorized_keys
# Run on the SERVER as root after registering a node.
#
# Usage: bash add-tunnel-key.sh <node-id> <path-to-node-pubkey.pub>
#   OR:  bash add-tunnel-key.sh <node-id>  (reads from stdin)
# ============================================================
set -euo pipefail

NODE_ID="${1:-}"
PUBKEY_FILE="${2:-}"
TUNNEL_USER="picommand-tunnel"
AUTH_KEYS="/var/lib/picommand-tunnel/.ssh/authorized_keys"

[[ -z "$NODE_ID" ]] && { echo "Usage: $0 <node-id> [pubkey-file]"; exit 1; }

if [[ -n "$PUBKEY_FILE" ]]; then
  PUBKEY=$(cat "$PUBKEY_FILE")
else
  echo "Paste the node's SSH public key (one line, then Ctrl-D):"
  PUBKEY=$(cat)
fi

# Restrict this key to only allow reverse port forwarding
# no-pty prevents interactive shell, permitopen restricts ports
RESTRICTED="no-pty,no-agent-forwarding,no-X11-forwarding,command=\"/bin/false\" ${PUBKEY}"

echo "$RESTRICTED" >> "$AUTH_KEYS"
echo "✓ Added key for node: $NODE_ID"
echo "  Tunnel user: $TUNNEL_USER"
