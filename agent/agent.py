#!/usr/bin/env python3
"""
PiCommand Agent — runs on each Raspberry Pi

Responsibilities:
  1. Connect to the home server via WebSocket (WSS)
  2. Authenticate using Ed25519 key pair
  3. Send periodic heartbeats and metrics
  4. Execute commands sent by the server
  5. Handle file transfers
  6. Reconnect automatically on disconnect
  7. Maintain reverse SSH tunnel via autossh

Config file: /etc/picommand/agent.conf (INI format)
Keys:       /etc/picommand/keys/node_key (Ed25519 private key)
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import configparser
import json
import logging
import os
import platform
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import psutil
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("/var/log/picommand-agent.log", delay=True),
    ]
)
logger = logging.getLogger("picommand.agent")


# ── Config ────────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "server": {
        "url": "wss://your-server.example.com",
        "verify_ssl": "true",
    },
    "node": {
        "node_id": "",
        "display_name": socket.gethostname(),
    },
    "keys": {
        "private_key_path": "/etc/picommand/keys/node_key",
    },
    "tunnel": {
        "enabled": "true",
        "server_host": "your-server.example.com",
        "server_ssh_port": "22",
        "server_user": "picommand-tunnel",
        "tunnel_port": "0",          # assigned by server
        "local_port": "22",
        "autossh_flags": "-M 0 -o ServerAliveInterval=30 -o ServerAliveCountMax=3",
    },
    "metrics": {
        "interval_seconds": "30",
        "services_to_monitor": "ssh,home-assistant,mosquitto",
    },
    "reconnect": {
        "initial_delay": "5",
        "max_delay": "300",
        "backoff_factor": "2",
    },
}


def load_config(path: str = "/etc/picommand/agent.conf") -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    # Set defaults
    for section, values in DEFAULT_CONFIG.items():
        cfg[section] = values
    cfg.read(path)
    return cfg


# ── Crypto ────────────────────────────────────────────────────────────────────

def load_private_key(path: str) -> ed25519.Ed25519PrivateKey:
    with open(path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def sign_challenge(private_key: ed25519.Ed25519PrivateKey, challenge: str) -> str:
    sig = private_key.sign(challenge.encode())
    return binascii.hexlify(sig).decode()


def get_public_key_pem(private_key: ed25519.Ed25519PrivateKey) -> str:
    return private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()


def generate_keypair(private_key_path: str):
    """Generate a new Ed25519 keypair if it doesn't exist."""
    priv_path = Path(private_key_path)
    pub_path = priv_path.with_suffix(".pub")

    if priv_path.exists():
        logger.info(f"Key already exists: {priv_path}")
        return

    priv_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    private_key = ed25519.Ed25519PrivateKey.generate()

    priv_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()
    )
    pub_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo
    )

    priv_path.write_bytes(priv_pem)
    priv_path.chmod(0o600)
    pub_path.write_bytes(pub_pem)
    pub_path.chmod(0o644)

    logger.info(f"Generated new keypair at {priv_path}")
    print(f"\n{'='*60}")
    print("PUBLIC KEY (register this with your server):")
    print('='*60)
    print(pub_pem.decode())
    print('='*60)


# ── System Metrics ────────────────────────────────────────────────────────────

def collect_metrics() -> dict:
    metrics = {}
    try:
        cpu = psutil.cpu_percent(interval=0.1)
        metrics["cpu_percent"] = cpu

        mem = psutil.virtual_memory()
        metrics["ram_percent"] = mem.percent
        metrics["ram_used_mb"] = mem.used // (1024 * 1024)
        metrics["ram_total_mb"] = mem.total // (1024 * 1024)

        disk = psutil.disk_usage("/")
        metrics["disk_percent"] = disk.percent
        metrics["disk_used_gb"] = disk.used / (1024 ** 3)
        metrics["disk_total_gb"] = disk.total / (1024 ** 3)

        load = psutil.getloadavg()
        metrics["load_avg_1"] = load[0]
        metrics["load_avg_5"] = load[1]
        metrics["load_avg_15"] = load[2]

        metrics["uptime_seconds"] = int(time.time() - psutil.boot_time())

        net = psutil.net_io_counters()
        metrics["net_bytes_sent"] = net.bytes_sent
        metrics["net_bytes_recv"] = net.bytes_recv

        # CPU temperature (Raspberry Pi)
        temp_path = Path("/sys/class/thermal/thermal_zone0/temp")
        if temp_path.exists():
            metrics["cpu_temp_c"] = int(temp_path.read_text()) / 1000.0
        elif hasattr(psutil, "sensors_temperatures"):
            temps = psutil.sensors_temperatures()
            if temps:
                for name in ("cpu_thermal", "coretemp", "k10temp"):
                    if name in temps and temps[name]:
                        metrics["cpu_temp_c"] = temps[name][0].current
                        break

    except Exception as e:
        logger.warning(f"Metrics collection error: {e}")

    return metrics


def collect_node_metadata() -> dict:
    meta = {
        "hostname": socket.gethostname(),
        "arch": platform.machine(),
        "os_version": f"{platform.system()} {platform.release()}",
    }
    # Detect Pi model
    model_path = Path("/proc/device-tree/model")
    if model_path.exists():
        try:
            meta["pi_model"] = model_path.read_text().rstrip("\x00")
        except Exception:
            pass
    return meta


def get_service_statuses(service_names: list[str]) -> list[dict]:
    services = []
    for name in service_names:
        name = name.strip()
        if not name:
            continue
        try:
            active = subprocess.run(
                ["systemctl", "is-active", name],
                capture_output=True, text=True, timeout=5
            )
            enabled = subprocess.run(
                ["systemctl", "is-enabled", name],
                capture_output=True, text=True, timeout=5
            )
            services.append({
                "name": name,
                "active": active.stdout.strip() == "active",
                "enabled": enabled.stdout.strip() == "enabled",
            })
        except Exception:
            services.append({"name": name, "active": False, "enabled": False})
    return services


# ── Command Execution ─────────────────────────────────────────────────────────

async def run_command(command: str, timeout: int = 30) -> dict:
    """Execute a shell command and return output."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return {
                "exit_code": -1,
                "stdout": "",
                "stderr": f"Command timed out after {timeout}s",
            }

        return {
            "exit_code": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace")[:65536],
            "stderr": stderr.decode("utf-8", errors="replace")[:16384],
        }
    except Exception as e:
        return {"exit_code": -1, "stdout": "", "stderr": str(e)}


# ── Tunnel Management ─────────────────────────────────────────────────────────

class TunnelManager:
    def __init__(self, cfg: configparser.ConfigParser):
        self.cfg = cfg
        self._proc: subprocess.Popen | None = None

    def start(self, assigned_port: int | None = None):
        if not self.cfg.getboolean("tunnel", "enabled", fallback=True):
            return

        port = assigned_port or self.cfg.getint("tunnel", "tunnel_port", fallback=0)
        if port == 0:
            logger.warning("No tunnel port assigned, skipping tunnel")
            return

        self.stop()

        local_port = self.cfg.get("tunnel", "local_port")
        server_host = self.cfg.get("tunnel", "server_host")
        server_ssh_port = self.cfg.get("tunnel", "server_ssh_port")
        server_user = self.cfg.get("tunnel", "server_user")
        key_path = self.cfg.get("keys", "private_key_path")
        extra_flags = self.cfg.get("tunnel", "autossh_flags")

        cmd = (
            f"autossh -N -T "
            f"{extra_flags} "
            f"-o StrictHostKeyChecking=no "
            f"-i {key_path} "
            f"-p {server_ssh_port} "
            f"-R {port}:localhost:{local_port} "
            f"{server_user}@{server_host}"
        )
        logger.info(f"Starting SSH tunnel: remote port {port} → local :{local_port}")
        try:
            self._proc = subprocess.Popen(
                shlex.split(cmd),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logger.warning("autossh not found, trying plain ssh")
            cmd = cmd.replace("autossh", "ssh")
            self._proc = subprocess.Popen(
                shlex.split(cmd),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    def stop(self):
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
            self._proc = None


# ── Main Agent Loop ───────────────────────────────────────────────────────────

class PiCommandAgent:
    def __init__(self, config_path: str = "/etc/picommand/agent.conf"):
        self.cfg = load_config(config_path)
        self.node_id = self.cfg.get("node", "node_id")
        self.server_url = self.cfg.get("server", "url").rstrip("/")
        self.key_path = self.cfg.get("keys", "private_key_path")
        self.metrics_interval = self.cfg.getint("metrics", "interval_seconds", fallback=30)
        self.services_to_monitor = self.cfg.get("metrics", "services_to_monitor").split(",")

        self.reconnect_delay = self.cfg.getint("reconnect", "initial_delay", fallback=5)
        self.max_reconnect_delay = self.cfg.getint("reconnect", "max_delay", fallback=300)
        self.backoff_factor = self.cfg.getfloat("reconnect", "backoff_factor", fallback=2.0)
        self._current_delay = self.reconnect_delay

        self.private_key = load_private_key(self.key_path)
        self.metadata = collect_node_metadata()
        self.tunnel = TunnelManager(self.cfg)
        self._running = True
        self._ws = None

    async def run(self):
        """Main loop with exponential backoff reconnection."""
        logger.info(f"PiCommand Agent starting — node_id={self.node_id}")
        while self._running:
            try:
                await self._connect_and_serve()
                self._current_delay = self.reconnect_delay  # reset on clean disconnect
            except (OSError, websockets.exceptions.WebSocketException) as e:
                logger.warning(f"Connection error: {e}")
            except Exception as e:
                logger.error(f"Unexpected error: {e}\n{traceback.format_exc()}")

            if self._running:
                logger.info(f"Reconnecting in {self._current_delay}s...")
                await asyncio.sleep(self._current_delay)
                self._current_delay = min(
                    self._current_delay * self.backoff_factor,
                    self.max_reconnect_delay
                )

    async def _connect_and_serve(self):
        ws_url = f"{self.server_url}/ws/node/{self.node_id}"
        verify_ssl = self.cfg.getboolean("server", "verify_ssl", fallback=True)

        ssl_context = None
        if ws_url.startswith("wss://") and not verify_ssl:
            import ssl
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        async with websockets.connect(
            ws_url,
            ssl=ssl_context,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=10,
        ) as ws:
            self._ws = ws
            logger.info(f"Connected to {self.server_url}")

            # Auth challenge-response
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            msg = json.loads(raw)
            if msg.get("type") != "auth_challenge":
                raise ValueError(f"Expected auth_challenge, got: {msg.get('type')}")

            challenge = msg["challenge"]
            signature = sign_challenge(self.private_key, challenge)
            await ws.send(json.dumps({
                "type": "auth_response",
                "signature": signature,
                "metadata": self.metadata,
            }))

            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            msg = json.loads(raw)
            if msg.get("type") == "auth_fail":
                raise PermissionError(f"Auth failed: {msg.get('reason')}")
            if msg.get("type") != "auth_ok":
                raise ValueError(f"Unexpected auth response: {msg.get('type')}")

            logger.info("Authentication successful")

            # Start background tasks
            metrics_task = asyncio.create_task(self._metrics_loop(ws))
            try:
                await self._message_loop(ws)
            finally:
                metrics_task.cancel()
                try:
                    await metrics_task
                except asyncio.CancelledError:
                    pass
            self._ws = None

    async def _message_loop(self, ws):
        """Receive and handle server messages."""
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")

            if msg_type == "execute_command":
                asyncio.create_task(self._handle_command(ws, msg))

            elif msg_type == "file_download":
                asyncio.create_task(self._handle_file_download(ws, msg))

            elif msg_type == "file_upload":
                asyncio.create_task(self._handle_file_upload(ws, msg))

            elif msg_type == "ping":
                await ws.send(json.dumps({"type": "pong"}))

            elif msg_type == "restart_tunnel":
                port = msg.get("port")
                logger.info(f"Restarting tunnel on port {port}")
                self.tunnel.start(assigned_port=port)

    async def _handle_command(self, ws, msg: dict):
        command_id = msg["command_id"]
        command = msg["command"]
        timeout = msg.get("timeout", 30)
        logger.info(f"Executing command [{command_id[:8]}]: {command!r}")

        result = await run_command(command, timeout)
        await ws.send(json.dumps({
            "type": "command_result",
            "command_id": command_id,
            **result,
        }))

    async def _handle_file_download(self, ws, msg: dict):
        """Server requests a file from us."""
        transfer_id = msg["transfer_id"]
        remote_path = msg["remote_path"]
        logger.info(f"File download requested: {remote_path}")
        try:
            with open(remote_path, "rb") as f:
                data = f.read()
            await ws.send(json.dumps({
                "type": "file_data",
                "transfer_id": transfer_id,
                "data": base64.b64encode(data).decode(),
            }))
        except Exception as e:
            await ws.send(json.dumps({
                "type": "file_data",
                "transfer_id": transfer_id,
                "error": str(e),
            }))

    async def _handle_file_upload(self, ws, msg: dict):
        """Server is pushing a file to us."""
        transfer_id = msg["transfer_id"]
        dest_path = msg["dest_path"]
        data = base64.b64decode(msg.get("data", ""))
        logger.info(f"File upload: {len(data)} bytes → {dest_path}")
        try:
            Path(dest_path).parent.mkdir(parents=True, exist_ok=True)
            with open(dest_path, "wb") as f:
                f.write(data)
            await ws.send(json.dumps({
                "type": "file_upload_result",
                "transfer_id": transfer_id,
                "success": True,
            }))
        except Exception as e:
            await ws.send(json.dumps({
                "type": "file_upload_result",
                "transfer_id": transfer_id,
                "success": False,
                "error": str(e),
            }))

    async def _metrics_loop(self, ws):
        """Send metrics and heartbeats periodically."""
        services_interval = max(self.metrics_interval * 2, 60)
        services_counter = 0

        while True:
            await asyncio.sleep(self.metrics_interval)
            try:
                metrics = collect_metrics()
                metrics["type"] = "metrics"
                await ws.send(json.dumps(metrics))

                services_counter += self.metrics_interval
                if services_counter >= services_interval:
                    services_counter = 0
                    statuses = get_service_statuses(self.services_to_monitor)
                    if statuses:
                        await ws.send(json.dumps({
                            "type": "service_status",
                            "services": statuses,
                        }))
            except websockets.exceptions.ConnectionClosed:
                break
            except Exception as e:
                logger.warning(f"Metrics send error: {e}")

    def shutdown(self):
        self._running = False
        self.tunnel.stop()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="PiCommand Agent")
    parser.add_argument("--config", default="/etc/picommand/agent.conf")
    parser.add_argument("--generate-keys", action="store_true",
                        help="Generate Ed25519 keypair and exit")
    parser.add_argument("--show-pubkey", action="store_true",
                        help="Print public key and exit")
    args = parser.parse_args()

    cfg = load_config(args.config)
    key_path = cfg.get("keys", "private_key_path")

    if args.generate_keys:
        generate_keypair(key_path)
        return

    if args.show_pubkey:
        priv = load_private_key(key_path)
        print(get_public_key_pem(priv))
        return

    agent = PiCommandAgent(args.config)

    def _shutdown(sig, frame):
        logger.info("Shutdown signal received")
        agent.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    asyncio.run(agent.run())


if __name__ == "__main__":
    main()
