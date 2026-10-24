import enum
import uuid
from datetime import datetime, timezone
from sqlalchemy import DateTime, Enum, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class AuditAction(str, enum.Enum):
    USER_CREATED = "USER_CREATED"
    USER_DEACTIVATED = "USER_DEACTIVATED"
    ROLE_PROVISIONED = "ROLE_PROVISIONED"
    BRANCH_REPARENTED = "BRANCH_REPARENTED"
    PAYROLL_RECORD_UPDATED = "PAYROLL_RECORD_UPDATED"
    PAYROLL_BATCH_CALCULATED = "PAYROLL_BATCH_CALCULATED"
    TIMESHEET_APPROVED = "TIMESHEET_APPROVED"
    REVIEW_FINALIZED = "REVIEW_FINALIZED"


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    action: Mapped[AuditAction] = mapped_column(
        Enum(AuditAction, name="audit_action_enum"),
        nullable=False,
        index=True,
    )
    target_entity: Mapped[str] = mapped_column(String(100), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
        index=True,
    )

    actor = relationship("User", foreign_keys=[actor_id])

    __table_args__ = (
        Index("idx_audit_logs_actor_action", "actor_id", "action"),
    )