import enum
import uuid
from datetime import date as py_date, datetime, timezone
from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class LoanStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    ACTIVE = "ACTIVE"
    PAID = "PAID"


class EMIStatus(str, enum.Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    SKIPPED = "SKIPPED"


class PayrollRecord(Base):
    """
    Employee baseline compensation terms.
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
    tax_rate_percent: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
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


class EmployeeLoan(Base):
    """
    Employee loan or salary advance tracking.
    """
    __tablename__ = "employee_loans"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    total_amount: Mapped[float] = mapped_column(Float, nullable=False)
    total_installments: Mapped[int] = mapped_column(Integer, nullable=False)
    remaining_balance: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    status: Mapped[LoanStatus] = mapped_column(
        Enum(LoanStatus, name="loan_status_enum"),
        default=LoanStatus.APPROVED,
        nullable=False,
        index=True,
    )

    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    disbursed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

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

    user = relationship("User", foreign_keys=[user_id], backref="loans")
    approver = relationship("User", foreign_keys=[approved_by])
    emis = relationship("LoanEMI", back_populates="loan", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_loans_user_status", "user_id", "status"),
    )


class LoanEMI(Base):
    """
    Individual installment deduction tied to target payroll year/month.
    """
    __tablename__ = "loan_emis"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    loan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("employee_loans.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    installment_number: Mapped[int] = mapped_column(Integer, nullable=False)
    deduction_year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    deduction_month: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    custom_deduction_date: Mapped[py_date | None] = mapped_column(Date, nullable=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)

    status: Mapped[EMIStatus] = mapped_column(
        Enum(EMIStatus, name="emi_status_enum"),
        default=EMIStatus.PENDING,
        nullable=False,
        index=True,
    )
    paid_in_batch_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    loan = relationship("EmployeeLoan", back_populates="emis")

    __table_args__ = (
        Index("idx_emi_schedule", "deduction_year", "deduction_month", "status"),
    )


class PayrollItem(Base):
    """
    Computed payslip: Gross Pay = Base + Hourly + Bonus.
    Net Pay = Gross Pay - EMI Deductions (Zero Tax).
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

    # Loan & Advance Salary Deductions
    loan_deduction: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    tax_deduction: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    health_deduction: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
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