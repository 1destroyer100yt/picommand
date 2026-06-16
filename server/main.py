"""
PiCommand Server — Main Application
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from server.core.config import get_settings
from server.db.database import init_db
from server.api.node_ws import router as ws_router
from server.api.routes import router as api_router
from server.services.rate_limit import limiter
from server.services.background import (
    offline_watchdog, metrics_pruner, job_scheduler, server_auto_update
)

settings = get_settings()

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("picommand")

# Background task handles (so we can cancel them on shutdown)
_bg_tasks: list[asyncio.Task] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting {settings.APP_NAME} v{settings.APP_VERSION}")
    await init_db()
    Path(settings.UPLOAD_DIR).mkdir(parents=True, exist_ok=True)

    # Launch background tasks (Issues #3, #11, #12, #17)
    app.state.update_in_progress = False
    _bg_tasks.append(asyncio.create_task(offline_watchdog()))
    _bg_tasks.append(asyncio.create_task(metrics_pruner()))
    _bg_tasks.append(asyncio.create_task(job_scheduler()))
    _bg_tasks.append(asyncio.create_task(server_auto_update()))
    logger.info(f"Started {len(_bg_tasks)} background tasks")

    yield

    for t in _bg_tasks:
        t.cancel()
    for t in _bg_tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass
    logger.info("Shutdown complete")


app = FastAPI(
    title="PiCommand",
    version=settings.APP_VERSION,
    description="Self-hosted Raspberry Pi Remote Management Platform",
    lifespan=lifespan,
    docs_url="/api/docs" if settings.DEBUG else None,
    redoc_url=None,
)

# Rate limiting (Issue #7)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_HOSTS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API and WebSocket routes
app.include_router(ws_router)
app.include_router(api_router)

# Serve the dashboard SPA
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    if (static_dir / "assets").exists():
        app.mount("/assets", StaticFiles(directory=str(static_dir / "assets")), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str):
        if full_path.startswith("ws/") or full_path.startswith("api/"):
            from fastapi import HTTPException
            raise HTTPException(status_code=404)
        index = static_dir / "index.html"
        if index.exists():
            return FileResponse(str(index))
        return {"detail": "Dashboard not built yet"}
