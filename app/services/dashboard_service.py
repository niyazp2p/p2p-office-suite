import calendar
import uuid
from datetime import date, datetime, timedelta, timezone
from sqlalchemy import extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.calendar import CalendarEvent
from app.models.financials import PayrollItem
from app.models.hr import ChecklistType, OKR, PerformanceReview, UserChecklist
from app.models.task import Priority, Task, TaskStatus
from app.models.timesheet import Timesheet
from app.models.user import RoleEnum, User
from app.schemas.dashboard import (
    ContributorDashboard,
    ExecutiveHRDashboard,
    MasterDashboardResponse,
    OverallocatedMemberMini,
    TaskMiniCard,
    TeamLeadDashboard,
    UpcomingEventMini,
)


async def build_contributor_dashboard(
    user: User, db: AsyncSession
) -> ContributorDashboard:
    now = datetime.now(timezone.utc)
    today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    today_end = today_start + timedelta(days=1)
    month_start = date(now.year, now.month, 1)

    # 1. Tasks
    task_res = await db.execute(
        select(
            func.count().filter(Task.status == TaskStatus.TODO),
            func.count().filter(Task.status == TaskStatus.IN_PROGRESS),
            func.count().filter(Task.status == TaskStatus.DONE),
            func.count().filter(
                Task.due_date < now,
                Task.status.in_([TaskStatus.TODO, TaskStatus.IN_PROGRESS]),
            ),
        ).where(Task.assignee_id == user.id)
    )
    todo_cnt, in_prog_cnt, done_cnt, overdue_cnt = task_res.one()

    # Priority tasks (Top 5 urgent or in-progress)
    priority_tasks_stmt = (
        select(Task)
        .where(
            Task.assignee_id == user.id,
            Task.status.in_([TaskStatus.TODO, TaskStatus.IN_PROGRESS]),
        )
        .order_by(Task.due_date.asc().nulls_last())
        .limit(5)
    )
    p_tasks = (await db.execute(priority_tasks_stmt)).scalars().all()
    priority_cards = [
        TaskMiniCard(
            id=t.id,
            title=t.title,
            status=t.status,
            priority=t.priority,
            due_date=t.due_date,
            estimated_hours=t.estimated_hours,
            assignee_name=f"{user.first_name} {user.last_name}",
        )
        for t in p_tasks
    ]

    # 2. Timesheets
    ts_res = await db.execute(
        select(
            func.coalesce(
                func.sum(Timesheet.hours_spent).filter(Timesheet.is_approved.is_(True)),
                0.0,
            ),
            func.coalesce(
                func.sum(Timesheet.hours_spent).filter(Timesheet.is_approved.is_(False)),
                0.0,
            ),
        ).where(Timesheet.user_id == user.id, Timesheet.date >= month_start)
    )
    app_h, pend_h = ts_res.one()

    # 3. Checklists
    cl_res = await db.execute(
        select(
            func.count().filter(UserChecklist.is_completed.is_(True)),
            func.count(),
        ).where(UserChecklist.user_id == user.id)
    )
    cl_done, cl_total = cl_res.one()

    # 4. OKRs
    okr_res = await db.execute(
        select(OKR).where(OKR.user_id == user.id)
    )
    okrs = okr_res.scalars().all()
    avg_okr = (
        round(
            sum(
                min(100.0, (o.current_value / o.target_value) * 100.0)
                for o in okrs
                if o.target_value > 0
            )
            / len(okrs),
            1,
        )
        if okrs
        else 0.0
    )

    # 5. Today's Events
    cal_res = await db.execute(
        select(CalendarEvent).where(
            CalendarEvent.user_id == user.id,
            CalendarEvent.start_time <= today_end,
            CalendarEvent.end_time >= today_start,
        )
    )
    schedule = [
        UpcomingEventMini(
            id=str(e.id),
            title=e.title,
            start=e.start_time,
            end=e.end_time,
            type="MEETING" if e.is_meeting else "EVENT",
        )
        for e in cal_res.scalars().all()
    ]

    return ContributorDashboard(
        pending_tasks_count=todo_cnt,
        in_progress_tasks_count=in_prog_cnt,
        completed_tasks_count=done_cnt,
        overdue_tasks_count=overdue_cnt,
        monthly_approved_hours=round(float(app_h), 1),
        monthly_pending_hours=round(float(pend_h), 1),
        active_checklist_completed_steps=cl_done,
        active_checklist_total_steps=cl_total,
        active_okrs_count=len(okrs),
        average_okr_progress=avg_okr,
        priority_tasks=priority_cards,
        today_schedule=schedule,
    )


async def build_supervisor_dashboard(
    user: User, db: AsyncSession
) -> TeamLeadDashboard:
    subtree_prefix = f"{user.path}{user.id}/"

    # 1. Team Headcount
    members_count = (
        await db.execute(
            select(func.count(User.id)).where(
                User.path.startswith(subtree_prefix), User.is_active.is_(True)
            )
        )
    ).scalar() or 0

    directs_count = (
        await db.execute(
            select(func.count(User.id)).where(
                User.manager_id == user.id, User.is_active.is_(True)
            )
        )
    ).scalar() or 0

    # 2. Team Task Velocity
    team_tasks_stmt = (
        select(
            func.count().filter(Task.status == TaskStatus.IN_PROGRESS),
            func.count().filter(Task.status == TaskStatus.IN_REVIEW),
            func.count().filter(Task.status == TaskStatus.BLOCKED),
        )
        .join(User, Task.assignee_id == User.id)
        .where(User.path.startswith(subtree_prefix))
    )
    prog_cnt, review_cnt, blocked_cnt = (await db.execute(team_tasks_stmt)).one()

    # 3. Pending Timesheets
    pending_ts = (
        await db.execute(
            select(func.count(Timesheet.id))
            .join(User, Timesheet.user_id == User.id)
            .where(
                User.path.startswith(subtree_prefix),
                Timesheet.is_approved.is_(False),
            )
        )
    ).scalar() or 0

    # 4. Review Queue (Tasks in IN_REVIEW needing Lead Approval)
    review_queue_stmt = (
        select(Task)
        .options(selectinload(Task.assignee))
        .join(User, Task.assignee_id == User.id)
        .where(
            User.path.startswith(subtree_prefix),
            Task.status == TaskStatus.IN_REVIEW,
        )
        .limit(5)
    )
    rev_tasks = (await db.execute(review_queue_stmt)).scalars().all()
    review_cards = [
        TaskMiniCard(
            id=t.id,
            title=t.title,
            status=t.status,
            priority=t.priority,
            due_date=t.due_date,
            estimated_hours=t.estimated_hours,
            assignee_name=f"{t.assignee.first_name} {t.assignee.last_name}",
        )
        for t in rev_tasks
    ]

    # Urgent Team Tasks
    urgent_stmt = (
        select(Task)
        .options(selectinload(Task.assignee))
        .join(User, Task.assignee_id == User.id)
        .where(
            User.path.startswith(subtree_prefix),
            Task.status.in_([TaskStatus.TODO, TaskStatus.IN_PROGRESS]),
            Task.priority.in_([Priority.HIGH, Priority.URGENT]),
        )
        .order_by(Task.due_date.asc().nulls_last())
        .limit(5)
    )
    urg_tasks = (await db.execute(urgent_stmt)).scalars().all()
    urgent_cards = [
        TaskMiniCard(
            id=t.id,
            title=t.title,
            status=t.status,
            priority=t.priority,
            due_date=t.due_date,
            estimated_hours=t.estimated_hours,
            assignee_name=f"{t.assignee.first_name} {t.assignee.last_name}",
        )
        for t in urg_tasks
    ]

    # 5. Overallocated Subordinates (>8h/day over upcoming 7 days)
    today = datetime.now(timezone.utc).date()
    subordinates = (
        await db.execute(
            select(User).where(
                User.path.startswith(subtree_prefix), User.is_active.is_(True)
            )
        )
    ).scalars().all()

    overallocated: list[OverallocatedMemberMini] = []
    for sub in subordinates:
        sub_tasks = (
            await db.execute(
                select(Task).where(
                    Task.assignee_id == sub.id,
                    Task.status.in_([TaskStatus.TODO, TaskStatus.IN_PROGRESS]),
                    Task.start_date.is_not(None),
                    Task.due_date.is_not(None),
                )
            )
        ).scalars().all()

        daily_loads = {today + timedelta(days=i): 0.0 for i in range(7)}
        for t in sub_tasks:
            t_start = t.start_date.date()
            t_due = t.due_date.date()
            span = max(1, (t_due - t_start).days + 1)
            daily_rate = t.estimated_hours / span
            for day in daily_loads:
                if t_start <= day <= t_due:
                    daily_loads[day] += daily_rate

        max_day = max(daily_loads.values()) if daily_loads else 0.0
        if max_day > 8.0:
            overallocated.append(
                OverallocatedMemberMini(
                    user_id=sub.id,
                    name=f"{sub.first_name} {sub.last_name}",
                    peak_hours_day=round(max_day, 1),
                )
            )

    return TeamLeadDashboard(
        team_members_count=members_count,
        direct_reports_count=directs_count,
        team_active_tasks_count=prog_cnt,
        team_in_review_tasks_count=review_cnt,
        team_blocked_tasks_count=blocked_cnt,
        pending_timesheets_to_approve_count=pending_ts,
        overallocated_members=overallocated,
        review_queue=review_cards,
        urgent_team_tasks=urgent_cards,
    )


async def build_executive_dashboard(
    user: User, db: AsyncSession
) -> ExecutiveHRDashboard:
    now = datetime.now(timezone.utc)

    # 1. Headcount
    headcount_res = await db.execute(
        select(
            func.count(User.id),
            func.count().filter(User.is_active.is_(True)),
            func.count().filter(User.is_active.is_(False)),
        )
    )
    total_u, active_u, deact_u = headcount_res.one()

    # 2. HR Checklists
    cl_res = await db.execute(
        select(
            func.count().filter(
                UserChecklist.type == ChecklistType.ONBOARDING,
                UserChecklist.is_completed.is_(False),
            ),
            func.count().filter(
                UserChecklist.type == ChecklistType.OFFBOARDING,
                UserChecklist.is_completed.is_(False),
            ),
        )
    )
    open_onb, open_offb = cl_res.one()

    # 3. Reviews
    rev_res = await db.execute(
        select(
            func.count().filter(PerformanceReview.is_finalized.is_(False)),
            func.count().filter(PerformanceReview.is_finalized.is_(True)),
        )
    )
    pend_rev, fin_rev = rev_res.one()

    # 4. Payroll Totals (Current Month)
    pay_res = await db.execute(
        select(
            func.coalesce(func.sum(PayrollItem.gross_pay), 0.0),
            func.coalesce(func.sum(PayrollItem.net_pay), 0.0),
            func.coalesce(func.sum(PayrollItem.tax_deduction), 0.0),
        ).where(PayrollItem.year == now.year, PayrollItem.month == now.month)
    )
    gross_p, net_p, tax_p = pay_res.one()

    # 5. Company-wide Tasks
    tasks_res = await db.execute(
        select(
            func.count(Task.id),
            func.count().filter(Task.status == TaskStatus.BLOCKED),
        )
    )
    tot_tasks, blk_tasks = tasks_res.one()

    return ExecutiveHRDashboard(
        total_company_headcount=total_u,
        active_employees=active_u,
        deactivated_employees=deact_u,
        open_onboardings_count=open_onb,
        open_offboardings_count=open_offb,
        pending_performance_reviews_count=pend_rev,
        finalized_performance_reviews_count=fin_rev,
        current_month_gross_payroll=round(float(gross_p), 2),
        current_month_net_payroll=round(float(net_p), 2),
        total_tax_withheld=round(float(tax_p), 2),
        total_company_tasks=tot_tasks,
        company_blocked_tasks=blk_tasks,
    )