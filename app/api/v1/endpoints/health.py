from typing import Annotated
from fastapi import APIRouter, Depends, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis

router = APIRouter()


@router.get("/liveness", status_code=status.HTTP_200_OK)
async def liveness() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readiness", status_code=status.HTTP_200_OK)
async def readiness(
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> dict[str, str]:
    # Test DB
    await db.execute(text("SELECT 1"))
    # Test Redis
    await redis.ping()
    return {"status": "ready", "database": "connected", "redis": "connected"}