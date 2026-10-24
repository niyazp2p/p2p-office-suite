from __future__ import annotations

import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field
from app.models.hr import ChecklistType, OKRCycle
from app.schemas.user import UserResponse


# Checklist Schemas
class ChecklistStepBase(BaseModel):
    task_title: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    type: ChecklistType


class ChecklistStepCreate(ChecklistStepBase):
    pass


class ChecklistStepUpdate(BaseModel):
    is_completed: bool


class ChecklistStepResponse(ChecklistStepBase):
    id: uuid.UUID
    user_id: uuid.UUID
    is_completed: bool
    completed_at: datetime | None = None
    assigned_by: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ChecklistBatchCreate(BaseModel):
    type: ChecklistType
    steps: list[str] = Field(
        ...,
        min_length=1,
        description="List of step titles (e.g. ['Identity Verification', 'IT Provisioning'])",
    )


# OKR Schemas
class OKRBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    target_value: float = Field(..., gt=0.0)
    current_value: float = Field(default=0.0, ge=0.0)
    unit: str = Field(default="%", max_length=50)
    cycle: OKRCycle = OKRCycle.Q1
    year: int = Field(default=2026, ge=2020, le=2100)


class OKRCreate(OKRBase):
    user_id: uuid.UUID | None = None  # Managers can declare OKRs for subordinates


class OKRUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    current_value: float | None = Field(None, ge=0.0)
    target_value: float | None = Field(None, gt=0.0)
    unit: str | None = None


class OKRResponse(OKRBase):
    id: uuid.UUID
    user_id: uuid.UUID
    user: UserResponse | None = None
    progress_percentage: float = 0.0
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# Performance Review Schemas
class PerformanceReviewBase(BaseModel):
    reviewee_id: uuid.UUID
    cycle: OKRCycle = OKRCycle.ANNUAL
    year: int = Field(default=2026, ge=2020, le=2100)


class PerformanceReviewCreate(PerformanceReviewBase):
    pass


class SelfEvaluationUpdate(BaseModel):
    self_score: float = Field(..., ge=1.0, le=5.0, description="Self score from 1.0 to 5.0")
    self_notes: str = Field(..., min_length=1)


class ManagerEvaluationUpdate(BaseModel):
    manager_score: float = Field(..., ge=1.0, le=5.0, description="Manager score from 1.0 to 5.0")
    feedback: str = Field(..., min_length=1)


class PerformanceReviewResponse(PerformanceReviewBase):
    id: uuid.UUID
    reviewer_id: uuid.UUID
    self_score: float | None = None
    self_notes: str | None = None
    manager_score: float | None = None
    feedback: str | None = None
    is_finalized: bool
    finalized_by: uuid.UUID | None = None
    finalized_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    reviewee: UserResponse | None = None
    reviewer: UserResponse | None = None

    model_config = ConfigDict(from_attributes=True)