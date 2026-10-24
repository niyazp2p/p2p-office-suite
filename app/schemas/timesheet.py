from __future__ import annotations

import uuid
from datetime import date as py_date
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field
from app.schemas.user import UserResponse


class TimesheetBase(BaseModel):
    task_id: uuid.UUID | None = None
    date: py_date
    hours_spent: float = Field(..., gt=0.0, le=24.0, description="Hours logged (0 to 24)")
    notes: str | None = None


class TimesheetCreate(TimesheetBase):
    pass


class TimesheetUpdate(BaseModel):
    hours_spent: float | None = Field(None, gt=0.0, le=24.0)
    notes: str | None = None
    date: py_date | None = None
    task_id: uuid.UUID | None = None


class TimesheetResponse(TimesheetBase):
    id: uuid.UUID
    user_id: uuid.UUID
    is_approved: bool
    approved_by: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime
    user: UserResponse | None = None
    approver: UserResponse | None = None

    model_config = ConfigDict(from_attributes=True)


class TimesheetSummaryResponse(BaseModel):
    total_hours: float
    approved_hours: float
    pending_hours: float
    entries: list[TimesheetResponse]