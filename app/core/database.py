from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base

from app.core.config import settings

# Configure asyncpg connection arguments for SSL and Neon PgBouncer
connect_args = {
    "ssl": "require",
}

if "-pooler" in settings.DATABASE_ASYNC_URL:
    connect_args["prepared_statement_cache_size"] = 0
    connect_args["statement_cache_size"] = 0

async_engine = create_async_engine(
    settings.DATABASE_ASYNC_URL,
    echo=False,
    pool_size=10,
    max_overflow=15,
    pool_timeout=30,
    pool_recycle=300,  # Recycle every 5 minutes for Neon auto-suspend safety
    pool_pre_ping=True,  # Discards stale or dropped sockets before execution
    connect_args=connect_args,
)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
)

Base = declarative_base()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency provider for FastAPI route handlers."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()