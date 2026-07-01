"""
WebSocket endpoint: /ws/node/{node_id}

This is the persistent connection endpoint that each Pi calls home to.
Protocol is JSON messages over WSS.

Message types (node → server):
  auth_request   - initial authentication with signed challenge
  heartbeat      - keepalive with basic metrics
  metrics        - full telemetry push
  command_result - response to an execute_command request
  file_data      - response to a file_download request
  log_line       - streamed log output
  service_status - systemd service status update
  event          - arbitrary event notification

Message types (server → node):
  auth_challenge  - challenge to sign
  auth_ok         - authentication succeeded
  auth_fail       - authentication failed
  execute_command - run a shell command
  file_download   - send a file to server
  file_upload     - receive a file from server
  ping            - heartbeat check
  restart_tunnel  - reconnect SSH reverse tunnel
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import traceback
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.config import get_settings
from server.core.security import generate_node_challenge, verify_node_signature
from server.db.database import AsyncSessionLocal
from server.db.models import Node, NodeMetric, NodeService, NodeStatus, AuditLog
from server.services.connection_manager import manager, ConnectionState
from server.services.alert_service import check_metric_alerts

settings = get_settings()
router = APIRouter()
logger = logging.getLogger("picommand.node_ws")


async def _get_node_by_id(db: AsyncSession, node_id: str) -> Node | None:
    result = await db.execute(select(Node).where(Node.node_id == node_id))
    return result.scalar_one_or_none()


@router.websocket("/ws/node/{node_id}")
async def node_websocket(websocket: WebSocket, node_id: str):
    """
    Persistent WebSocket connection from a Pi node.
    Flow:
      1. Accept connection
      2. Send challenge
      3. Node signs challenge with private key
      4. Verify signature against stored public key
      5. Mark node online, enter message loop
    """
    await websocket.accept()
    logger.info(f"WS connection attempt: node_id={node_id}")

    # ── Step 1: Look up node ──────────────────────────────────────────────
    async with AsyncSessionLocal() as db:
        node = await _get_node_by_id(db, node_id)

    if not node:
        await websocket.send_text(json.dumps({
            "type": "auth_fail",
            "reason": "Unknown node"
        }))
        await websocket.close(code=4001)
        return

    if node.status == NodeStatus.disabled:
        await websocket.send_text(json.dumps({
            "type": "auth_fail",
            "reason": "Node is disabled"
        }))
        await websocket.close(code=4003)
        return

    # ── Step 2: Challenge-response auth ───────────────────────────────────
    challenge = generate_node_challenge()
    await websocket.send_text(json.dumps({
        "type": "auth_challenge",
        "challenge": challenge,
    }))

    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=15)
        msg = json.loads(raw)
    except (asyncio.TimeoutError, json.JSONDecodeError):
        await websocket.close(code=4002)
        return

    if msg.get("type") != "auth_response":
        await websocket.close(code=4002)
        return

    signature = msg.get("signature", "")
    if not verify_node_signature(node.public_key, challenge, signature):
        logger.warning(f"Auth failed for node {node_id}: bad signature")
        await websocket.send_text(json.dumps({
            "type": "auth_fail",
            "reason": "Invalid signature"
        }))
        await websocket.close(code=4001)
        return

    # ── Step 3: Mark online ───────────────────────────────────────────────
    client_ip = websocket.client.host if websocket.client else "unknown"
    node_meta = msg.get("metadata", {})

    async with AsyncSessionLocal() as db:
        # Only overwrite metadata fields the agent actually sent — a reconnect
        # without metadata must not wipe known hostname/os/model with NULLs.
        values = {
            "status": NodeStatus.online,
            "last_seen": datetime.now(timezone.utc),
            "ip_address": client_ip,
        }
        for db_field, meta_key in (
            ("hostname", "hostname"), ("os_version", "os_version"),
            ("arch", "arch"), ("pi_model", "pi_model"),
        ):
            if node_meta.get(meta_key):
                values[db_field] = node_meta[meta_key]
        if node_meta.get("agent_version"):
            values["agent_version"] = node_meta["agent_version"]
        await db.execute(
            update(Node).where(Node.node_id == node_id).values(**values)
        )
        await db.commit()

        # Audit log
        db.add(AuditLog(
            node_id=node.id,
            action="node_connected",
            details={"ip": client_ip, "metadata": node_meta},
            ip_address=client_ip,
        ))
        await db.commit()

    # Register connection
    state = ConnectionState(
        node_id=node_id,
        node_db_id=str(node.id),
        websocket=websocket,
        ip_address=client_ip,
        metadata=node_meta,
    )
    await manager.connect(state)

    await websocket.send_text(json.dumps({
        "type": "auth_ok",
        "server_time": datetime.now(timezone.utc).isoformat(),
    }))

    # ── Step 4: Message loop ──────────────────────────────────────────────
    try:
        while True:
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=settings.WS_HEARTBEAT_INTERVAL * 2 + 10
                )
            except asyncio.TimeoutError:
                logger.warning(f"Node {node_id} timed out (no heartbeat)")
                break

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")

            if msg_type == "heartbeat":
                state.last_heartbeat = datetime.now(timezone.utc)
                # Update last_seen in DB (batched in background)
                asyncio.create_task(_update_last_seen(node.id))

            elif msg_type == "metrics":
                asyncio.create_task(_store_metrics(node.id, msg))
                # Issue #4: agent never sends heartbeat, so bump last_seen on every metrics push too
                asyncio.create_task(_update_last_seen(node.id))

            elif msg_type == "command_result":
                command_id = msg.get("command_id")
                if command_id:
                    result = {
                        "exit_code": msg.get("exit_code", -1),
                        "stdout": msg.get("stdout", ""),
                        "stderr": msg.get("stderr", ""),
                    }
                    manager.resolve_command(node_id, command_id, result)
                    asyncio.create_task(_update_command_result(command_id, result))

            elif msg_type == "file_data":
                transfer_id = msg.get("transfer_id")
                if transfer_id:
                    if msg.get("error"):
                        manager.resolve_file_transfer(
                            node_id, transfer_id,
                            Exception(msg["error"])
                        )
                    else:
                        raw_data = base64.b64decode(msg.get("data", ""))
                        manager.resolve_file_transfer(node_id, transfer_id, raw_data)

            elif msg_type == "file_upload_result":
                transfer_id = msg.get("transfer_id")
                if transfer_id:
                    if msg.get("success"):
                        manager.resolve_file_transfer(node_id, transfer_id, True)
                    else:
                        manager.resolve_file_transfer(
                            node_id, transfer_id,
                            Exception(msg.get("error", "Upload failed"))
                        )

            elif msg_type == "service_status":
                asyncio.create_task(_update_services(node.id, msg.get("services", [])))

            elif msg_type == "pong":
                state.last_heartbeat = datetime.now(timezone.utc)

    except WebSocketDisconnect:
        logger.info(f"Node {node_id} disconnected cleanly")
    except Exception as e:
        logger.error(f"Node {node_id} error: {e}\n{traceback.format_exc()}")
    finally:
        # Only mark the node offline if THIS socket was still the live
        # connection. If the node already reconnected, disconnect() returns
        # False and we must not clobber the new connection's online status.
        was_current = await manager.disconnect(node_id, expected_state=state)
        if was_current:
            async with AsyncSessionLocal() as db:
                await db.execute(
                    update(Node)
                    .where(Node.node_id == node_id)
                    .values(status=NodeStatus.offline)
                )
                await db.commit()


# ── Background DB tasks ───────────────────────────────────────────────────────

async def _update_last_seen(node_db_id: UUID):
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Node)
            .where(Node.id == node_db_id)
            .values(last_seen=datetime.now(timezone.utc))
        )
        await db.commit()


async def _store_metrics(node_db_id: UUID, msg: dict):
    async with AsyncSessionLocal() as db:
        metric = NodeMetric(
            node_id=node_db_id,
            cpu_percent=msg.get("cpu_percent"),
            ram_percent=msg.get("ram_percent"),
            ram_used_mb=msg.get("ram_used_mb"),
            ram_total_mb=msg.get("ram_total_mb"),
            disk_percent=msg.get("disk_percent"),
            disk_used_gb=msg.get("disk_used_gb"),
            disk_total_gb=msg.get("disk_total_gb"),
            cpu_temp_c=msg.get("cpu_temp_c"),
            load_avg_1=msg.get("load_avg_1"),
            load_avg_5=msg.get("load_avg_5"),
            load_avg_15=msg.get("load_avg_15"),
            uptime_seconds=msg.get("uptime_seconds"),
            net_bytes_sent=msg.get("net_bytes_sent"),
            net_bytes_recv=msg.get("net_bytes_recv"),
        )
        db.add(metric)
        await db.commit()

    # Check alert rules
    await check_metric_alerts(node_db_id, msg)


async def _update_command_result(command_id: str, result: dict):
    from server.db.models import Command, CommandStatus
    try:
        cmd_uuid = UUID(command_id)
    except (ValueError, AttributeError, TypeError):
        logger.warning(f"Ignoring command_result with malformed command_id: {command_id!r}")
        return
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Command)
            .where(Command.id == cmd_uuid)
            .values(
                status=CommandStatus.completed if result["exit_code"] == 0 else CommandStatus.failed,
                exit_code=result["exit_code"],
                stdout=result["stdout"],
                stderr=result["stderr"],
                completed_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()


async def _update_services(node_db_id: UUID, services: list[dict]):
    async with AsyncSessionLocal() as db:
        for svc in services:
            from sqlalchemy.dialects.postgresql import insert
            stmt = insert(NodeService).values(
                node_id=node_db_id,
                service_name=svc["name"],
                is_active=svc.get("active"),
                is_enabled=svc.get("enabled"),
                last_checked=datetime.now(timezone.utc),
            ).on_conflict_do_update(
                index_elements=["node_id", "service_name"],
                set_={
                    "is_active": svc.get("active"),
                    "is_enabled": svc.get("enabled"),
                    "last_checked": datetime.now(timezone.utc),
                }
            )
            await db.execute(stmt)
        await db.commit()
