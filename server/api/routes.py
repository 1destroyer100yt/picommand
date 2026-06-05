"""
REST API: nodes, commands, metrics, file transfers, deployments, users, alerts
"""
from __future__ import annotations

import asyncio
import base64
import io
import secrets
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import UUID

from fastapi import (
    APIRouter, Depends, HTTPException, UploadFile, File,
    BackgroundTasks, Query, Request, Response
)
from fastapi.responses import StreamingResponse
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy import select, desc, func, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.dependencies import (
    get_current_user, require_admin, require_operator, require_viewer
)
from server.core.security import (
    verify_password, hash_password, create_access_token,
    create_refresh_token, generate_api_token, hash_api_token
)
from server.db.database import get_db
from server.db.models import (
    User, UserRole, Node, NodeMetric, NodeStatus, Command, CommandStatus,
    AuditLog, FileTransfer, Deployment, Alert, AlertSeverity, DeployStatus,
    ApiToken
)
from server.services.connection_manager import manager

router = APIRouter()


# ════════════════════════════════════════════════════════════════════════════════
# AUTH
# ════════════════════════════════════════════════════════════════════════════════

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


@router.post("/api/auth/login", response_model=TokenResponse, tags=["auth"])
async def login(form: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == form.username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(form.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")

    await db.execute(
        update(User).where(User.id == user.id).values(last_login=datetime.now(timezone.utc))
    )
    await db.commit()

    token = create_access_token(str(user.id), user.role.value)
    from server.core.config import get_settings
    s = get_settings()
    return TokenResponse(access_token=token, expires_in=s.ACCESS_TOKEN_EXPIRE_MINUTES * 60)


@router.get("/api/auth/me", tags=["auth"])
async def me(user: User = Depends(get_current_user)):
    return {
        "id": str(user.id),
        "username": user.username,
        "email": user.email,
        "role": user.role.value,
    }


# ════════════════════════════════════════════════════════════════════════════════
# NODES
# ════════════════════════════════════════════════════════════════════════════════

class NodeCreate(BaseModel):
    node_id: str = Field(..., min_length=3, max_length=64, pattern=r'^[a-z0-9\-]+$')
    display_name: str
    description: Optional[str] = None
    public_key: str
    location: Optional[str] = None
    tags: list[str] = []


class NodeUpdate(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    location: Optional[str] = None
    tags: Optional[list[str]] = None
    status: Optional[NodeStatus] = None


def _node_to_dict(node: Node, online: bool = False) -> dict:
    return {
        "id": str(node.id),
        "node_id": node.node_id,
        "display_name": node.display_name,
        "description": node.description,
        "status": node.status.value if not online else "online",
        "is_online": manager.is_connected(node.node_id),
        "tags": node.tags or [],
        "location": node.location,
        "last_seen": node.last_seen.isoformat() if node.last_seen else None,
        "ssh_tunnel_port": node.ssh_tunnel_port,
        "ip_address": str(node.ip_address) if node.ip_address else None,
        "hostname": node.hostname,
        "os_version": node.os_version,
        "arch": node.arch,
        "pi_model": node.pi_model,
        "created_at": node.created_at.isoformat(),
    }


@router.get("/api/nodes", tags=["nodes"])
async def list_nodes(
    status: Optional[str] = None,
    tag: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_viewer),
):
    q = select(Node)
    if status:
        q = q.where(Node.status == NodeStatus(status))
    if tag:
        q = q.where(Node.tags.contains([tag]))
    q = q.order_by(Node.display_name)
    result = await db.execute(q)
    nodes = result.scalars().all()
    return [_node_to_dict(n) for n in nodes]


@router.post("/api/nodes", status_code=201, tags=["nodes"])
async def register_node(
    body: NodeCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Register a new Pi node. Admin only."""
    existing = await db.execute(select(Node).where(Node.node_id == body.node_id))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="node_id already exists")

    # Assign SSH tunnel port
    used_ports = await db.execute(select(Node.ssh_tunnel_port).where(Node.ssh_tunnel_port.isnot(None)))
    used = {p for (p,) in used_ports}
    from server.core.config import get_settings
    s = get_settings()
    port = None
    for p in range(s.TUNNEL_PORT_RANGE_START, s.TUNNEL_PORT_RANGE_END):
        if p not in used:
            port = p
            break
    if not port:
        raise HTTPException(status_code=503, detail="No tunnel ports available")

    node = Node(
        node_id=body.node_id,
        display_name=body.display_name,
        description=body.description,
        public_key=body.public_key,
        location=body.location,
        tags=body.tags,
        ssh_tunnel_port=port,
        status=NodeStatus.pending,
        approved_at=datetime.now(timezone.utc),
        approved_by=user.id,
    )
    db.add(node)
    await db.commit()
    await db.refresh(node)

    db.add(AuditLog(user_id=user.id, node_id=node.id, action="node_registered",
                    details={"node_id": body.node_id}))
    await db.commit()
    return _node_to_dict(node)


@router.get("/api/nodes/{node_id}", tags=["nodes"])
async def get_node(
    node_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_viewer),
):
    result = await db.execute(select(Node).where(Node.node_id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(404, "Node not found")
    return _node_to_dict(node)


@router.patch("/api/nodes/{node_id}", tags=["nodes"])
async def update_node(
    node_id: str,
    body: NodeUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator),
):
    result = await db.execute(select(Node).where(Node.node_id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(404, "Node not found")
    for field, val in body.model_dump(exclude_none=True).items():
        setattr(node, field, val)
    await db.commit()
    return _node_to_dict(node)


@router.delete("/api/nodes/{node_id}", status_code=204, tags=["nodes"])
async def delete_node(
    node_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_admin),
):
    result = await db.execute(select(Node).where(Node.node_id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(404, "Node not found")
    await db.delete(node)
    await db.commit()


# ════════════════════════════════════════════════════════════════════════════════
# COMMANDS
# ════════════════════════════════════════════════════════════════════════════════

class CommandRequest(BaseModel):
    command: str
    timeout: int = Field(default=30, ge=1, le=300)


@router.post("/api/nodes/{node_id}/commands", tags=["commands"])
async def execute_command(
    node_id: str,
    body: CommandRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator),
):
    result = await db.execute(select(Node).where(Node.node_id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(404, "Node not found")
    if not manager.is_connected(node_id):
        raise HTTPException(503, "Node is offline")

    cmd = Command(
        node_id=node.id,
        issued_by=user.id,
        command=body.command,
        status=CommandStatus.running,
        started_at=datetime.now(timezone.utc),
        timeout_seconds=body.timeout,
    )
    db.add(cmd)
    await db.commit()
    await db.refresh(cmd)

    db.add(AuditLog(user_id=user.id, node_id=node.id, action="command_executed",
                    details={"command": body.command}))
    await db.commit()

    try:
        result_data = await manager.execute_command(node_id, body.command, str(cmd.id), body.timeout)
    except asyncio.TimeoutError:
        await db.execute(
            update(Command).where(Command.id == cmd.id).values(status=CommandStatus.timeout)
        )
        await db.commit()
        raise HTTPException(408, "Command timed out")
    except ConnectionError as e:
        raise HTTPException(503, str(e))

    return {
        "command_id": str(cmd.id),
        "exit_code": result_data["exit_code"],
        "stdout": result_data["stdout"],
        "stderr": result_data["stderr"],
        "status": "completed" if result_data["exit_code"] == 0 else "failed",
    }


@router.get("/api/nodes/{node_id}/commands", tags=["commands"])
async def list_commands(
    node_id: str,
    limit: int = Query(50, le=200),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_viewer),
):
    result = await db.execute(select(Node).where(Node.node_id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(404, "Node not found")

    cmds = await db.execute(
        select(Command)
        .where(Command.node_id == node.id)
        .order_by(desc(Command.created_at))
        .limit(limit)
    )
    return [
        {
            "id": str(c.id),
            "command": c.command,
            "status": c.status.value,
            "exit_code": c.exit_code,
            "stdout": c.stdout,
            "stderr": c.stderr,
            "created_at": c.created_at.isoformat(),
            "completed_at": c.completed_at.isoformat() if c.completed_at else None,
        }
        for c in cmds.scalars().all()
    ]


# ════════════════════════════════════════════════════════════════════════════════
# METRICS
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/api/nodes/{node_id}/metrics", tags=["metrics"])
async def get_metrics(
    node_id: str,
    hours: int = Query(1, ge=1, le=168),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_viewer),
):
    result = await db.execute(select(Node).where(Node.node_id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(404, "Node not found")

    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    metrics = await db.execute(
        select(NodeMetric)
        .where(NodeMetric.node_id == node.id, NodeMetric.recorded_at >= since)
        .order_by(NodeMetric.recorded_at)
        .limit(1440)
    )
    rows = metrics.scalars().all()
    return [
        {
            "t": m.recorded_at.isoformat(),
            "cpu": m.cpu_percent,
            "ram": m.ram_percent,
            "disk": m.disk_percent,
            "temp": m.cpu_temp_c,
            "load1": m.load_avg_1,
            "uptime": m.uptime_seconds,
        }
        for m in rows
    ]


@router.get("/api/nodes/{node_id}/metrics/latest", tags=["metrics"])
async def get_latest_metrics(
    node_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_viewer),
):
    result = await db.execute(select(Node).where(Node.node_id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(404, "Node not found")

    m = await db.execute(
        select(NodeMetric)
        .where(NodeMetric.node_id == node.id)
        .order_by(desc(NodeMetric.recorded_at))
        .limit(1)
    )
    metric = m.scalar_one_or_none()
    if not metric:
        return {}
    return {
        "recorded_at": metric.recorded_at.isoformat(),
        "cpu_percent": metric.cpu_percent,
        "ram_percent": metric.ram_percent,
        "ram_used_mb": metric.ram_used_mb,
        "ram_total_mb": metric.ram_total_mb,
        "disk_percent": metric.disk_percent,
        "disk_used_gb": metric.disk_used_gb,
        "disk_total_gb": metric.disk_total_gb,
        "cpu_temp_c": metric.cpu_temp_c,
        "load_avg_1": metric.load_avg_1,
        "load_avg_5": metric.load_avg_5,
        "load_avg_15": metric.load_avg_15,
        "uptime_seconds": metric.uptime_seconds,
    }


# ════════════════════════════════════════════════════════════════════════════════
# ALERTS
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/api/alerts", tags=["alerts"])
async def list_alerts(
    unresolved_only: bool = True,
    limit: int = Query(50, le=500),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_viewer),
):
    q = select(Alert).order_by(desc(Alert.fired_at)).limit(limit)
    if unresolved_only:
        q = q.where(Alert.resolved_at.is_(None))
    result = await db.execute(q)
    alerts = result.scalars().all()
    return [
        {
            "id": str(a.id),
            "node_id": str(a.node_id),
            "severity": a.severity.value,
            "message": a.message,
            "metric": a.metric,
            "metric_value": a.metric_value,
            "fired_at": a.fired_at.isoformat(),
            "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
        }
        for a in alerts
    ]


@router.post("/api/alerts/{alert_id}/acknowledge", tags=["alerts"])
async def acknowledge_alert(
    alert_id: UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator),
):
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    alert = result.scalar_one_or_none()
    if not alert:
        raise HTTPException(404, "Alert not found")
    alert.acknowledged_by = user.id
    alert.acknowledged_at = datetime.now(timezone.utc)
    await db.commit()
    return {"status": "acknowledged"}


# ════════════════════════════════════════════════════════════════════════════════
# DEPLOYMENTS
# ════════════════════════════════════════════════════════════════════════════════

class DeploymentCreate(BaseModel):
    package_name: str
    script: str


@router.post("/api/nodes/{node_id}/deploy", tags=["deployments"])
async def deploy(
    node_id: str,
    body: DeploymentCreate,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator),
):
    result = await db.execute(select(Node).where(Node.node_id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(404, "Node not found")
    if not manager.is_connected(node_id):
        raise HTTPException(503, "Node is offline")

    dep = Deployment(
        node_id=node.id,
        initiated_by=user.id,
        package_name=body.package_name,
        script=body.script,
        status=DeployStatus.in_progress,
    )
    db.add(dep)
    await db.commit()
    await db.refresh(dep)

    background_tasks.add_task(_run_deployment, node_id, str(node.id), str(dep.id), body.script)
    return {"deployment_id": str(dep.id), "status": "in_progress"}


async def _run_deployment(node_id: str, node_db_id: str, dep_id: str, script: str):
    from server.db.database import AsyncSessionLocal
    try:
        result = await manager.execute_command(node_id, script, dep_id, timeout=300)
        status = DeployStatus.success if result["exit_code"] == 0 else DeployStatus.failed
        output = result["stdout"] + result["stderr"]
    except Exception as e:
        status = DeployStatus.failed
        output = str(e)

    async with AsyncSessionLocal() as db:
        await db.execute(
            update(Deployment).where(Deployment.id == UUID(dep_id)).values(
                status=status,
                output=output,
                completed_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()


# ════════════════════════════════════════════════════════════════════════════════
# FILE TRANSFERS
# ════════════════════════════════════════════════════════════════════════════════

@router.post("/api/nodes/{node_id}/files/download", tags=["files"])
async def download_from_node(
    node_id: str,
    remote_path: str = Query(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator),
):
    """Retrieve a file from the Pi."""
    result = await db.execute(select(Node).where(Node.node_id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(404, "Node not found")
    if not manager.is_connected(node_id):
        raise HTTPException(503, "Node is offline")

    transfer_id = str(uuid.uuid4())
    try:
        data = await manager.request_file_download(node_id, remote_path, transfer_id)
    except Exception as e:
        raise HTTPException(500, f"Transfer failed: {e}")

    filename = remote_path.split("/")[-1]
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


# ════════════════════════════════════════════════════════════════════════════════
# SERVER-SENT EVENTS (real-time dashboard)
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/api/events", tags=["realtime"])
async def event_stream(request: Request, _: User = Depends(require_viewer)):
    """SSE endpoint for real-time node events."""
    q = manager.subscribe_events()

    async def generate():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    import json
                    event = await asyncio.wait_for(q.get(), timeout=30)
                    yield f"data: {json.dumps(event)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            manager.unsubscribe_events(q)

    return StreamingResponse(generate(), media_type="text/event-stream")


# ════════════════════════════════════════════════════════════════════════════════
# STATS OVERVIEW
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/api/stats", tags=["stats"])
async def get_stats(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_viewer),
):
    total_nodes = await db.execute(select(func.count()).select_from(Node))
    online_count = manager.connected_count()
    total_cmds = await db.execute(select(func.count()).select_from(Command))
    active_alerts = await db.execute(
        select(func.count()).select_from(Alert).where(Alert.resolved_at.is_(None))
    )
    return {
        "nodes_total": total_nodes.scalar(),
        "nodes_online": online_count,
        "commands_total": total_cmds.scalar(),
        "alerts_active": active_alerts.scalar(),
    }


# ════════════════════════════════════════════════════════════════════════════════
# USER MANAGEMENT
# ════════════════════════════════════════════════════════════════════════════════

class UserCreate(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8)
    email: Optional[str] = None
    role: str = "viewer"


class UserUpdate(BaseModel):
    email: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None
    password: Optional[str] = Field(None, min_length=8)


@router.get("/api/users", tags=["users"])
async def list_users(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    result = await db.execute(select(User).order_by(User.username))
    users = result.scalars().all()
    return [
        {
            "id": str(u.id),
            "username": u.username,
            "email": u.email,
            "role": u.role.value,
            "is_active": u.is_active,
            "last_login": u.last_login.isoformat() if u.last_login else None,
            "created_at": u.created_at.isoformat(),
        }
        for u in users
    ]


@router.post("/api/users", status_code=201, tags=["users"])
async def create_user(
    body: UserCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_admin),
):
    existing = await db.execute(select(User).where(User.username == body.username))
    if existing.scalar_one_or_none():
        raise HTTPException(409, "Username already exists")

    try:
        role = UserRole(body.role)
    except ValueError:
        raise HTTPException(400, f"Invalid role. Valid roles: {[r.value for r in UserRole]}")

    user = User(
        username=body.username,
        email=body.email,
        password_hash=hash_password(body.password),
        role=role,
        is_active=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return {"id": str(user.id), "username": user.username, "role": user.role.value}


@router.patch("/api/users/{user_id}", tags=["users"])
async def update_user(
    user_id: UUID,
    body: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")

    updates = {}
    if body.email is not None:
        updates["email"] = body.email
    if body.is_active is not None:
        updates["is_active"] = body.is_active
    if body.role is not None:
        try:
            updates["role"] = UserRole(body.role)
        except ValueError:
            raise HTTPException(400, "Invalid role")
    if body.password is not None:
        updates["password_hash"] = hash_password(body.password)

    if updates:
        await db.execute(update(User).where(User.id == user_id).values(**updates))
        await db.commit()

    return {"status": "updated"}


@router.delete("/api/users/{user_id}", status_code=204, tags=["users"])
async def delete_user(
    user_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    if str(user_id) == str(current_user.id):
        raise HTTPException(400, "Cannot delete yourself")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
    await db.delete(user)
    await db.commit()


@router.post("/api/auth/change-password", tags=["auth"])
async def change_password(
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old_pw = body.get("old_password", "")
    new_pw = body.get("new_password", "")
    if not new_pw or len(new_pw) < 8:
        raise HTTPException(400, "New password must be at least 8 characters")
    if not verify_password(old_pw, current_user.password_hash):
        raise HTTPException(401, "Current password is incorrect")
    await db.execute(
        update(User).where(User.id == current_user.id)
        .values(password_hash=hash_password(new_pw))
    )
    await db.commit()
    return {"status": "password changed"}


# ════════════════════════════════════════════════════════════════════════════════
# NODE SERVICES (real data from push)
# ════════════════════════════════════════════════════════════════════════════════

@router.get("/api/nodes/{node_id}/services", tags=["nodes"])
async def get_node_services(
    node_id: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_viewer),
):
    from server.db.models import NodeService
    result = await db.execute(select(Node).where(Node.node_id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(404, "Node not found")

    svcs = await db.execute(
        select(NodeService)
        .where(NodeService.node_id == node.id)
        .order_by(NodeService.service_name)
    )
    return [
        {
            "name": s.service_name,
            "active": s.is_active,
            "enabled": s.is_enabled,
            "last_checked": s.last_checked.isoformat() if s.last_checked else None,
        }
        for s in svcs.scalars().all()
    ]


# ════════════════════════════════════════════════════════════════════════════════
# FILE UPLOAD (push file TO node)
# ════════════════════════════════════════════════════════════════════════════════

class FileUploadRequest(BaseModel):
    dest_path: str


@router.post("/api/nodes/{node_id}/files/upload", tags=["files"])
async def upload_to_node(
    node_id: str,
    dest_path: str = Query(..., description="Destination path on the Pi"),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator),
):
    """Push a file to the Pi."""
    result = await db.execute(select(Node).where(Node.node_id == node_id))
    node = result.scalar_one_or_none()
    if not node:
        raise HTTPException(404, "Node not found")
    if not manager.is_connected(node_id):
        raise HTTPException(503, "Node is offline")

    from server.core.config import get_settings
    s = get_settings()
    content = await file.read()
    if len(content) > s.MAX_UPLOAD_SIZE_MB * 1024 * 1024:
        raise HTTPException(413, f"File exceeds {s.MAX_UPLOAD_SIZE_MB}MB limit")

    transfer_id = str(uuid.uuid4())
    try:
        await manager.push_file_to_node(node_id, dest_path, content, transfer_id)
    except Exception as e:
        raise HTTPException(500, f"Transfer failed: {e}")

    return {"status": "uploaded", "dest_path": dest_path, "bytes": len(content)}


# ════════════════════════════════════════════════════════════════════════════════
# ALERT RULES
# ════════════════════════════════════════════════════════════════════════════════

class AlertRuleCreate(BaseModel):
    node_id: Optional[str] = None   # None = global rule
    metric: str                     # cpu_percent, ram_percent, disk_percent, cpu_temp_c
    operator: str                   # gt, lt, gte, lte
    threshold: float
    severity: str = "warning"
    message: Optional[str] = None


@router.get("/api/alert-rules", tags=["alerts"])
async def list_alert_rules(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_viewer),
):
    from server.db.models import AlertRule
    result = await db.execute(select(AlertRule).order_by(AlertRule.created_at))
    rules = result.scalars().all()
    return [
        {
            "id": str(r.id),
            "node_id": str(r.node_id) if r.node_id else None,
            "metric": r.metric,
            "operator": r.operator,
            "threshold": r.threshold,
            "severity": r.severity.value,
            "message": r.message,
            "enabled": r.enabled,
        }
        for r in rules
    ]


@router.post("/api/alert-rules", status_code=201, tags=["alerts"])
async def create_alert_rule(
    body: AlertRuleCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_operator),
):
    from server.db.models import AlertRule

    node_db_id = None
    if body.node_id:
        result = await db.execute(select(Node).where(Node.node_id == body.node_id))
        node = result.scalar_one_or_none()
        if not node:
            raise HTTPException(404, "Node not found")
        node_db_id = node.id

    rule = AlertRule(
        node_id=node_db_id,
        metric=body.metric,
        operator=body.operator,
        threshold=body.threshold,
        severity=AlertSeverity(body.severity),
        message=body.message or f"{body.metric} {body.operator} {body.threshold}",
        enabled=True,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    return {"id": str(rule.id), "status": "created"}


@router.delete("/api/alert-rules/{rule_id}", status_code=204, tags=["alerts"])
async def delete_alert_rule(
    rule_id: UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_operator),
):
    from server.db.models import AlertRule
    result = await db.execute(select(AlertRule).where(AlertRule.id == rule_id))
    rule = result.scalar_one_or_none()
    if not rule:
        raise HTTPException(404, "Rule not found")
    await db.delete(rule)
    await db.commit()
