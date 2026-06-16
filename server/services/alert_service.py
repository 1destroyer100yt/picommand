"""
Alert Service (Issues #1 & #2)

Checks incoming metrics against alert rules and fires/resolves alerts.

Rule resolution order, per metric:
  1. Custom AlertRule rows that target this node (node_id == this node)
  2. Custom global AlertRule rows (node_id IS NULL)
  3. Built-in DEFAULT_RULES — used for a metric ONLY when no custom rule
     (node-specific or global) covers that metric.

All four operators are supported: gt, lt, gte, lte.
On every fire and resolve we dispatch a notification (webhook + ntfy), each
wrapped so a broken endpoint can't crash the check.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select

from server.db.database import AsyncSessionLocal
from server.db.models import Alert, AlertRule, AlertSeverity, Node
from server.services.notification_service import dispatch_alert

logger = logging.getLogger("picommand.alerts")

# Built-in fallback thresholds. Each entry: (metric, operator, threshold, severity)
# These only apply to a metric when no custom rule covers that metric.
DEFAULT_RULES = [
    ("cpu_percent", "gte", 90.0, AlertSeverity.warning),
    ("cpu_percent", "gte", 98.0, AlertSeverity.critical),
    ("ram_percent", "gte", 85.0, AlertSeverity.warning),
    ("ram_percent", "gte", 95.0, AlertSeverity.critical),
    ("disk_percent", "gte", 80.0, AlertSeverity.warning),
    ("disk_percent", "gte", 95.0, AlertSeverity.critical),
    ("cpu_temp_c", "gte", 70.0, AlertSeverity.warning),
    ("cpu_temp_c", "gte", 80.0, AlertSeverity.critical),
]


def _evaluate_operator(value: float, op: str, threshold: float) -> bool:
    """Return True if `value op threshold` holds."""
    if op == "gt":
        return value > threshold
    if op == "lt":
        return value < threshold
    if op == "gte":
        return value >= threshold
    if op == "lte":
        return value <= threshold
    # Unknown operator → never fires (fail safe, logged once per evaluation)
    logger.warning(f"Unknown alert operator: {op!r}")
    return False


class _Rule:
    """Normalized rule used internally for both custom and default rules."""
    __slots__ = ("metric", "operator", "threshold", "severity", "message", "source")

    def __init__(self, metric, operator, threshold, severity, message, source):
        self.metric = metric
        self.operator = operator
        self.threshold = threshold
        self.severity = severity
        self.message = message
        self.source = source  # "custom" or "default"


async def _resolve_node_slug(db, node_db_id: UUID) -> str:
    res = await db.execute(select(Node.node_id).where(Node.id == node_db_id))
    slug = res.scalar_one_or_none()
    return slug or str(node_db_id)


async def _gather_rules(db, node_db_id: UUID) -> list[_Rule]:
    """
    Build the effective rule set for this node:
      custom rules (node-specific + global) take precedence per metric;
      defaults fill in only for metrics with no custom coverage.
    """
    result = await db.execute(
        select(AlertRule).where(
            AlertRule.enabled.is_(True),
            (AlertRule.node_id == node_db_id) | (AlertRule.node_id.is_(None)),
        )
    )
    custom = result.scalars().all()

    rules: list[_Rule] = []
    covered_metrics: set[str] = set()
    for r in custom:
        rules.append(_Rule(
            metric=r.metric,
            operator=r.operator,
            threshold=r.threshold,
            severity=r.severity,
            message=r.message,
            source="custom",
        ))
        covered_metrics.add(r.metric)

    # Add defaults only for metrics not covered by any custom rule
    for metric, operator, threshold, severity in DEFAULT_RULES:
        if metric in covered_metrics:
            continue
        rules.append(_Rule(
            metric=metric,
            operator=operator,
            threshold=threshold,
            severity=severity,
            message=None,
            source="default",
        ))

    return rules


async def check_metric_alerts(node_db_id: UUID, metrics: dict):
    """
    Called after every metrics push.
    Fires new alerts if a rule matches, resolves existing ones if recovered.
    Dedup is per (node, metric, severity): one active alert per combination.
    """
    async with AsyncSessionLocal() as db:
        rules = await _gather_rules(db, node_db_id)
        node_slug = await _resolve_node_slug(db, node_db_id)

        # Track which (metric, severity) combinations fired this round so that a
        # recovered metric with no firing rule gets its stale alert resolved.
        evaluated_keys: set[tuple[str, AlertSeverity]] = set()
        fired_keys: set[tuple[str, AlertSeverity]] = set()

        # Collect notifications to dispatch AFTER commit (so DB state is durable
        # before we tell the outside world).
        to_notify: list[tuple[Alert, str, str, float | None]] = []

        for rule in rules:
            value = metrics.get(rule.metric)
            if value is None:
                continue

            key = (rule.metric, rule.severity)
            evaluated_keys.add(key)

            existing = await db.execute(
                select(Alert).where(
                    Alert.node_id == node_db_id,
                    Alert.metric == rule.metric,
                    Alert.severity == rule.severity,
                    Alert.resolved_at.is_(None),
                )
            )
            existing_alert = existing.scalar_one_or_none()

            if _evaluate_operator(value, rule.operator, rule.threshold):
                fired_keys.add(key)
                if not existing_alert:
                    msg = rule.message or (
                        f"{rule.metric} is {value:.1f} "
                        f"({rule.operator} {rule.threshold})"
                    )
                    alert = Alert(
                        node_id=node_db_id,
                        severity=rule.severity,
                        message=msg,
                        metric=rule.metric,
                        metric_value=value,
                    )
                    db.add(alert)
                    logger.warning(
                        f"Alert fired [{rule.source}]: node={node_slug} "
                        f"{rule.metric}={value} {rule.operator} {rule.threshold}"
                    )
                    to_notify.append((alert, node_slug, "alert_fired", rule.threshold))

        # Resolve any active alert whose (metric, severity) was evaluated this
        # round but did NOT fire.
        for key in evaluated_keys - fired_keys:
            metric_name, severity = key
            existing = await db.execute(
                select(Alert).where(
                    Alert.node_id == node_db_id,
                    Alert.metric == metric_name,
                    Alert.severity == severity,
                    Alert.resolved_at.is_(None),
                )
            )
            existing_alert = existing.scalar_one_or_none()
            if existing_alert:
                existing_alert.resolved_at = datetime.now(timezone.utc)
                logger.info(f"Alert resolved: node={node_slug} {metric_name} ({severity.value})")
                to_notify.append((existing_alert, node_slug, "alert_resolved", None))

        await db.commit()

    # Dispatch notifications after the DB transaction is committed.
    for alert, slug, event_type, threshold in to_notify:
        await dispatch_alert(alert, slug, event_type, threshold)
