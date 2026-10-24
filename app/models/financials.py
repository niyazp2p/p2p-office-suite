import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class PayrollRecord(Base):
    """
    Employee compensation package: base salary, hourly rate, and statutory deductions.
    """
    __tablename__ = "payroll_records"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
        index=True,
    )
    base_salary: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    hourly_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    tax_rate_percent: Mapped[float] = mapped_column(Float, default=10.0, nullable=False)
    health_deduction: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    user = relationship("User", foreign_keys=[user_id], backref="payroll_profile")


class PayrollItem(Base):
    """
    Computed historical payslip ledger for a specific employee, year, and month.
    """
    __tablename__ = "payroll_items"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    calculation_batch_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    month: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    approved_hours: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    base_pay: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    hourly_pay: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    bonus: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    gross_pay: Mapped[float] = mapped_column(Float, nullable=False)
    tax_deduction: Mapped[float] = mapped_column(Float, nullable=False)
    health_deduction: Mapped[float] = mapped_column(Float, nullable=False)
    net_pay: Mapped[float] = mapped_column(Float, nullable=False)

    calculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    user = relationship("User", foreign_keys=[user_id], backref="payroll_items")

    __table_args__ = (
        UniqueConstraint("user_id", "year", "month", name="uq_user_payroll_cycle"),
        Index("idx_payroll_item_cycle", "year", "month"),
    )