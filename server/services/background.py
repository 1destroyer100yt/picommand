"""
Background tasks (Issues #3, #11, #12, #17)

All tasks are launched from main.py's lifespan and cancelled on shutdown.
Each loop swallows and logs its own exceptions so one failure never kills the
loop or the process.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import uuid
from datetime import datetime, timezone, timedelta

import httpx
from sqlalchemy import select, update, delete

from server.core.config import get_settings
from server.db.database import AsyncSessionLocal
from server.db.models import (
    Node, NodeMetric, NodeStatus, ScheduledJob, AuditLog, Command, CommandStatus
)
from server.services.connection_manager import manager
from server.services.notification_service import dispatch_node_offline

logger = logging.getLogger("picommand.background")
settings = get_settings()


# ════════════════════════════════════════════════════════════════════════════════
# #3 — Offline watchdog
# ════════════════════════════════════════════════════════════════════════════════

async def offline_watchdog():
    """
    Every 30s, mark nodes offline whose last_seen is older than
    WS_HEARTBEAT_INTERVAL * 3 but whose status isn't already offline.
    Fires a critical notification on each transition.
    """
    interval = 30
    stale_after = settings.WS_HEARTBEAT_INTERVAL * 3
    while True:
        try:
            await asyncio.sleep(interval)
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after)
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Node).where(
                        Node.status != NodeStatus.offline,
                        Node.last_seen.isnot(None),
                        Node.last_seen < cutoff,
                    )
                )
                stale = result.scalars().all()
                for node in stale:
                    # Skip if the node is actually still connected (heartbeat
                    # may just be lagging the DB write).
                    if manager.is_connected(node.node_id):
                        continue
                    await db.execute(
                        update(Node).where(Node.id == node.id)
                        .values(status=NodeStatus.offline)
                    )
                    logger.warning(f"Watchdog: node {node.node_id} marked offline")
                    db.add(AuditLog(
                        node_id=node.id,
                        action="node_offline_watchdog",
                        details={"last_seen": node.last_seen.isoformat()},
                    ))
                    # Fire notification after we know the row exists
                    asyncio.create_task(dispatch_node_offline(
                        node.node_id,
                        f"Node {node.node_id} went offline unexpectedly",
                    ))
                if stale:
                    await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"offline_watchdog error: {e}")


# ════════════════════════════════════════════════════════════════════════════════
# #11 — Metrics pruner
# ════════════════════════════════════════════════════════════════════════════════

async def metrics_pruner():
    """Once daily, delete NodeMetric rows older than METRICS_RETENTION_DAYS."""
    interval = 24 * 3600
    while True:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=settings.METRICS_RETENTION_DAYS)
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    delete(NodeMetric).where(NodeMetric.recorded_at < cutoff)
                )
                await db.commit()
                deleted = result.rowcount or 0
                logger.info(f"Metrics pruner: deleted {deleted} rows older than "
                            f"{settings.METRICS_RETENTION_DAYS} days")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"metrics_pruner error: {e}")
        await asyncio.sleep(interval)


# ════════════════════════════════════════════════════════════════════════════════
# #12 — Cron scheduler
# ════════════════════════════════════════════════════════════════════════════════

async def job_scheduler():
    """
    Every minute, run any enabled ScheduledJob whose cron expression is due
    against connected nodes. Updates last_run and last_exit_code.
    """
    try:
        from croniter import croniter
    except ImportError:
        logger.warning("croniter not installed — scheduled jobs disabled")
        return

    while True:
        try:
            await asyncio.sleep(60)
            now = datetime.now(timezone.utc)
            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(ScheduledJob).where(ScheduledJob.enabled.is_(True))
                )
                jobs = result.scalars().all()

                for job in jobs:
                    if not croniter.is_valid(job.cron_expression):
                        continue
                    # Determine the most recent scheduled fire time at or before now.
                    base = job.last_run or (now - timedelta(minutes=1))
                    if base.tzinfo is None:
                        base = base.replace(tzinfo=timezone.utc)
                    itr = croniter(job.cron_expression, base)
                    next_fire = itr.get_next(datetime)
                    if next_fire.tzinfo is None:
                        next_fire = next_fire.replace(tzinfo=timezone.utc)

                    if next_fire <= now:
                        # Resolve node slug
                        node_res = await db.execute(select(Node).where(Node.id == job.node_id))
                        node = node_res.scalar_one_or_none()
                        if not node or not manager.is_connected(node.node_id):
                            # Still advance last_run so we don't spin on a backlog.
                            await db.execute(
                                update(ScheduledJob).where(ScheduledJob.id == job.id)
                                .values(last_run=now)
                            )
                            continue

                        exit_code = None
                        try:
                            cmd_id = str(uuid.uuid4())
                            r = await manager.execute_command(node.node_id, job.command, cmd_id, timeout=120)
                            exit_code = r.get("exit_code")
                            logger.info(f"Scheduled job {job.id} ran on {node.node_id} exit={exit_code}")
                        except Exception as e:
                            logger.warning(f"Scheduled job {job.id} failed: {e}")

                        await db.execute(
                            update(ScheduledJob).where(ScheduledJob.id == job.id)
                            .values(last_run=now, last_exit_code=exit_code)
                        )
                        db.add(AuditLog(
                            node_id=job.node_id,
                            action="scheduled_job_run",
                            details={"job_id": str(job.id), "exit_code": exit_code},
                        ))
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"job_scheduler error: {e}")


# ════════════════════════════════════════════════════════════════════════════════
# #17 — Server auto-update
# ════════════════════════════════════════════════════════════════════════════════

async def _log_auto_update(component, old_version, new_version, success, error=None):
    try:
        async with AsyncSessionLocal() as db:
            db.add(AuditLog(
                action="auto_update",
                details={
                    "component": component,
                    "old_version": old_version,
                    "new_version": new_version,
                    "success": success,
                    "error": error,
                },
            ))
            await db.commit()
    except Exception as e:
        logger.error(f"failed to log auto_update: {e}")


async def server_auto_update():
    """
    Every SERVER_UPDATE_CHECK_HOURS, git fetch and compare HEAD to origin/main.
    If behind, set the update-in-progress flag, run scripts/update-server.sh,
    and exit (systemd restarts us). The update script itself handles rollback.
    """
    if not settings.SERVER_AUTO_UPDATE:
        logger.info("Server auto-update disabled")
        return

    interval = settings.SERVER_UPDATE_CHECK_HOURS * 3600
    repo = settings.REPO_DIR

    while True:
        try:
            await asyncio.sleep(interval)

            def _run(args):
                return subprocess.run(args, cwd=repo, capture_output=True, text=True, timeout=120)

            fetch = _run(["git", "fetch", "origin", "main"])
            if fetch.returncode != 0:
                logger.warning(f"git fetch failed: {fetch.stderr.strip()}")
                continue

            head = _run(["git", "rev-parse", "HEAD"]).stdout.strip()
            origin = _run(["git", "rev-parse", "origin/main"]).stdout.strip()

            if head and origin and head != origin:
                logger.warning(f"Server behind origin/main ({head[:8]} → {origin[:8]}); updating")
                manager.set_update_in_progress(True)
                await _log_auto_update("server", head[:8], origin[:8], success=True)
                # Hand off to the update script; it restarts the service.
                subprocess.Popen(
                    ["bash", f"{repo}/scripts/update-server.sh"],
                    cwd=repo,
                )
                # Give the script a moment; systemd will stop us.
                await asyncio.sleep(5)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"server_auto_update error: {e}")
            await _log_auto_update("server", None, None, success=False, error=str(e))
