"""
Alert Service

Checks incoming metrics against configured alert rules and fires/resolves alerts.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select, update

from server.db.database import AsyncSessionLocal
from server.db.models import Alert, AlertRule, AlertSeverity

logger = logging.getLogger("picommand.alerts")

# Default thresholds (used if no custom rules)
DEFAULT_RULES = [
    ("cpu_percent", 90.0, AlertSeverity.warning),
    ("cpu_percent", 98.0, AlertSeverity.critical),
    ("ram_percent", 85.0, AlertSeverity.warning),
    ("ram_percent", 95.0, AlertSeverity.critical),
    ("disk_percent", 80.0, AlertSeverity.warning),
    ("disk_percent", 95.0, AlertSeverity.critical),
    ("cpu_temp_c", 70.0, AlertSeverity.warning),
    ("cpu_temp_c", 80.0, AlertSeverity.critical),
]


async def check_metric_alerts(node_db_id: UUID, metrics: dict):
    """
    Called after every metrics push.
    Fires new alerts if thresholds exceeded, resolves existing if recovered.
    """
    async with AsyncSessionLocal() as db:
        for metric_name, threshold, severity in DEFAULT_RULES:
            value = metrics.get(metric_name)
            if value is None:
                continue

            # Find existing unresolved alert for this node+metric+severity
            existing = await db.execute(
                select(Alert).where(
                    Alert.node_id == node_db_id,
                    Alert.metric == metric_name,
                    Alert.severity == severity,
                    Alert.resolved_at.is_(None)
                )
            )
            existing_alert = existing.scalar_one_or_none()

            if value >= threshold:
                # Fire alert if not already active
                if not existing_alert:
                    alert = Alert(
                        node_id=node_db_id,
                        severity=severity,
                        message=f"{metric_name} is {value:.1f} (threshold: {threshold})",
                        metric=metric_name,
                        metric_value=value,
                    )
                    db.add(alert)
                    logger.warning(f"Alert fired: node={node_db_id} {metric_name}={value}")
            else:
                # Resolve if was active
                if existing_alert:
                    existing_alert.resolved_at = datetime.now(timezone.utc)
                    logger.info(f"Alert resolved: node={node_db_id} {metric_name}={value}")

        await db.commit()
