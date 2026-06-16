"""
SQLAlchemy ORM Models
"""
from __future__ import annotations
import enum
from datetime import datetime
from typing import Optional, List
from uuid import uuid4

from sqlalchemy import (
    Column, String, Text, Boolean, Integer, BigInteger,
    Float, DateTime, Enum, ForeignKey, ARRAY, JSON, Index, UniqueConstraint
)
from sqlalchemy.dialects.postgresql import UUID, INET, REAL
from sqlalchemy.orm import DeclarativeBase, relationship
from sqlalchemy.ext.asyncio import AsyncAttrs


class Base(AsyncAttrs, DeclarativeBase):
    pass


class UserRole(str, enum.Enum):
    admin = "admin"
    operator = "operator"
    viewer = "viewer"


class NodeStatus(str, enum.Enum):
    online = "online"
    offline = "offline"
    pending = "pending"
    disabled = "disabled"


class CommandStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    timeout = "timeout"


class AlertSeverity(str, enum.Enum):
    info = "info"
    warning = "warning"
    critical = "critical"


class DeployStatus(str, enum.Enum):
    pending = "pending"
    in_progress = "in_progress"
    success = "success"
    failed = "failed"


class User(Base):
    __tablename__ = "users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    username = Column(String(64), unique=True, nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    role = Column(Enum(UserRole, name='user_role'), nullable=False, default=UserRole.viewer)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    last_login = Column(DateTime(timezone=True))

    api_tokens = relationship("ApiToken", back_populates="user", cascade="all, delete")


class ApiToken(Base):
    __tablename__ = "api_tokens"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash = Column(Text, unique=True, nullable=False)
    name = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    expires_at = Column(DateTime(timezone=True))
    last_used = Column(DateTime(timezone=True))

    user = relationship("User", back_populates="api_tokens")


class Node(Base):
    __tablename__ = "nodes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    node_id = Column(String(64), unique=True, nullable=False)
    display_name = Column(String(128), nullable=False)
    description = Column(Text)
    public_key = Column(Text, nullable=False)
    status = Column(Enum(NodeStatus, name='node_status'), nullable=False, default=NodeStatus.pending)
    tags = Column(ARRAY(String), default=[])
    location = Column(String(255))
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    approved_at = Column(DateTime(timezone=True))
    approved_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    last_seen = Column(DateTime(timezone=True))
    ssh_tunnel_port = Column(Integer, unique=True)
    ip_address = Column(INET)
    hostname = Column(String(255))
    os_version = Column(Text)
    arch = Column(String(32))
    pi_model = Column(Text)
    agent_version = Column(String(32))   # Issue #16: reported agent version

    metrics = relationship("NodeMetric", back_populates="node", cascade="all, delete")
    commands = relationship("Command", back_populates="node", cascade="all, delete")
    services = relationship("NodeService", back_populates="node", cascade="all, delete")
    alerts = relationship("Alert", back_populates="node", cascade="all, delete")
    deployments = relationship("Deployment", back_populates="node", cascade="all, delete")


class NodeMetric(Base):
    __tablename__ = "node_metrics"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    node_id = Column(UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    recorded_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    cpu_percent = Column(Float)
    ram_percent = Column(Float)
    ram_used_mb = Column(Integer)
    ram_total_mb = Column(Integer)
    disk_percent = Column(Float)
    disk_used_gb = Column(Float)
    disk_total_gb = Column(Float)
    cpu_temp_c = Column(Float)
    load_avg_1 = Column(Float)
    load_avg_5 = Column(Float)
    load_avg_15 = Column(Float)
    uptime_seconds = Column(BigInteger)
    net_bytes_sent = Column(BigInteger)
    net_bytes_recv = Column(BigInteger)

    node = relationship("Node", back_populates="metrics")

    __table_args__ = (
        Index("idx_metrics_node_time", "node_id", "recorded_at"),
    )


class NodeService(Base):
    __tablename__ = "node_services"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    node_id = Column(UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    service_name = Column(String(128), nullable=False)
    is_active = Column(Boolean)
    is_enabled = Column(Boolean)
    last_checked = Column(DateTime(timezone=True))

    node = relationship("Node", back_populates="services")

    # Issue #4: required for the ON CONFLICT (node_id, service_name) upsert in node_ws.py
    __table_args__ = (
        UniqueConstraint("node_id", "service_name", name="uq_node_service"),
    )


class Command(Base):
    __tablename__ = "commands"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    node_id = Column(UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    issued_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    command = Column(Text, nullable=False)
    status = Column(Enum(CommandStatus, name='command_status'), nullable=False, default=CommandStatus.pending)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    exit_code = Column(Integer)
    stdout = Column(Text)
    stderr = Column(Text)
    timeout_seconds = Column(Integer, nullable=False, default=30)

    node = relationship("Node", back_populates="commands")

    __table_args__ = (
        Index("idx_commands_node", "node_id", "created_at"),
    )


class AuditLog(Base):
    __tablename__ = "audit_log"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    node_id = Column(UUID(as_uuid=True), ForeignKey("nodes.id"))
    action = Column(String(128), nullable=False)
    details = Column(JSON)
    ip_address = Column(INET)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    __table_args__ = (
        Index("idx_audit_created", "created_at"),
        Index("idx_audit_node", "node_id", "created_at"),
    )


class Alert(Base):
    __tablename__ = "alerts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    node_id = Column(UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    severity = Column(Enum(AlertSeverity, name='alert_severity'), nullable=False)
    message = Column(Text, nullable=False)
    metric = Column(String(64))
    metric_value = Column(Float)
    fired_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    resolved_at = Column(DateTime(timezone=True))
    acknowledged_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    acknowledged_at = Column(DateTime(timezone=True))

    node = relationship("Node", back_populates="alerts")


class Deployment(Base):
    __tablename__ = "deployments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    node_id = Column(UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    initiated_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    package_name = Column(String(255), nullable=False)
    script = Column(Text, nullable=False)
    status = Column(Enum(DeployStatus, name='deploy_status'), nullable=False, default=DeployStatus.pending)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    completed_at = Column(DateTime(timezone=True))
    output = Column(Text)

    node = relationship("Node", back_populates="deployments")


class FileTransfer(Base):
    __tablename__ = "file_transfers"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    node_id = Column(UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    initiated_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    direction = Column(String(16), nullable=False)   # "push" or "pull"
    remote_path = Column(Text, nullable=False)
    file_size_bytes = Column(BigInteger)
    status = Column(String(32), nullable=False, default="pending")  # completed / failed
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    completed_at = Column(DateTime(timezone=True))
    error = Column(Text)

    node = relationship("Node")


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    node_id = Column(UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=True)
    metric = Column(String(64), nullable=False)
    operator = Column(String(8), nullable=False)     # gt, lt, gte, lte
    threshold = Column(Float, nullable=False)
    severity = Column(Enum(AlertSeverity, name='alert_severity'), nullable=False, default=AlertSeverity.warning)
    message = Column(Text)
    enabled = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)


class ScheduledJob(Base):
    """Issue #12: recurring command execution via cron expressions."""
    __tablename__ = "scheduled_jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid4)
    node_id = Column(UUID(as_uuid=True), ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    command = Column(Text, nullable=False)
    cron_expression = Column(String(128), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    last_run = Column(DateTime(timezone=True))
    last_exit_code = Column(Integer)
    created_by = Column(UUID(as_uuid=True), ForeignKey("users.id"))
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    node = relationship("Node")
