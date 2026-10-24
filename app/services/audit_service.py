import uuid
from typing import Any
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.audit import AuditAction, AuditLog


async def log_audit_event(
    db: AsyncSession,
    actor_id: uuid.UUID | None,
    action: AuditAction,
    target_entity: str,
    target_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    entry = AuditLog(
        actor_id=actor_id,
        action=action,
        target_entity=target_entity,
        target_id=target_id,
        details=details or {},
    )
    db.add(entry)
    await db.flush()
    return entry