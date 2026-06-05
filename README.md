# PiCommand

**Self-hosted Raspberry Pi Remote Management Platform**

Secure, encrypted "phone-home" management of Raspberry Pi nodes from a central server.
No inbound ports required on remote networks.

```
Pi (remote) ──── WSS ────► Server (home) ◄──── You (browser)
     │                          │
     └──── SSH reverse tunnel ──┘
```

## Features

- 🔐 Ed25519 key authentication — only registered Pis can connect
- 🌐 Outbound-only connections from Pi (no firewall changes needed remotely)
- 💻 Remote terminal — execute commands from the web dashboard
- 📊 Real-time metrics — CPU, RAM, disk, temperature, uptime
- 🔔 Alerting — threshold-based alerts with acknowledgment
- 📁 File transfer — pull files from or push files to any Pi
- 🚀 Deployment — push scripts/updates to nodes
- 👥 RBAC — admin / operator / viewer roles
- 📋 Audit log — every command logged with timestamp and user
- 🔄 Auto-reconnect — exponential backoff, survives network outages
- 🖥️ SSH reverse tunnel — full SSH access via autossh

## Project Layout

```
picommand/
├── server/               # FastAPI server (runs on your home server/VM)
│   ├── main.py           # App entry point
│   ├── core/
│   │   ├── config.py     # Settings (env vars)
│   │   ├── security.py   # JWT, hashing, key verification
│   │   └── dependencies.py  # FastAPI auth dependencies
│   ├── db/
│   │   ├── schema.sql    # PostgreSQL schema
│   │   ├── models.py     # SQLAlchemy ORM models
│   │   └── database.py   # Async engine & sessions
│   ├── api/
│   │   ├── routes.py     # REST API endpoints
│   │   └── node_ws.py    # WebSocket node handler
│   ├── services/
│   │   ├── connection_manager.py  # Live node registry
│   │   └── alert_service.py       # Alert rule evaluation
│   └── static/
│       └── index.html    # Web dashboard (single-file SPA)
│
├── agent/                # Pi agent (runs on each Raspberry Pi)
│   ├── agent.py          # Main agent
│   └── requirements.txt
│
├── scripts/              # Setup scripts
│   ├── install-server.sh # Server installer (Ubuntu/Debian)
│   ├── install-agent.sh  # Pi agent installer
│   └── add-tunnel-key.sh # Add Pi SSH key for tunnel
│
└── docs/
    └── SETUP.md          # Full setup & operations guide
```

## Quick Start

```bash
# 1. Server (Proxmox VM / Ubuntu 22.04)
sudo bash scripts/install-server.sh

# 2. Each Raspberry Pi
export SERVER_URL=wss://picomand67.duckdns.org
export NODE_ID=Tiff-pi
sudo bash scripts/install-agent.sh

# 3. Register node in dashboard → start agent
sudo systemctl start picommand-agent
```

Full guide: [docs/SETUP.md](docs/SETUP.md)

## Security Model

| Layer | Mechanism |
|-------|-----------|
| Transport | TLS (WSS) |
| Authentication | Ed25519 challenge-response |
| Authorization | JWT + RBAC |
| Passwords | bcrypt |
| API tokens | SHA-256 hashed |
| Audit | All commands logged |
| Node isolation | Each node has independent key |

## Requirements

**Server**: Ubuntu 22.04 / Debian 12, 2GB RAM, Python 3.11+, PostgreSQL 14+, Redis

**Pi Agent**: Raspberry Pi OS (Debian 12), Python 3.9+, autossh
