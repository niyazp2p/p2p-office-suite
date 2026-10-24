import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field
from app.schemas.user import UserResponse


class CalendarEventBase(BaseModel):
    title: str = Field(..., min_length=1, max_length=255)
    description: str | None = None
    start_time: datetime
    end_time: datetime
    is_meeting: bool = False


class CalendarEventCreate(CalendarEventBase):
    user_id: uuid.UUID | None = None  # Admin/TL can schedule meetings for subordinates


class CalendarEventUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    is_meeting: bool | None = None


class CalendarEventResponse(CalendarEventBase):
    id: uuid.UUID
    user_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    user: UserResponse | None = None

    model_config = ConfigDict(from_attributes=True)


# Aggregated view combining Calendar Events and Task spans
class AggregatedCalendarItem(BaseModel):
    id: str
    title: str
    start: datetime
    end: datetime
    item_type: str  # "EVENT", "MEETING", or "TASK_DEADLINE"
    status_or_priority: str | None = None
    assignee_name: str | None = None
    is_all_day: bool = False