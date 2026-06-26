"""
FastAPI dependency injection for authentication and authorization.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import Depends, HTTPException, status, WebSocket
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from server.core.security import decode_token, hash_api_token
from server.db.database import get_db
from server.db.models import User, UserRole, ApiToken

bearer_scheme = HTTPBearer(auto_error=False)


# ── JWT Auth ───────────────────────────────────────────────────────────────────

async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")

    token = credentials.credentials

    # Try JWT first. Only an access token is valid for API access — a refresh
    # token must not be usable as a bearer credential here.
    try:
        payload = decode_token(token)
        if payload.get("type") == "refresh":
            raise HTTPException(status_code=401, detail="Refresh token cannot be used for API access")
        user_id = payload.get("sub")
        if not user_id:
            raise ValueError("no sub")
        result = await db.execute(select(User).where(User.id == UUID(user_id)))
        user = result.scalar_one_or_none()
        if not user or not user.is_active:
            raise HTTPException(status_code=401, detail="User not found or disabled")
        return user
    except (JWTError, ValueError):
        # Issue #6: UUID(user_id) raises ValueError for non-UUID subs;
        # catch it here so malformed tokens return 401, not 500.
        pass

    # Try API token
    token_hash = hash_api_token(token)
    result = await db.execute(
        select(ApiToken).where(ApiToken.token_hash == token_hash)
    )
    api_token = result.scalar_one_or_none()
    if not api_token:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Reject expired tokens
    if api_token.expires_at is not None:
        exp = api_token.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        if exp < datetime.now(timezone.utc):
            raise HTTPException(status_code=401, detail="Token expired")

    result = await db.execute(select(User).where(User.id == api_token.user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or disabled")

    # Issue #8: stamp last_used on successful API-token auth.
    await db.execute(
        update(ApiToken)
        .where(ApiToken.id == api_token.id)
        .values(last_used=datetime.now(timezone.utc))
    )
    await db.commit()

    return user


# ── RBAC Helpers ───────────────────────────────────────────────────────────────

class RequireRole:
    """Dependency factory: RequireRole('admin') or RequireRole('admin', 'operator')"""

    def __init__(self, *roles: str):
        self.roles = {UserRole(r) for r in roles}

    async def __call__(self, user: User = Depends(get_current_user)) -> User:
        if user.role not in self.roles:
            raise HTTPException(
                status_code=403,
                detail=f"Requires role: {', '.join(r.value for r in self.roles)}"
            )
        return user


require_admin = RequireRole("admin")
require_operator = RequireRole("admin", "operator")
require_viewer = RequireRole("admin", "operator", "viewer")


# ── WebSocket Auth ─────────────────────────────────────────────────────────────

async def ws_get_node_id(websocket: WebSocket) -> Optional[str]:
    """Extract node_id from WS query params or headers."""
    return websocket.query_params.get("node_id")
