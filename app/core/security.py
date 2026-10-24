from datetime import datetime, timedelta, timezone
import uuid
from typing import Any
import bcrypt
from jose import jwt
from redis.asyncio import Redis

from app.core.config import settings

ALGORITHM = "HS256"
REFRESH_TOKEN_EXPIRE_DAYS = 7


def _safe_password_bytes(password: str) -> bytes:
    pw_bytes = password.encode("utf-8")
    return pw_bytes[:72] if len(pw_bytes) > 72 else pw_bytes


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        pw_bytes = _safe_password_bytes(plain_password)
        hash_bytes = hashed_password.encode("utf-8")
        return bcrypt.checkpw(pw_bytes, hash_bytes)
    except Exception:
        return False


def get_password_hash(password: str) -> str:
    pw_bytes = _safe_password_bytes(password)
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(pw_bytes, salt).decode("utf-8")


def create_token_pair(
    subject: str, role: str, path: str
) -> tuple[str, str, int]:
    """Generates an access token and a refresh token with unique JTIs."""
    now = datetime.now(timezone.utc)
    access_jti = str(uuid.uuid4())
    refresh_jti = str(uuid.uuid4())

    access_exp = now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    refresh_exp = now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

    # 1. Access Token Payload (Custom claims for instant API gateway checks)
    access_payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "path": path,
        "jti": access_jti,
        "type": "access",
        "exp": access_exp,
        "iat": now,
    }

    # 2. Refresh Token Payload
    refresh_payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "path": path,
        "jti": refresh_jti,
        "type": "refresh",
        "exp": refresh_exp,
        "iat": now,
    }

    access_token = jwt.encode(
        access_payload, settings.SECRET_KEY, algorithm=ALGORITHM
    )
    refresh_token = jwt.encode(
        refresh_payload, settings.SECRET_KEY, algorithm=ALGORITHM
    )

    return (
        access_token,
        refresh_token,
        settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


async def blacklist_token(redis: Redis, jti: str, ttl_seconds: int) -> None:
    await redis.set(f"bl_{jti}", "revoked", ex=ttl_seconds)


async def is_token_blacklisted(redis: Redis, jti: str) -> bool:
    val = await redis.get(f"bl_{jti}")
    return val is not None