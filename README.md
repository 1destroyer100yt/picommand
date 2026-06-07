# PiCommand

**Self-hosted Raspberry Pi Remote Management Platform**

Manage all your Raspberry Pis from anywhere — no open ports required on remote networks. Pis phone home over encrypted WebSocket connections to your central server.

```
Pi (gf's house) ──── WSS ────► Server (your home) ◄──── You (browser / Home Assistant)
Pi (basement)   ──── WSS ────►        │
Pi (garage)     ──── WSS ────►        │
                                  PostgreSQL + Redis
```

---

## Features

| | |
|---|---|
| 🔐 **Ed25519 auth** | Only registered Pis can connect — challenge-response, no passwords |
| 🌐 **No inbound ports** | Pis initiate outbound connections, works through any firewall/NAT |
| 💻 **Remote terminal** | Run commands on any Pi from the web dashboard |
| 📊 **Real-time metrics** | CPU, RAM, disk, temperature, load, uptime — live graphs |
| 🔔 **Alerting** | Threshold-based alerts with acknowledgment |
| 📁 **File transfer** | Push or pull files to/from any Pi |
| 🚀 **Deployments** | Push and run scripts across nodes |
| 👥 **RBAC** | Admin / Operator / Viewer roles |
| 📋 **Audit log** | Every command logged with user and timestamp |
| 🔄 **Auto-reconnect** | Exponential backoff, survives network outages |
| 🔌 **SSH reverse tunnel** | Full SSH access via autossh |
| 🏠 **Home Assistant** | HACS integration — sensors, buttons, run_command service |

---

## Quick Start

### 1. Server (Ubuntu 22.04 / Debian 12 VM)

```bash
git clone https://github.com/1destroyer100yt/picommand.git
cd picommand
sudo bash scripts/install-server.sh
```

Default login: `admin` / `changeme` — **change this immediately.**

### 2. Each Raspberry Pi

```bash
export SERVER_URL=wss://your-domain.com
export NODE_ID=my-pi
sudo bash scripts/install-agent.sh
```

Then register the node in the dashboard with the Pi's public key:
```bash
sudo cat /etc/picommand/keys/node_key.pub
```

### 3. Update server

```bash
sudo bash scripts/update-server.sh
```

---

## Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Install via HACS:

1. **HACS → Custom Repositories** → Add `https://github.com/1destroyer100yt/picommand` as **Integration**
2. Download **PiCommand** from HACS
3. Restart Home Assistant
4. **Settings → Devices & Services → Add Integration → PiCommand**
5. Enter your PiCommand server URL and credentials

### What you get in HA

**Sensors** (per node): CPU %, RAM %, Temperature, Disk %, Uptime, IP address, Load average

**Binary Sensors**: Online/Offline connectivity status

**Buttons**: Reboot, apt update & upgrade

**Service**: `picommand.run_command`

```yaml
service: picommand.run_command
data:
  node_id: my-pi
  command: "systemctl restart nginx"
  timeout: 30
```

### Example Automations

```yaml
# Alert when Pi goes offline
automation:
  trigger:
    platform: state
    entity_id: binary_sensor.my_pi_online
    to: "off"
    for: "00:02:00"
  action:
    service: notify.mobile_app
    data:
      message: "my-pi is offline!"

# Alert when Pi is too hot
automation:
  trigger:
    platform: numeric_state
    entity_id: sensor.my_pi_temperature
    above: 75
  action:
    service: notify.mobile_app
    data:
      message: "Pi temp: {{ states('sensor.my_pi_temperature') }}°C"
```

---

## Project Layout

```
picommand/
├── server/                        # FastAPI server
│   ├── api/routes.py              # REST API
│   ├── api/node_ws.py             # WebSocket node handler
│   ├── core/                      # Auth, config, security
│   ├── db/                        # PostgreSQL models + schema
│   ├── services/                  # Connection manager, alerts
│   └── static/index.html          # Web dashboard (single-file SPA)
├── agent/agent.py                 # Pi agent
├── custom_components/picommand/   # Home Assistant integration (HACS)
├── scripts/                       # install-server, install-agent, update
└── docs/SETUP.md                  # Full setup guide
```

---

## Architecture

- **Transport**: WebSocket (WSS) — outbound from Pi, works through any NAT/firewall
- **Node auth**: Ed25519 challenge-response
- **User auth**: JWT + bcrypt
- **Database**: PostgreSQL (async asyncpg + SQLAlchemy)
- **Dashboard**: Vanilla JS SPA, no build step
- **SSH access**: autossh reverse tunnel

## Security

| Layer | Mechanism |
|-------|-----------|
| Transport | TLS (WSS) |
| Node auth | Ed25519 challenge-response |
| User auth | JWT (HS256) |
| Passwords | bcrypt |
| RBAC | Admin / Operator / Viewer |
| Audit | All commands logged |

## Requirements

**Server**: Ubuntu 22.04+ or Debian 12, 2GB RAM, Python 3.11+, PostgreSQL 14+, Redis

**Pi Agent**: Raspberry Pi OS (Bookworm), Python 3.9+, autossh

**Home Assistant**: 2024.1+ with HACS
