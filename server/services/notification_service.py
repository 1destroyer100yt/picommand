"""
Notification Service (Issue #2)

Dispatches alert notifications to external channels. Every channel is wrapped
in its own try/except so a broken endpoint can never crash the alert check or
the watchdog that calls it.

Channels:
  - Webhook  : POST JSON to ALERT_WEBHOOK_URL
  - ntfy.sh  : POST plain-text body to {NTFY_URL}/{NTFY_TOPIC}

Both channels are no-ops when their config is blank, so the server runs
unchanged with nothing configured.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import httpx

from server.core.config import get_settings

logger = logging.getLogger("picommand.notifications")

# Severity may arrive as an enum (AlertSeverity), a plain string, or None.
_NTFY_PRIORITY = {
    "info": "low",
    "warning": "default",
    "critical": "urgent",
}
_NTFY_TAGS = {
    "info": "information_source",
    "warning": "warning",
    "critical": "rotating_light",
}


def _severity_str(severity) -> str:
    if severity is None:
        return "warning"
    # AlertSeverity is a str-enum, so .value or str() both work; be defensive.
    return getattr(severity, "value", str(severity))


async def dispatch_alert(
    alert,
    node_id_str: str,
    event_type: str,
    threshold: Optional[float] = None,
) -> None:
    """
    Fan out an alert to all configured channels.

    Args:
        alert:        the Alert ORM object (has .severity, .message, .metric, .metric_value)
        node_id_str:  human-readable node_id (slug), e.g. "tifftls"
        event_type:   "alert_fired" or "alert_resolved"
        threshold:    the rule threshold that triggered this (optional)
    """
    s = get_settings()
    severity = _severity_str(getattr(alert, "severity", None))
    message = getattr(alert, "message", "")
    metric = getattr(alert, "metric", None)
    metric_value = getattr(alert, "metric_value", None)
    timestamp = datetime.now(timezone.utc).isoformat()

    payload = {
        "event": event_type,
        "node_id": node_id_str,
        "metric": metric,
        "value": metric_value,
        "threshold": threshold,
        "severity": severity,
        "message": message,
        "timestamp": timestamp,
    }

    # ── Webhook channel ────────────────────────────────────────────────────
    if s.ALERT_WEBHOOK_URL:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(s.ALERT_WEBHOOK_URL, json=payload)
        except Exception as e:
            logger.warning(f"Webhook notification failed: {e}")

    # ── ntfy.sh channel ────────────────────────────────────────────────────
    if s.NTFY_URL and s.NTFY_TOPIC:
        try:
            url = f"{s.NTFY_URL.rstrip('/')}/{s.NTFY_TOPIC}"
            headers = {
                "Title": f"PiCommand Alert - {node_id_str}",
                "Priority": _NTFY_PRIORITY.get(severity, "default"),
                "Tags": _NTFY_TAGS.get(severity, "warning"),
            }
            body = message or f"{event_type} on {node_id_str}"
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(url, content=body.encode("utf-8"), headers=headers)
        except Exception as e:
            logger.warning(f"ntfy notification failed: {e}")


async def dispatch_node_offline(node_id_str: str, message: str) -> None:
    """
    Convenience wrapper for the offline watchdog (Issue #3). Builds a minimal
    alert-like object and dispatches it as a critical 'alert_fired' event.
    """
    class _AdHoc:
        severity = "critical"
        metric = "connectivity"
        metric_value = None

    a = _AdHoc()
    a.message = message
    await dispatch_alert(a, node_id_str, "alert_fired", threshold=None)
