"""
PiCommand Server Configuration
"""
from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache
from pathlib import Path


class Settings(BaseSettings):
    # ── App ──────────────────────────────────────────────────────────────
    APP_NAME: str = "PiCommand"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    SECRET_KEY: str = Field(..., description="Random 64-char hex string")
    ALLOWED_HOSTS: list[str] = ["*"]

    # ── Database ─────────────────────────────────────────────────────────
    DATABASE_URL: str = Field(
        "postgresql+asyncpg://picommand:picommand@localhost:5432/picommand",
        description="Async PostgreSQL DSN"
    )

    # ── Redis ─────────────────────────────────────────────────────────────
    REDIS_URL: str = "redis://localhost:6379/0"

    # ── Auth ─────────────────────────────────────────────────────────────
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30

    # ── SSH Tunnel Management ─────────────────────────────────────────────
    SSH_HOST_KEY_PATH: str = "/etc/picommand/ssh/host_key"
    SSH_AUTHORIZED_KEYS_DIR: str = "/etc/picommand/ssh/authorized"
    TUNNEL_PORT_RANGE_START: int = 12000
    TUNNEL_PORT_RANGE_END: int = 13000
    SSH_SERVER_PORT: int = 2222           # internal sshd for reverse tunnels

    # ── WebSocket ────────────────────────────────────────────────────────
    WS_HEARTBEAT_INTERVAL: int = 30       # seconds
    WS_COMMAND_TIMEOUT: int = 60

    # ── Metrics ──────────────────────────────────────────────────────────
    METRICS_RETENTION_DAYS: int = 7
    METRICS_HOURLY_RETENTION_DAYS: int = 90

    # ── File Transfers ────────────────────────────────────────────────────
    UPLOAD_DIR: str = "/var/lib/picommand/uploads"
    MAX_UPLOAD_SIZE_MB: int = 512

    # ── TLS (for production, put behind nginx) ────────────────────────────
    TLS_CERT_PATH: str = ""
    TLS_KEY_PATH: str = ""

    # ── Server Bind ───────────────────────────────────────────────────────
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
