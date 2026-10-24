import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.task import Priority, Task, TaskDependency, TaskStatus
from app.models.user import RoleEnum, User
from app.schemas.task import (
    DailyHourAllocation,
    GanttLink,
    GanttResponse,
    GanttTaskItem,
    KanbanBoardResponse,
    KanbanColumn,
    TaskCreate,
    TaskDependencyCreate,
    TaskDetailResponse,
    TaskResponse,
    TaskStatusTransition,
    TaskUpdate,
    UserWorkload,
    WorkloadResponse,
)
from app.services.task_service import (
    cascade_schedule_shift,
    detect_cycle_dfs,
    validate_task_assignment,
)

router = APIRouter()


# -----------------------------------------------------------------------------
# 1. High-Performance Tabular List View
# -----------------------------------------------------------------------------
@router.get("", response_model=list[TaskResponse])
async def list_tasks(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    status_filter: TaskStatus | None = Query(None, alias="status"),
    priority_filter: Priority | None = Query(None, alias="priority"),
    assignee_id: uuid.UUID | None = None,
    due_before: datetime | None = None,
    due_after: datetime | None = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
) -> list[TaskResponse]:
    """
    Paginated, multi-filtered task table.
    Enforces visibility: Superadmin/Admin see all; others see tasks they created, 
    tasks assigned to them, or tasks assigned to their downstream subtree.
    """
    stmt = (
        select(Task)
        .options(selectinload(Task.creator), selectinload(Task.assignee))
        .join(User, Task.assignee_id == User.id)
    )

    # Hierarchy scoping
    if current_user.role not in [RoleEnum.SUPERADMIN, RoleEnum.ADMIN]:
        subtree_prefix = f"{current_user.path}{current_user.id}/"
        stmt = stmt.where(
            (Task.creator_id == current_user.id)
            | (Task.assignee_id == current_user.id)
            | (User.path.startswith(subtree_prefix))
        )

    if status_filter:
        stmt = stmt.where(Task.status == status_filter)
    if priority_filter:
        stmt = stmt.where(Task.priority == priority_filter)
    if assignee_id:
        stmt = stmt.where(Task.assignee_id == assignee_id)
    if due_before:
        stmt = stmt.where(Task.due_date <= due_before)
    if due_after:
        stmt = stmt.where(Task.due_date >= due_after)

    stmt = stmt.order_by(Task.due_date.asc().nulls_last()).offset(skip).limit(limit)
    result = await db.execute(stmt)
    return result.scalars().all()


# -----------------------------------------------------------------------------
# 2. Task Creation with Subtree Enforcement & Dependencies
# -----------------------------------------------------------------------------
@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task(
    payload: TaskCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskResponse:
    # 1. Enforce downward assignment rule
    await validate_task_assignment(current_user, payload.assignee_id, db)

    task = Task(
        title=payload.title,
        description=payload.description,
        status=payload.status,
        priority=payload.priority,
        start_date=payload.start_date,
        due_date=payload.due_date,
        estimated_hours=payload.estimated_hours,
        creator_id=current_user.id,
        assignee_id=payload.assignee_id,
    )
    db.add(task)
    await db.flush()

    # 2. Attach preceding task dependencies with cycle validation
    for prec_id in payload.preceding_task_ids:
        await detect_cycle_dfs(prec_id, task.id, db)
        dep = TaskDependency(preceding_id=prec_id, dependent_id=task.id)
        db.add(dep)

    await db.commit()

    # Re-fetch with relationships
    stmt = (
        select(Task)
        .options(selectinload(Task.creator), selectinload(Task.assignee))
        .where(Task.id == task.id)
    )
    res = await db.execute(stmt)
    return res.scalar_one()


# -----------------------------------------------------------------------------
# 3. Kanban State Machine View & Drag-and-Drop Transition
# -----------------------------------------------------------------------------
@router.get("/kanban", response_model=KanbanBoardResponse)
async def get_kanban_board(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> KanbanBoardResponse:
    """Returns tasks grouped into TODO, IN_PROGRESS, IN_REVIEW, DONE, and BLOCKED columns."""
    stmt = (
        select(Task)
        .options(selectinload(Task.creator), selectinload(Task.assignee))
        .join(User, Task.assignee_id == User.id)
    )
    if current_user.role not in [RoleEnum.SUPERADMIN, RoleEnum.ADMIN]:
        subtree_prefix = f"{current_user.path}{current_user.id}/"
        stmt = stmt.where(
            (Task.creator_id == current_user.id)
            | (Task.assignee_id == current_user.id)
            | (User.path.startswith(subtree_prefix))
        )

    res = await db.execute(stmt)
    all_tasks = res.scalars().all()

    board: dict[TaskStatus, KanbanColumn] = {
        s: KanbanColumn(status=s, count=0, tasks=[]) for s in TaskStatus
    }
    for t in all_tasks:
        board[t.status].tasks.append(t)
        board[t.status].count += 1

    return KanbanBoardResponse(columns=board)


@router.patch("/{task_id}/status", response_model=TaskResponse)
async def update_task_status(
    task_id: uuid.UUID,
    payload: TaskStatusTransition,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskResponse:
    """
    Kanban drag-and-drop state transition.
    Security Gate: Transitioning into DONE requires creator or manager sign-off.
    """
    stmt = (
        select(Task)
        .options(selectinload(Task.creator), selectinload(Task.assignee))
        .where(Task.id == task_id)
    )
    res = await db.execute(stmt)
    task = res.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")

    # Guard: Only creator, manager (in path), or superadmin can approve DONE
    if payload.status == TaskStatus.DONE:
        is_creator = task.creator_id == current_user.id
        is_super = current_user.role in [RoleEnum.SUPERADMIN, RoleEnum.ADMIN]
        is_manager = str(current_user.id) in task.assignee.path
        if not (is_creator or is_super or is_manager):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only the task creator or direct supervisor can sign off and transition a task to DONE.",
            )

    task.status = payload.status
    await db.commit()
    await db.refresh(task)
    return task


# -----------------------------------------------------------------------------
# 4. Gantt & Timeline Engine
# -----------------------------------------------------------------------------
@router.get("/gantt", response_model=GanttResponse)
async def get_gantt_data(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GanttResponse:
    """Returns nodes and dependency edges formatted for Gantt chart components."""
    stmt = (
        select(Task)
        .options(selectinload(Task.assignee))
        .order_by(Task.start_date.asc().nulls_last())
    )
    res = await db.execute(stmt)
    tasks = res.scalars().all()

    gantt_items: list[GanttTaskItem] = []
    for t in tasks:
        duration = 1.0
        if t.start_date and t.due_date:
            duration = max(1.0, (t.due_date - t.start_date).total_seconds() / 86400)
        progress = 1.0 if t.status == TaskStatus.DONE else (0.5 if t.status == TaskStatus.IN_PROGRESS else 0.0)

        gantt_items.append(
            GanttTaskItem(
                id=str(t.id),
                text=t.title,
                start_date=t.start_date,
                end_date=t.due_date,
                duration=duration,
                progress=progress,
                priority=t.priority,
                status=t.status,
                assignee_name=f"{t.assignee.first_name} {t.assignee.last_name}",
            )
        )

    # Fetch dependency links
    dep_stmt = select(TaskDependency)
    dep_res = await db.execute(dep_stmt)
    links = [
        GanttLink(id=str(d.id), source=str(d.preceding_id), target=str(d.dependent_id))
        for d in dep_res.scalars().all()
    ]

    return GanttResponse(data=gantt_items, links=links)


@router.post("/{task_id}/dependencies", status_code=status.HTTP_201_CREATED)
async def add_task_dependency(
    task_id: uuid.UUID,
    payload: TaskDependencyCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    """Adds a Finish-to-Start dependency edge with cycle detection."""
    await detect_cycle_dfs(payload.preceding_id, task_id, db)
    dep = TaskDependency(preceding_id=payload.preceding_id, dependent_id=task_id)
    db.add(dep)
    await db.commit()
    return {"detail": "Dependency linked successfully."}


@router.delete("/{task_id}/dependencies/{preceding_id}", status_code=status.HTTP_200_OK)
async def remove_task_dependency(
    task_id: uuid.UUID,
    preceding_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    """Removes a dependency constraint."""
    stmt = delete(TaskDependency).where(
        TaskDependency.dependent_id == task_id,
        TaskDependency.preceding_id == preceding_id,
    )
    await db.execute(stmt)
    await db.commit()
    return {"detail": "Dependency removed."}


# -----------------------------------------------------------------------------
# 5. Workload Capacity Analyzer
# -----------------------------------------------------------------------------
@router.get("/workload", response_model=WorkloadResponse)
async def get_workload_capacity(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    horizon_days: int = Query(14, ge=7, le=30),
) -> WorkloadResponse:
    """
    Aggregates assigned task hours per user over the selected window (7, 14, or 30 days).
    Flags overallocation if average or daily hours exceed 8 hours/day.
    """
    today = datetime.now(timezone.utc).date()
    end_date = today + timedelta(days=horizon_days)

    # Scoped team members
    users_stmt = select(User).where(User.is_active.is_(True))
    if current_user.role not in [RoleEnum.SUPERADMIN, RoleEnum.ADMIN]:
        subtree_prefix = f"{current_user.path}{current_user.id}/"
        users_stmt = users_stmt.where(
            (User.id == current_user.id) | (User.path.startswith(subtree_prefix))
        )
    users_res = await db.execute(users_stmt)
    users = users_res.scalars().all()

    workload_list: list[UserWorkload] = []

    for u in users:
        # Fetch active tasks
        task_stmt = select(Task).where(
            Task.assignee_id == u.id,
            Task.status.in_([TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.IN_REVIEW]),
            Task.start_date.is_not(None),
            Task.due_date.is_not(None),
        )
        task_res = await db.execute(task_stmt)
        user_tasks = task_res.scalars().all()

        daily_map = {today + timedelta(days=i): 0.0 for i in range(horizon_days)}

        for t in user_tasks:
            t_start = t.start_date.date()
            t_due = t.due_date.date()
            task_days = max(1, (t_due - t_start).days + 1)
            daily_rate = t.estimated_hours / task_days

            for i in range(horizon_days):
                curr_day = today + timedelta(days=i)
                if t_start <= curr_day <= t_due:
                    daily_map[curr_day] += daily_rate

        total_hours = sum(daily_map.values())
        is_over = any(h > 8.0 for h in daily_map.values())

        workload_list.append(
            UserWorkload(
                user_id=u.id,
                user_name=f"{u.first_name} {u.last_name}",
                department=u.department,
                total_allocated_hours=round(total_hours, 2),
                is_overallocated=is_over,
                daily_breakdown=[
                    DailyHourAllocation(date=d, hours=round(h, 2))
                    for d, h in daily_map.items()
                ],
            )
        )

    return WorkloadResponse(horizon_days=horizon_days, users=workload_list)


# -----------------------------------------------------------------------------
# 6. Single Task Operations (GET, PATCH, DELETE)
# -----------------------------------------------------------------------------
@router.get("/{task_id}", response_model=TaskDetailResponse)
async def get_task(
    task_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskDetailResponse:
    stmt = (
        select(Task)
        .options(selectinload(Task.creator), selectinload(Task.assignee))
        .where(Task.id == task_id)
    )
    res = await db.execute(stmt)
    task = res.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")

    # Fetch preceding and dependent IDs
    pre_stmt = select(TaskDependency.preceding_id).where(TaskDependency.dependent_id == task_id)
    dep_stmt = select(TaskDependency.dependent_id).where(TaskDependency.preceding_id == task_id)
    pre_ids = (await db.execute(pre_stmt)).scalars().all()
    dep_ids = (await db.execute(dep_stmt)).scalars().all()

    resp = TaskDetailResponse.model_validate(task)
    resp.preceding_task_ids = list(pre_ids)
    resp.dependent_task_ids = list(dep_ids)
    return resp


@router.patch("/{task_id}", response_model=TaskResponse)
async def update_task(
    task_id: uuid.UUID,
    payload: TaskUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TaskResponse:
    stmt = (
        select(Task)
        .options(selectinload(Task.creator), selectinload(Task.assignee))
        .where(Task.id == task_id)
    )
    res = await db.execute(stmt)
    task = res.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")

    if payload.assignee_id and payload.assignee_id != task.assignee_id:
        await validate_task_assignment(current_user, payload.assignee_id, db)
        task.assignee_id = payload.assignee_id

    if payload.title is not None:
        task.title = payload.title
    if payload.description is not None:
        task.description = payload.description
    if payload.priority is not None:
        task.priority = payload.priority
    if payload.estimated_hours is not None:
        task.estimated_hours = payload.estimated_hours
    if payload.start_date is not None:
        task.start_date = payload.start_date
    if payload.due_date is not None:
        task.due_date = payload.due_date
        # Cascade schedule updates to dependent tasks
        await cascade_schedule_shift(task, db)

    await db.commit()
    await db.refresh(task)
    return task


@router.delete("/{task_id}", status_code=status.HTTP_200_OK)
async def delete_task(
    task_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    stmt = select(Task).where(Task.id == task_id)
    res = await db.execute(stmt)
    task = res.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found.")

    # Only creator, assigned supervisor, or superadmin can delete
    is_creator = task.creator_id == current_user.id
    is_super = current_user.role in [RoleEnum.SUPERADMIN, RoleEnum.ADMIN]
    if not (is_creator or is_super):
        raise HTTPException(status_code=403, detail="Not authorized to delete this task.")

    await db.delete(task)
    await db.commit()
    return {"detail": "Task deleted successfully."}