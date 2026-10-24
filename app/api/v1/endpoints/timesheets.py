import uuid
from datetime import date
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.task import Task
from app.models.timesheet import Timesheet
from app.models.user import RoleEnum, User
from app.schemas.timesheet import (
    TimesheetCreate,
    TimesheetResponse,
    TimesheetSummaryResponse,
    TimesheetUpdate,
)

router = APIRouter()


@router.get("", response_model=TimesheetSummaryResponse)
async def list_timesheets(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    is_approved: bool | None = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
) -> TimesheetSummaryResponse:
    stmt = (
        select(Timesheet)
        .options(selectinload(Timesheet.user), selectinload(Timesheet.approver))
        .join(User, Timesheet.user_id == User.id)
    )

    # Scoping: Associates/Interns view only their own logs; Managers view reports in subtree
    if current_user.role not in [RoleEnum.SUPERADMIN, RoleEnum.ADMIN, RoleEnum.HR]:
        subtree_prefix = f"{current_user.path}{current_user.id}/"
        stmt = stmt.where(
            (Timesheet.user_id == current_user.id)
            | (User.path.startswith(subtree_prefix))
        )

    if user_id:
        stmt = stmt.where(Timesheet.user_id == user_id)
    if task_id:
        stmt = stmt.where(Timesheet.task_id == task_id)
    if start_date:
        stmt = stmt.where(Timesheet.date >= start_date)
    if end_date:
        stmt = stmt.where(Timesheet.date <= end_date)
    if is_approved is not None:
        stmt = stmt.where(Timesheet.is_approved == is_approved)

    stmt = stmt.order_by(Timesheet.date.desc()).offset(skip).limit(limit)
    res = await db.execute(stmt)
    entries = res.scalars().all()

    total_h = sum(e.hours_spent for e in entries)
    approved_h = sum(e.hours_spent for e in entries if e.is_approved)
    pending_h = total_h - approved_h

    return TimesheetSummaryResponse(
        total_hours=round(total_h, 2),
        approved_hours=round(approved_h, 2),
        pending_hours=round(pending_h, 2),
        entries=entries,
    )


@router.post("", response_model=TimesheetResponse, status_code=status.HTTP_201_CREATED)
async def create_timesheet_entry(
    payload: TimesheetCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TimesheetResponse:
    # Validate Task existence and assignment if task_id passed
    if payload.task_id:
        task_stmt = select(Task).where(Task.id == payload.task_id)
        task_res = await db.execute(task_stmt)
        task = task_res.scalar_one_or_none()
        if not task:
            raise HTTPException(status_code=404, detail="Referenced task not found.")

    entry = Timesheet(
        user_id=current_user.id,
        task_id=payload.task_id,
        date=payload.date,
        hours_spent=payload.hours_spent,
        notes=payload.notes,
        is_approved=False,
    )
    db.add(entry)
    await db.commit()

    stmt = (
        select(Timesheet)
        .options(selectinload(Timesheet.user), selectinload(Timesheet.approver))
        .where(Timesheet.id == entry.id)
    )
    res = await db.execute(stmt)
    return res.scalar_one()


@router.patch("/{entry_id}", response_model=TimesheetResponse)
async def update_timesheet_entry(
    entry_id: uuid.UUID,
    payload: TimesheetUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TimesheetResponse:
    stmt = (
        select(Timesheet)
        .options(selectinload(Timesheet.user), selectinload(Timesheet.approver))
        .where(Timesheet.id == entry_id)
    )
    res = await db.execute(stmt)
    entry = res.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Timesheet record not found.")

    if entry.user_id != current_user.id and current_user.role not in [
        RoleEnum.SUPERADMIN,
        RoleEnum.ADMIN,
    ]:
        raise HTTPException(status_code=403, detail="Cannot edit another user's timesheet.")

    if entry.is_approved:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Locked: Approved timesheets cannot be edited or tampered with.",
        )

    if payload.hours_spent is not None:
        entry.hours_spent = payload.hours_spent
    if payload.notes is not None:
        entry.notes = payload.notes
    if payload.date is not None:
        entry.date = payload.date
    if payload.task_id is not None:
        entry.task_id = payload.task_id

    await db.commit()
    await db.refresh(entry)
    return entry


@router.delete("/{entry_id}", status_code=status.HTTP_200_OK)
async def delete_timesheet_entry(
    entry_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    stmt = select(Timesheet).where(Timesheet.id == entry_id)
    res = await db.execute(stmt)
    entry = res.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Timesheet record not found.")

    if entry.user_id != current_user.id and current_user.role not in [
        RoleEnum.SUPERADMIN,
        RoleEnum.ADMIN,
    ]:
        raise HTTPException(status_code=403, detail="Not authorized to delete this timesheet.")

    if entry.is_approved:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Locked: Approved timesheets cannot be deleted.",
        )

    await db.delete(entry)
    await db.commit()
    return {"detail": "Timesheet entry deleted."}


@router.post("/{entry_id}/approve", response_model=TimesheetResponse)
async def approve_timesheet(
    entry_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TimesheetResponse:
    """
    Supervisor Approval Gate:
    Only direct managers (in the user's reporting path), Team Leads, Admins, or Superadmins
    can approve and lock timesheet records.
    """
    stmt = (
        select(Timesheet)
        .options(selectinload(Timesheet.user), selectinload(Timesheet.approver))
        .where(Timesheet.id == entry_id)
    )
    res = await db.execute(stmt)
    entry = res.scalar_one_or_none()
    if not entry:
        raise HTTPException(status_code=404, detail="Timesheet not found.")

    is_super = current_user.role in [RoleEnum.SUPERADMIN, RoleEnum.ADMIN, RoleEnum.HR]
    is_supervisor = str(current_user.id) in entry.user.path

    if not (is_super or is_supervisor):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access Denied: You do not possess supervisory authority over this employee's reporting line.",
        )

    entry.is_approved = True
    entry.approved_by = current_user.id
    await db.commit()
    await db.refresh(entry)
    return entry