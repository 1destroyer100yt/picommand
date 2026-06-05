# PiCommand — Complete Setup & Operations Guide

## Architecture Overview

```
Internet
    │
    │  (WSS + HTTPS)
    ▼
┌─────────────────────────────────────────┐
│ Your Home Server / Proxmox VM           │
│                                         │
│  nginx (port 80/443)                    │
│    └── proxy → uvicorn :8000            │
│                                         │
│  PiCommand Server (FastAPI)             │
│    ├── REST API  (/api/*)               │
│    ├── WebSocket (/ws/node/{id})        │
│    ├── SSE       (/api/events)          │
│    └── Dashboard (/index.html)          │
│                                         │
│  PostgreSQL (metrics, audit, nodes)     │
│  Redis      (session cache)             │
│  sshd       (reverse tunnel target)     │
└─────────────────────────────────────────┘
         ▲          ▲           ▲
         │ WSS      │ WSS       │ WSS
         │ autossh  │ autossh   │ autossh
         │          │           │
    ┌────┴──┐  ┌────┴──┐  ┌────┴──┐
    │ Pi 1  │  │ Pi 2  │  │ Pi 3  │
    │ Garage│  │ Office│  │Remote │
    └───────┘  └───────┘  └───────┘
```

## Connection Flow

### WebSocket (management channel)
1. Pi boots → `picommand-agent.service` starts
2. Agent loads Ed25519 private key from `/etc/picommand/keys/node_key`
3. Agent opens WSS connection to `wss://server/ws/node/{node_id}`
4. Server sends random 32-byte challenge
5. Agent signs challenge with private key → sends signature
6. Server verifies against stored public key → sends `auth_ok`
7. Agent begins sending metrics every 30s
8. Server can now send `execute_command` messages

### SSH Reverse Tunnel (shell access)
1. Agent runs `autossh` → establishes `ssh -R 12001:localhost:22 picommand-tunnel@server`
2. Server now has port 12001 bound locally → forwards to Pi's SSH port 22
3. From server: `ssh -p 12001 pi@localhost` → direct SSH shell into Pi

## Quick Start

### 1. Server Installation (Proxmox VM / Ubuntu 22.04)

```bash
# On your Proxmox VM or Linux server
git clone https://github.com/you/picommand
cd picommand

# Optional: set your domain for TLS
export DOMAIN=picommand.yourdomain.com

sudo bash scripts/install-server.sh
```

After install:
- Dashboard: `http://your-server-ip`
- Default login: `admin` / `changeme` ← **change this immediately**

### 2. Get TLS Certificate (production)

```bash
sudo certbot --nginx -d picommand.yourdomain.com
```

Certbot will auto-update nginx config. Certificates auto-renew via cron.

### 3. Agent Installation (each Raspberry Pi)

```bash
# Copy picommand directory to the Pi
scp -r picommand pi@192.168.1.x:~/

# SSH to Pi and run installer
ssh pi@192.168.1.x
cd ~/picommand
export SERVER_URL=wss://picommand.yourdomain.com
export NODE_ID=garage-pi
sudo bash scripts/install-agent.sh
```

Installer will:
1. Install Python deps
2. Generate Ed25519 keypair
3. Write config to `/etc/picommand/agent.conf`
4. Install systemd service (not started yet)
5. Print the public key

### 4. Register Node on Server

1. Copy the public key printed by the installer
2. Open dashboard → click **Register Node**
3. Fill in: Node ID (e.g. `garage-pi`), Display Name, paste public key
4. Click Register

### 5. Start Agent and Enable Tunnel

```bash
# On the Pi
sudo systemctl start picommand-agent
sudo systemctl status picommand-agent

# Watch the logs
journalctl -u picommand-agent -f
```

You should see in logs:
```
Connected to wss://picommand.yourdomain.com
Authentication successful
```

And in the dashboard, the node turns **Online** ✅

## SSH Reverse Tunnel Setup

The WebSocket channel handles command execution. For a full SSH shell:

### Server side: add Pi's SSH key to tunnel user

```bash
# First, get Pi's SSH public key (not the agent key, the system SSH key)
# On Pi:
cat /home/pi/.ssh/id_ed25519.pub
# If no key: ssh-keygen -t ed25519

# On server:
sudo bash scripts/add-tunnel-key.sh garage-pi
# Paste the Pi's SSH public key when prompted
```

### Agent config: enable tunnel

Edit `/etc/picommand/agent.conf` on the Pi:

```ini
[tunnel]
enabled = true
server_host = picommand.yourdomain.com
server_ssh_port = 22
server_user = picommand-tunnel
tunnel_port = 12001     # assigned port from server dashboard
local_port = 22
```

Restart agent: `sudo systemctl restart picommand-agent`

### Connect via tunnel from server

```bash
# Direct SSH to Pi through reverse tunnel
ssh -p 12001 pi@localhost

# Or use the assigned tunnel port from the dashboard
ssh -p <tunnel_port> pi@localhost
```

## Configuration Reference

### Server: `/opt/picommand/.env`

| Variable | Default | Description |
|----------|---------|-------------|
| `SECRET_KEY` | required | 64-char random hex |
| `DATABASE_URL` | required | PostgreSQL async DSN |
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection |
| `PORT` | `8000` | uvicorn listen port |
| `TUNNEL_PORT_RANGE_START` | `12000` | First tunnel port |
| `TUNNEL_PORT_RANGE_END` | `13000` | Last tunnel port (1000 nodes max) |
| `WS_HEARTBEAT_INTERVAL` | `30` | Seconds between heartbeats |
| `METRICS_RETENTION_DAYS` | `7` | Days to keep per-minute metrics |
| `DEBUG` | `false` | Enable API docs at /api/docs |

### Agent: `/etc/picommand/agent.conf`

| Key | Description |
|-----|-------------|
| `server.url` | Server WebSocket URL (`wss://...`) |
| `node.node_id` | Unique node identifier (alphanumeric + hyphens) |
| `keys.private_key_path` | Path to Ed25519 private key |
| `tunnel.enabled` | Enable SSH reverse tunnel |
| `tunnel.tunnel_port` | Assigned remote port |
| `metrics.interval_seconds` | How often to push metrics (default: 30) |
| `metrics.services_to_monitor` | Comma-separated systemd service names |
| `reconnect.max_delay` | Max seconds between reconnect attempts |

## User Roles

| Role | Permissions |
|------|-------------|
| `admin` | Full access: register/delete nodes, manage users, all commands |
| `operator` | Execute commands, push files, deploy, manage alerts |
| `viewer` | Read-only: view nodes, metrics, logs |

### Create additional users

```bash
# Via API (as admin)
curl -X POST http://server/api/users \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"username":"ops","email":"ops@company.com","password":"...","role":"operator"}'
```

## API Examples

```bash
# Login
TOKEN=$(curl -s -X POST http://server/api/auth/login \
  -d "username=admin&password=yourpassword" | jq -r .access_token)

# List nodes
curl -H "Authorization: Bearer $TOKEN" http://server/api/nodes

# Execute command
curl -X POST http://server/api/nodes/garage-pi/commands \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"command":"uptime","timeout":10}'

# Get latest metrics
curl -H "Authorization: Bearer $TOKEN" \
  http://server/api/nodes/garage-pi/metrics/latest

# Download file from Pi
curl -H "Authorization: Bearer $TOKEN" \
  "http://server/api/nodes/garage-pi/files/download?remote_path=/etc/hostname" \
  -o hostname.txt

# Push deployment script
curl -X POST http://server/api/nodes/garage-pi/deploy \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"package_name":"update-all","script":"sudo apt update && sudo apt upgrade -y"}'
```

## Proxmox VM Recommendations

### VM Sizing
- **RAM**: 2GB minimum, 4GB recommended
- **vCPU**: 2 cores
- **Disk**: 32GB (metrics grow ~100MB/day per node)
- **Network**: Bridged to your LAN
- **OS**: Ubuntu 22.04 Server or Debian 12

### Proxmox Network Setup (Dream Machine)
1. Assign static IP to VM in DHCP server (bind to MAC)
2. Port forward on Dream Machine:
   - External 443 → VM:443 (dashboard + WSS)
   - External 22 → VM:22 (SSH tunnels from Pis) — or use non-standard port

### Static IP for VM
```bash
# /etc/netplan/00-installer-config.yaml
network:
  ethernets:
    eth0:
      dhcp4: false
      addresses: [192.168.1.50/24]
      gateway4: 192.168.1.1
      nameservers:
        addresses: [1.1.1.1, 8.8.8.8]
```

## Security Hardening

### Server

```bash
# Disable password SSH auth (use keys only)
sed -i 's/#PasswordAuthentication yes/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl reload sshd

# Restrict picommand service ports
# Only nginx should be public-facing; block direct :8000 access
ufw delete allow 8000/tcp

# Automatic security updates
apt-get install -y unattended-upgrades
dpkg-reconfigure --priority=low unattended-upgrades
```

### Database backups
```bash
# Add to crontab (daily backup)
0 2 * * * pg_dump picommand | gzip > /var/backups/picommand-$(date +%Y%m%d).sql.gz
# Keep 30 days
find /var/backups -name 'picommand-*.sql.gz' -mtime +30 -delete
```

### Threat Model

| Threat | Mitigation |
|--------|-----------|
| Unauthorized Pi connecting | Ed25519 key authentication, must be pre-registered |
| MITM on WebSocket | WSS (TLS), certificate pinning optional |
| Stolen Pi | Node can be disabled in dashboard instantly |
| Brute-force login | fail2ban, bcrypt password hashing, JWT expiry |
| Command injection | All commands audited, RBAC controls who can execute |
| Pi executing malicious server commands | Pi runs as limited user, consider AppArmor profile |
| Database compromise | Passwords hashed with bcrypt, tokens hashed with SHA-256 |
| Token theft | Short JWT expiry (60min), HTTPS-only |

## Operations

### Service management

```bash
# Server
sudo systemctl status picommand
sudo journalctl -u picommand -f
sudo systemctl restart picommand

# Agent (on Pi)
sudo systemctl status picommand-agent
sudo journalctl -u picommand-agent -f
```

### Update server

```bash
cd /opt/picommand
git pull
source venv/bin/activate
pip install -r server/requirements.txt
sudo systemctl restart picommand
```

### Update all Pi agents remotely

Via dashboard or API:
```bash
# Deploy update script to all nodes
for NODE in garage-pi office-pi workshop-pi; do
  curl -X POST "http://server/api/nodes/$NODE/deploy" \
    -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d '{
      "package_name": "agent-update",
      "script": "cd /opt/picommand-agent && git pull && sudo systemctl restart picommand-agent"
    }'
done
```

### Metrics cleanup (cron on server)

```bash
# Delete metrics older than retention period
0 3 * * * psql picommand -c "DELETE FROM node_metrics WHERE recorded_at < NOW() - INTERVAL '7 days';"
```

## Scaling Beyond 100 Nodes

For larger deployments:

1. **PostgreSQL tuning**: Increase `shared_buffers`, `work_mem`, add connection pooling (pgBouncer)
2. **Multiple workers**: Increase uvicorn `--workers` count (use Redis for session sharing)
3. **Metrics archiving**: Aggregate old per-minute data to hourly buckets
4. **TimescaleDB**: Drop-in PostgreSQL extension for time-series metrics at scale
5. **Load balancer**: nginx upstream with multiple PiCommand instances

## Troubleshooting

### Node won't connect
```bash
# On Pi - check agent logs
journalctl -u picommand-agent -n 50 --no-pager

# Common issues:
# - Wrong SERVER_URL (must be wss:// not ws:// in production)
# - Node not registered on server
# - Wrong node_id in config
# - Public key mismatch (regenerate keys and re-register)
```

### Dashboard shows node offline but agent is running
```bash
# Check server can reach Pi
curl http://server/api/nodes/garage-pi

# Check WebSocket from server perspective
journalctl -u picommand -n 50 --no-pager | grep garage-pi
```

### SSH tunnel not working
```bash
# On Pi - check autossh
ps aux | grep autossh
journalctl -u picommand-agent | grep tunnel

# On server - check bound ports
ss -tlnp | grep 120
# Should show LISTEN on your tunnel ports
```
