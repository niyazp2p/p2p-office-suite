import uuid
from datetime import datetime, timezone
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.calendar import CalendarEvent
from app.models.task import Task
from app.models.user import RoleEnum, User
from app.schemas.calendar import (
    AggregatedCalendarItem,
    CalendarEventCreate,
    CalendarEventResponse,
    CalendarEventUpdate,
)

router = APIRouter()


@router.get("/events", response_model=list[AggregatedCalendarItem])
async def get_aggregated_calendar(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    start_time: datetime = Query(...),
    end_time: datetime = Query(...),
) -> list[AggregatedCalendarItem]:
    """
    Returns unified calendar events:
    1. Custom CalendarEvents (meetings, milestone blocks).
    2. Scheduled Tasks (startDate to dueDate spans).
    """
    items: list[AggregatedCalendarItem] = []

    # 1. Fetch CalendarEvents
    event_stmt = (
        select(CalendarEvent)
        .options(selectinload(CalendarEvent.user))
        .where(
            CalendarEvent.start_time <= end_time,
            CalendarEvent.end_time >= start_time,
        )
    )
    if current_user.role not in [RoleEnum.SUPERADMIN, RoleEnum.ADMIN]:
        subtree_prefix = f"{current_user.path}{current_user.id}/"
        event_stmt = event_stmt.join(User, CalendarEvent.user_id == User.id).where(
            (CalendarEvent.user_id == current_user.id)
            | (User.path.startswith(subtree_prefix))
        )

    event_res = await db.execute(event_stmt)
    for ev in event_res.scalars().all():
        items.append(
            AggregatedCalendarItem(
                id=str(ev.id),
                title=ev.title,
                start=ev.start_time,
                end=ev.end_time,
                item_type="MEETING" if ev.is_meeting else "EVENT",
                assignee_name=f"{ev.user.first_name} {ev.user.last_name}",
            )
        )

    # 2. Fetch Tasks with active scheduling
    task_stmt = (
        select(Task)
        .options(selectinload(Task.assignee))
        .where(
            Task.start_date.is_not(None),
            Task.due_date.is_not(None),
            Task.start_date <= end_time,
            Task.due_date >= start_time,
        )
    )
    if current_user.role not in [RoleEnum.SUPERADMIN, RoleEnum.ADMIN]:
        subtree_prefix = f"{current_user.path}{current_user.id}/"
        task_stmt = task_stmt.join(User, Task.assignee_id == User.id).where(
            (Task.creator_id == current_user.id)
            | (Task.assignee_id == current_user.id)
            | (User.path.startswith(subtree_prefix))
        )

    task_res = await db.execute(task_stmt)
    for t in task_res.scalars().all():
        items.append(
            AggregatedCalendarItem(
                id=str(t.id),
                title=f"Task: {t.title}",
                start=t.start_date,
                end=t.due_date,
                item_type="TASK_DEADLINE",
                status_or_priority=f"{t.status.value} | {t.priority.value}",
                assignee_name=f"{t.assignee.first_name} {t.assignee.last_name}",
            )
        )

    return sorted(items, key=lambda x: x.start)


@router.post("/events", response_model=CalendarEventResponse, status_code=status.HTTP_201_CREATED)
async def create_calendar_event(
    payload: CalendarEventCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CalendarEventResponse:
    target_user_id = payload.user_id or current_user.id

    if target_user_id != current_user.id and current_user.role not in [
        RoleEnum.SUPERADMIN,
        RoleEnum.ADMIN,
        RoleEnum.TEAM_LEAD,
    ]:
        raise HTTPException(
            status_code=403,
            detail="Cannot schedule calendar events on behalf of other team members.",
        )

    event = CalendarEvent(
        user_id=target_user_id,
        title=payload.title,
        description=payload.description,
        start_time=payload.start_time,
        end_time=payload.end_time,
        is_meeting=payload.is_meeting,
    )
    db.add(event)
    await db.commit()

    stmt = (
        select(CalendarEvent)
        .options(selectinload(CalendarEvent.user))
        .where(CalendarEvent.id == event.id)
    )
    res = await db.execute(stmt)
    return res.scalar_one()


@router.delete("/events/{event_id}", status_code=status.HTTP_200_OK)
async def delete_calendar_event(
    event_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    stmt = select(CalendarEvent).where(CalendarEvent.id == event_id)
    res = await db.execute(stmt)
    event = res.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found.")

    if event.user_id != current_user.id and current_user.role not in [
        RoleEnum.SUPERADMIN,
        RoleEnum.ADMIN,
    ]:
        raise HTTPException(status_code=403, detail="Not authorized to delete this event.")

    await db.delete(event)
    await db.commit()
    return {"detail": "Calendar event removed."}