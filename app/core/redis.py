from collections.abc import AsyncGenerator
from redis import asyncio as aioredis
from app.core.config import settings

# Create connection pool for Redis
redis_pool = aioredis.ConnectionPool.from_url(
    settings.REDIS_URL,
    decode_responses=True,
    max_connections=50,
)


def get_redis_client() -> aioredis.Redis:
    """Returns a standalone redis client from the shared pool."""
    return aioredis.Redis(connection_pool=redis_pool)


async def get_redis() -> AsyncGenerator[aioredis.Redis, None]:
    """FastAPI dependency for injecting Redis client into route handlers."""
    client = get_redis_client()
    try:
        yield client
    finally:
        await client.aclose()