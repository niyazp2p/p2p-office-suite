from __future__ import annotations

import uuid
from datetime import date, datetime
from pydantic import BaseModel, ConfigDict
from app.models.task import Priority, TaskStatus
from app.models.user import RoleEnum


# -----------------------------------------------------------------------------
# Micro Widgets (Reusable blocks across dashboards)
# -----------------------------------------------------------------------------
class TaskMiniCard(BaseModel):
    id: uuid.UUID
    title: str
    status: TaskStatus
    priority: Priority
    due_date: datetime | None
    estimated_hours: float
    assignee_name: str | None = None

    model_config = ConfigDict(from_attributes=True)


class UpcomingEventMini(BaseModel):
    id: str
    title: str
    start: datetime
    end: datetime
    type: str  # "MEETING" or "TASK_DEADLINE"


class OverallocatedMemberMini(BaseModel):
    user_id: uuid.UUID
    name: str
    peak_hours_day: float


# -----------------------------------------------------------------------------
# 1. Contributor Dashboard ("My Day" for Associate & Intern)
# -----------------------------------------------------------------------------
class ContributorDashboard(BaseModel):
    # Task KPIs
    pending_tasks_count: int
    in_progress_tasks_count: int
    completed_tasks_count: int
    overdue_tasks_count: int

    # Timesheet summary (Current month)
    monthly_approved_hours: float
    monthly_pending_hours: float

    # Checklists (e.g. Intern Onboarding progression)
    active_checklist_completed_steps: int
    active_checklist_total_steps: int

    # Active OKRs
    active_okrs_count: int
    average_okr_progress: float

    # Immediate schedule / focus tasks
    priority_tasks: list[TaskMiniCard]
    today_schedule: list[UpcomingEventMini]


# -----------------------------------------------------------------------------
# 2. Team Lead / Operator Dashboard (Subtree Operational Cockpit)
# -----------------------------------------------------------------------------
class TeamLeadDashboard(BaseModel):
    # Hierarchy bounds
    team_members_count: int
    direct_reports_count: int

    # Task velocity across downstream team
    team_active_tasks_count: int
    team_in_review_tasks_count: int  # Tasks awaiting lead inspection
    team_blocked_tasks_count: int

    # Timesheets waiting for Team Lead sign-off
    pending_timesheets_to_approve_count: int

    # Workload alerts
    overallocated_members: list[OverallocatedMemberMini]

    # Team Action Queues
    review_queue: list[TaskMiniCard]
    urgent_team_tasks: list[TaskMiniCard]


# -----------------------------------------------------------------------------
# 3. Executive & HR Dashboard (Company-wide Overview)
# -----------------------------------------------------------------------------
class ExecutiveHRDashboard(BaseModel):
    # Headcount & Staffing
    total_company_headcount: int
    active_employees: int
    deactivated_employees: int

    # HR Talent Lifecycle
    open_onboardings_count: int
    open_offboardings_count: int
    pending_performance_reviews_count: int
    finalized_performance_reviews_count: int

    # Financial / Payroll Overview
    current_month_gross_payroll: float
    current_month_net_payroll: float
    total_tax_withheld: float

    # System-wide Task Pulse
    total_company_tasks: int
    company_blocked_tasks: int


# -----------------------------------------------------------------------------
# Master Dynamic Response Envelope
# -----------------------------------------------------------------------------
class MasterDashboardResponse(BaseModel):
    user_id: uuid.UUID
    role: RoleEnum
    employee_code: str
    user_name: str
    view_type: str  # "CONTRIBUTOR", "SUPERVISOR", or "EXECUTIVE"

    # Exactly one populated based on role
    contributor_view: ContributorDashboard | None = None
    supervisor_view: TeamLeadDashboard | None = None
    executive_view: ExecutiveHRDashboard | None = None