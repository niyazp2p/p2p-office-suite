from __future__ import annotations

import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field
from app.models.audit import AuditAction
from app.schemas.user import UserResponse


class PayrollRecordBase(BaseModel):
    base_salary: float = Field(default=0.0, ge=0.0)
    hourly_rate: float = Field(default=0.0, ge=0.0)
    tax_rate_percent: float = Field(default=10.0, ge=0.0, le=100.0)
    health_deduction: float = Field(default=0.0, ge=0.0)
    is_active: bool = True


class PayrollRecordCreateOrUpdate(PayrollRecordBase):
    pass


class PayrollRecordResponse(PayrollRecordBase):
    id: uuid.UUID
    user_id: uuid.UUID
    user: UserResponse | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PayrollCalculateRequest(BaseModel):
    year: int = Field(default=2026, ge=2020, le=2100)
    month: int = Field(default=10, ge=1, le=12)
    bonuses: dict[uuid.UUID, float] = Field(
        default_factory=dict,
        description="Optional one-time bonus mapped to user UUID",
    )


class PayrollItemResponse(BaseModel):
    id: uuid.UUID
    calculation_batch_id: uuid.UUID
    user_id: uuid.UUID
    year: int
    month: int
    approved_hours: float
    base_pay: float
    hourly_pay: float
    bonus: float
    gross_pay: float
    tax_deduction: float
    health_deduction: float
    net_pay: float
    calculated_at: datetime
    user: UserResponse | None = None

    model_config = ConfigDict(from_attributes=True)


class PayrollBatchResponse(BaseModel):
    calculation_batch_id: uuid.UUID
    year: int
    month: int
    total_gross_disbursed: float
    total_net_disbursed: float
    total_tax_withheld: float
    records_processed: int
    items: list[PayrollItemResponse]


class AuditLogResponse(BaseModel):
    id: uuid.UUID
    actor_id: uuid.UUID | None = None
    action: AuditAction
    target_entity: str
    target_id: str | None = None
    details: dict | None = None
    timestamp: datetime
    actor: UserResponse | None = None

    model_config = ConfigDict(from_attributes=True)