"""
Authentication: JWT tokens, password hashing, key verification
"""
from __future__ import annotations
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

from server.core.config import get_settings

settings = get_settings()
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


# ── Passwords ────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


# ── JWT Tokens ────────────────────────────────────────────────────────────────

def create_access_token(subject: str, role: str, expires_minutes: int | None = None) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=expires_minutes or settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    payload = {
        "sub": subject,
        "role": role,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "access",
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_refresh_token(subject: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    payload = {
        "sub": subject,
        "exp": expire,
        "iat": datetime.now(timezone.utc),
        "type": "refresh",
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Raises JWTError on invalid/expired tokens."""
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])


# ── API Tokens ────────────────────────────────────────────────────────────────

def generate_api_token() -> tuple[str, str]:
    """Returns (raw_token, hashed_token). Store only the hash."""
    raw = secrets.token_urlsafe(48)
    hashed = hashlib.sha256(raw.encode()).hexdigest()
    return raw, hashed


def hash_api_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


# ── Node Authentication ───────────────────────────────────────────────────────

def generate_node_challenge() -> str:
    """Random challenge for node key-challenge auth."""
    return secrets.token_hex(32)


def verify_node_signature(public_key_pem: str, challenge: str, signature_hex: str) -> bool:
    """
    Verify that a node signed the challenge with its private key.
    Uses Ed25519 or RSA depending on key type.
    """
    try:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519, padding
        from cryptography.exceptions import InvalidSignature
        import binascii

        signature = binascii.unhexlify(signature_hex)
        challenge_bytes = challenge.encode()

        pub_key = serialization.load_pem_public_key(public_key_pem.encode())

        if isinstance(pub_key, ed25519.Ed25519PublicKey):
            pub_key.verify(signature, challenge_bytes)
        else:
            # RSA
            pub_key.verify(
                signature,
                challenge_bytes,
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        return True
    except (InvalidSignature, Exception):
        return False
