import asyncio
from typing import Annotated
from fastapi import APIRouter, Depends, Response, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis

router = APIRouter()


@router.get("/liveness", status_code=status.HTTP_200_OK)
async def liveness() -> dict[str, str]:
    """Returns 200 if the web process is running."""
    return {"status": "ok"}


@router.get("/readiness")
async def readiness(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> dict[str, str]:
    """
    Validates live connections to PostgreSQL and Redis.
    Returns 200 if both are healthy, or 503 if any dependency fails.
    """
    db_status = "connected"
    redis_status = "connected"

    # 1. Probe PostgreSQL with timeout
    try:
        await asyncio.wait_for(db.execute(text("SELECT 1")), timeout=3.0)
    except Exception as exc:
        db_status = f"unhealthy: {type(exc).__name__}"

    # 2. Probe Redis with timeout
    try:
        await asyncio.wait_for(redis.ping(), timeout=3.0)
    except Exception as exc:
        redis_status = f"unhealthy: {type(exc).__name__}"

    is_ready = db_status == "connected" and redis_status == "connected"

    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status": "ready" if is_ready else "degraded",
        "database": db_status,
        "redis": redis_status,
    }