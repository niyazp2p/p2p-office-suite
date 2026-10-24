import uuid
from datetime import date, datetime
from pydantic import BaseModel, ConfigDict, Field
from app.models.task import Priority, TaskStatus
from app.schemas.user import UserResponse


class TaskBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    priority: Priority = Priority.MEDIUM
    start_date: datetime | None = None
    due_date: datetime | None = None
    estimated_hours: float = Field(default=0.0, ge=0.0)


class TaskCreate(TaskBase):
    assignee_id: uuid.UUID
    status: TaskStatus = TaskStatus.TODO
    preceding_task_ids: list[uuid.UUID] = []


class TaskUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    priority: Priority | None = None
    assignee_id: uuid.UUID | None = None
    start_date: datetime | None = None
    due_date: datetime | None = None
    estimated_hours: float | None = Field(None, ge=0.0)


class TaskStatusTransition(BaseModel):
    status: TaskStatus


class TaskDependencyCreate(BaseModel):
    preceding_id: uuid.UUID


class TaskResponse(TaskBase):
    id: uuid.UUID
    status: TaskStatus
    creator_id: uuid.UUID
    assignee_id: uuid.UUID
    creator: UserResponse | None = None
    assignee: UserResponse | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TaskDetailResponse(TaskResponse):
    preceding_task_ids: list[uuid.UUID] = []
    dependent_task_ids: list[uuid.UUID] = []


# Kanban View Schema
class KanbanColumn(BaseModel):
    status: TaskStatus
    count: int
    tasks: list[TaskResponse]


class KanbanBoardResponse(BaseModel):
    columns: dict[TaskStatus, KanbanColumn]


# Gantt Chart (dhtmlx-gantt compatible)
class GanttLink(BaseModel):
    id: str
    source: str  # preceding task ID
    target: str  # dependent task ID
    type: str = "0"  # Finish-to-Start


class GanttTaskItem(BaseModel):
    id: str
    text: str
    start_date: datetime | None
    end_date: datetime | None
    duration: float  # In days or hours
    progress: float
    priority: Priority
    status: TaskStatus
    assignee_name: str


class GanttResponse(BaseModel):
    data: list[GanttTaskItem]
    links: list[GanttLink]


# Workload Capacity Schema
class DailyHourAllocation(BaseModel):
    date: date
    hours: float


class UserWorkload(BaseModel):
    user_id: uuid.UUID
    user_name: str
    department: str | None
    daily_capacity_limit: float = 8.0
    total_allocated_hours: float
    is_overallocated: bool
    daily_breakdown: list[DailyHourAllocation]


class WorkloadResponse(BaseModel):
    horizon_days: int
    users: list[UserWorkload]