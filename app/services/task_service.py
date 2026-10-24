import uuid
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.task import Task, TaskDependency, TaskStatus
from app.models.user import RoleEnum, User


async def validate_task_assignment(
    actor: User, assignee_id: uuid.UUID, db: AsyncSession
) -> User:
    """
    Enforces downward-only task delegation:
    - Superadmin can assign to anyone.
    - Any user can assign tasks to themselves.
    - Other roles can only assign to users within their operational subtree:
      assignee.path.startswith(actor.path + str(actor.id) + "/")
    """
    if actor.id == assignee_id:
        return actor

    stmt = select(User).where(User.id == assignee_id, User.is_active.is_(True))
    res = await db.execute(stmt)
    assignee = res.scalar_one_or_none()
    if not assignee:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Target assignee {assignee_id} does not exist or is inactive.",
        )

    if actor.role == RoleEnum.SUPERADMIN:
        return assignee

    required_prefix = f"{actor.path}{actor.id}/"
    if not assignee.path.startswith(required_prefix):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Access Denied: Assignee {assignee.email} is outside your operational "
                f"reporting subtree. Tasks can only be delegated downwards."
            ),
        )

    return assignee


async def detect_cycle_dfs(
    preceding_id: uuid.UUID, dependent_id: uuid.UUID, db: AsyncSession
) -> None:
    """
    Checks if adding edge (preceding_id -> dependent_id) introduces a cycle in the task DAG.
    A cycle exists if there is already a path from dependent_id to preceding_id.
    """
    if preceding_id == dependent_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="A task cannot depend on itself.",
        )

    visited = set()
    stack = [dependent_id]

    while stack:
        curr = stack.pop()
        if curr == preceding_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Cyclic dependency detected: Adding this dependency would create a scheduling deadlock.",
            )
        if curr not in visited:
            visited.add(curr)
            # Find all tasks that depend on curr
            stmt = select(TaskDependency.dependent_id).where(
                TaskDependency.preceding_id == curr
            )
            res = await db.execute(stmt)
            for child_id in res.scalars():
                if child_id not in visited:
                    stack.append(child_id)


async def cascade_schedule_shift(task: Task, db: AsyncSession) -> None:
    """
    Cascades date shifts down the Finish-to-Start dependency chain:
    If preceding task's due_date > dependent task's start_date, shift dependent task.
    """
    if not task.due_date:
        return

    stmt = (
        select(Task)
        .join(TaskDependency, TaskDependency.dependent_id == Task.id)
        .where(TaskDependency.preceding_id == task.id)
    )
    result = await db.execute(stmt)
    dependents = result.scalars().all()

    for dep in dependents:
        if dep.start_date and dep.start_date < task.due_date:
            duration = (
                (dep.due_date - dep.start_date)
                if (dep.due_date and dep.start_date)
                else timedelta(days=1)
            )
            dep.start_date = task.due_date
            dep.due_date = task.due_date + duration
            # Recursively cascade downstream
            await cascade_schedule_shift(dep, db)