import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.user import RoleEnum


class UserBase(BaseModel):
    email: EmailStr
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    role: RoleEnum
    department: str | None = None
    designation: str | None = None
    avatar_url: str | None = None


class UserCreate(UserBase):
    password: str = Field(..., min_length=8)
    manager_id: uuid.UUID | None = None
    # Optional custom employee code override; if omitted, the system generates one
    employee_code: str | None = None


class UserUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    department: str | None = None
    designation: str | None = None
    avatar_url: str | None = None
    is_active: bool | None = None


# --- Password Management Schemas ---


class ChangePasswordRequest(BaseModel):
    """Authenticated user changing password with current credentials."""

    old_password: str
    new_password: str = Field(..., min_length=8)


class ResetPasswordWithCodeRequest(BaseModel):
    """Self-service password recovery using the unique employee code."""

    email: EmailStr
    employee_code: str = Field(..., min_length=3, max_length=50)
    new_password: str = Field(..., min_length=8)


class AdminResetPasswordRequest(BaseModel):
    """Supervisor overriding a subordinate password."""

    new_password: str = Field(..., min_length=8)


class TransferManagerRequest(BaseModel):
    new_manager_id: uuid.UUID


class PermittedRolesResponse(BaseModel):
    current_role: RoleEnum
    permitted_to_create: list[RoleEnum]


class UserResponse(UserBase):
    id: uuid.UUID
    employee_code: str
    manager_id: uuid.UUID | None
    path: str
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class OrgTreeNodeResponse(BaseModel):
    id: str
    employee_code: str
    name: str
    role: RoleEnum
    department: str | None
    designation: str | None
    children: list["OrgTreeNodeResponse"] = []

    model_config = ConfigDict(from_attributes=True)