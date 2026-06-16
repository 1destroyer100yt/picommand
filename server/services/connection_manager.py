"""
WebSocket Connection Manager

Manages persistent encrypted WebSocket connections from Pi nodes.
Each connected node gets a ConnectionState object that tracks:
  - the websocket
  - pending command futures
  - last heartbeat
  - buffered metrics
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
import traceback

from fastapi import WebSocket

logger = logging.getLogger("picommand.ws")


@dataclass
class ConnectionState:
    node_id: str
    node_db_id: str           # UUID from database
    websocket: WebSocket
    connected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_heartbeat: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    pending_commands: dict[str, asyncio.Future] = field(default_factory=dict)
    pending_file_transfers: dict[str, asyncio.Future] = field(default_factory=dict)
    ip_address: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def uptime_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.connected_at).total_seconds()


class ConnectionManager:
    """
    Thread-safe async connection registry.
    One instance shared across the FastAPI app (singleton via app.state).
    """

    def __init__(self):
        # node_id -> ConnectionState
        self._connections: dict[str, ConnectionState] = {}
        self._lock = asyncio.Lock()
        # Subscribers for SSE / dashboard push
        self._event_subscribers: list[asyncio.Queue] = []
        # Issue #16/#17: when the server is mid-update, suppress agent updates
        self._update_in_progress: bool = False

    # ── Update coordination ───────────────────────────────────────────────────

    def set_update_in_progress(self, value: bool) -> None:
        self._update_in_progress = value

    @property
    def update_in_progress(self) -> bool:
        return self._update_in_progress

    # ── Connection Lifecycle ──────────────────────────────────────────────────

    async def connect(self, state: ConnectionState):
        async with self._lock:
            self._connections[state.node_id] = state
        logger.info(f"Node connected: {state.node_id} from {state.ip_address}")
        await self._broadcast_event({
            "event": "node_connected",
            "node_id": state.node_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    async def disconnect(self, node_id: str):
        async with self._lock:
            state = self._connections.pop(node_id, None)
        if state:
            # Fail all pending command futures
            for fut in state.pending_commands.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("Node disconnected"))
            logger.info(f"Node disconnected: {node_id}")
            await self._broadcast_event({
                "event": "node_disconnected",
                "node_id": node_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

    def is_connected(self, node_id: str) -> bool:
        return node_id in self._connections

    def get_connection(self, node_id: str) -> Optional[ConnectionState]:
        return self._connections.get(node_id)

    def get_all_connections(self) -> dict[str, ConnectionState]:
        return dict(self._connections)

    def connected_count(self) -> int:
        return len(self._connections)

    # ── Sending Messages ──────────────────────────────────────────────────────

    async def send(self, node_id: str, message: dict) -> bool:
        """Send a JSON message to a specific node. Returns False if not connected."""
        state = self._connections.get(node_id)
        if not state:
            return False
        try:
            await state.websocket.send_text(json.dumps(message))
            return True
        except Exception as e:
            logger.warning(f"Send failed for {node_id}: {e}")
            await self.disconnect(node_id)
            return False

    async def broadcast(self, message: dict, exclude: set[str] | None = None):
        """Send a message to all connected nodes."""
        exclude = exclude or set()
        tasks = [
            self.send(nid, message)
            for nid in list(self._connections.keys())
            if nid not in exclude
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    # ── Command Execution ─────────────────────────────────────────────────────

    async def execute_command(
        self,
        node_id: str,
        command: str,
        command_db_id: str,
        timeout: int = 30,
    ) -> dict:
        """
        Send a command to a node and await the response.

        Returns dict with keys: exit_code, stdout, stderr
        Raises: ConnectionError, asyncio.TimeoutError
        """
        state = self._connections.get(node_id)
        if not state:
            raise ConnectionError(f"Node {node_id} is not connected")

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        state.pending_commands[command_db_id] = fut

        try:
            await self.send(node_id, {
                "type": "execute_command",
                "command_id": command_db_id,
                "command": command,
                "timeout": timeout,
            })

            result = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout + 5)
            return result
        except asyncio.TimeoutError:
            logger.warning(f"Command timed out on {node_id}: {command!r}")
            raise
        finally:
            state.pending_commands.pop(command_db_id, None)

    def resolve_command(self, node_id: str, command_id: str, result: dict):
        """Called when a node sends back a command result."""
        state = self._connections.get(node_id)
        if state and command_id in state.pending_commands:
            fut = state.pending_commands[command_id]
            if not fut.done():
                fut.set_result(result)

    # ── File Transfer ─────────────────────────────────────────────────────────

    async def request_file_download(self, node_id: str, remote_path: str, transfer_id: str) -> bytes:
        """Ask node to send a file. Returns raw bytes."""
        state = self._connections.get(node_id)
        if not state:
            raise ConnectionError(f"Node {node_id} is not connected")

        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        state.pending_file_transfers[transfer_id] = fut

        try:
            await self.send(node_id, {
                "type": "file_download",
                "transfer_id": transfer_id,
                "remote_path": remote_path,
            })
            return await asyncio.wait_for(fut, timeout=300)
        finally:
            state.pending_file_transfers.pop(transfer_id, None)

    def resolve_file_transfer(self, node_id: str, transfer_id: str, data: bytes | Exception):
        state = self._connections.get(node_id)
        if state and transfer_id in state.pending_file_transfers:
            fut = state.pending_file_transfers[transfer_id]
            if not fut.done():
                if isinstance(data, Exception):
                    fut.set_exception(data)
                else:
                    fut.set_result(data)

    # ── Server-Sent Events (dashboard real-time push) ────────────────────────

    def subscribe_events(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._event_subscribers.append(q)
        return q

    def unsubscribe_events(self, q: asyncio.Queue):
        try:
            self._event_subscribers.remove(q)
        except ValueError:
            pass

    async def _broadcast_event(self, event: dict):
        dead = []
        for q in self._event_subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self.unsubscribe_events(q)

    async def push_file_to_node(
        self, node_id: str, dest_path: str, content: bytes, transfer_id: str
    ) -> bool:
        """Push file data to a node and await confirmation."""
        import base64 as _b64
        state = self._connections.get(node_id)
        if not state:
            raise ConnectionError(f"Node {node_id} is not connected")

        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        state.pending_file_transfers[transfer_id] = fut

        try:
            await self.send(node_id, {
                "type": "file_upload",
                "transfer_id": transfer_id,
                "dest_path": dest_path,
                "data": _b64.b64encode(content).decode(),
            })
            return await asyncio.wait_for(fut, timeout=120)
        finally:
            state.pending_file_transfers.pop(transfer_id, None)


# Singleton — imported everywhere
manager = ConnectionManager()
