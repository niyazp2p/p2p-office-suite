import calendar
import io
import uuid
from datetime import date
from typing import Annotated
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user
from app.core.database import get_db
from app.core.redis import get_redis
from app.models.audit import AuditAction, AuditLog
from app.models.financials import PayrollItem, PayrollRecord
from app.models.timesheet import Timesheet
from app.models.user import RoleEnum, User
from app.schemas.financials import (
    AuditLogResponse,
    PayrollBatchResponse,
    PayrollCalculateRequest,
    PayrollRecordCreateOrUpdate,
    PayrollRecordResponse,
)
from app.services.audit_service import log_audit_event

router = APIRouter()

HR_ADMIN_ROLES = {RoleEnum.SUPERADMIN, RoleEnum.ADMIN, RoleEnum.HR}


# -----------------------------------------------------------------------------
# 1. Employee Compensation Profiles
# -----------------------------------------------------------------------------
@router.get("/payroll/profile/{user_id}", response_model=PayrollRecordResponse)
async def get_payroll_profile(
    user_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PayrollRecordResponse:
    """
    Employees can view their own profile.
    HR, Admin, and Superadmin can view any employee's compensation terms.
    """
    if current_user.id != user_id and current_user.role not in HR_ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Cannot view compensation records outside your scope.",
        )

    stmt = (
        select(PayrollRecord)
        .options(selectinload(PayrollRecord.user))
        .where(PayrollRecord.user_id == user_id)
    )
    res = await db.execute(stmt)
    rec = res.scalar_one_or_none()
    if not rec:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Compensation record not yet configured for this employee.",
        )
    return rec


@router.post("/payroll/profile/{user_id}", response_model=PayrollRecordResponse)
async def set_or_update_payroll_profile(
    user_id: uuid.UUID,
    payload: PayrollRecordCreateOrUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PayrollRecordResponse:
    """
    Only HR, Admin, or Superadmin can configure employee compensation packages.
    """
    if current_user.role not in HR_ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Only HR or Admins can configure compensation terms.",
        )

    # Validate target user exists
    user_res = await db.execute(select(User).where(User.id == user_id))
    if not user_res.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Target employee not found.")

    stmt = select(PayrollRecord).where(PayrollRecord.user_id == user_id)
    res = await db.execute(stmt)
    rec = res.scalar_one_or_none()

    if not rec:
        rec = PayrollRecord(user_id=user_id, **payload.model_dump())
        db.add(rec)
    else:
        for k, v in payload.model_dump().items():
            setattr(rec, k, v)

    await db.flush()
    await log_audit_event(
        db=db,
        actor_id=current_user.id,
        action=AuditAction.PAYROLL_RECORD_UPDATED,
        target_entity="payroll_records",
        target_id=str(user_id),
        details=payload.model_dump(),
    )
    await db.commit()

    reloaded = (
        await db.execute(
            select(PayrollRecord)
            .options(selectinload(PayrollRecord.user))
            .where(PayrollRecord.user_id == user_id)
        )
    ).scalar_one()
    return reloaded


# -----------------------------------------------------------------------------
# 2. Automated Payroll Calculation Engine with Redis Idempotency Lock
# -----------------------------------------------------------------------------
@router.post("/payroll/calculate", response_model=PayrollBatchResponse)
async def calculate_monthly_payroll(
    payload: PayrollCalculateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
) -> PayrollBatchResponse:
    """
    Calculates monthly payroll across all active employees.
    Locks execution via Redis (TTL = 60s) if an Idempotency-Key header is supplied.
    Formula:
      Gross = Base + (Approved Timesheet Hours * Hourly Rate) + Bonus
      Net = Gross - [(Gross * Tax Rate / 100) + Health Deduction]
    """
    if current_user.role not in HR_ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Insufficient authority to execute company-wide payroll calculations.",
        )

    # Redis Idempotency Lock
    if idempotency_key:
        lock_acquired = await redis.set(
            f"idemp_payroll_{idempotency_key}", "locked", nx=True, ex=60
        )
        if not lock_acquired:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Duplicate execution blocked: An identical payroll run is currently executing or completed.",
            )

    batch_id = uuid.uuid4()
    start_date = date(payload.year, payload.month, 1)
    end_day = calendar.monthrange(payload.year, payload.month)[1]
    end_date = date(payload.year, payload.month, end_day)

    # Fetch active employees with configured compensation profiles
    profiles_stmt = (
        select(PayrollRecord)
        .options(selectinload(PayrollRecord.user))
        .join(User, PayrollRecord.user_id == User.id)
        .where(PayrollRecord.is_active.is_(True), User.is_active.is_(True))
    )
    profiles = (await db.execute(profiles_stmt)).scalars().all()

    items: list[PayrollItem] = []
    total_gross = 0.0
    total_net = 0.0
    total_tax = 0.0

    for prof in profiles:
        # Sum approved timesheet hours in the calendar month
        ts_stmt = select(func.coalesce(func.sum(Timesheet.hours_spent), 0.0)).where(
            Timesheet.user_id == prof.user_id,
            Timesheet.is_approved.is_(True),
            Timesheet.date >= start_date,
            Timesheet.date <= end_date,
        )
        approved_hrs = float((await db.execute(ts_stmt)).scalar() or 0.0)

        bonus_amt = float(payload.bonuses.get(prof.user_id, 0.0))
        hourly_earnings = approved_hrs * prof.hourly_rate
        gross = prof.base_salary + hourly_earnings + bonus_amt

        tax_amt = gross * (prof.tax_rate_percent / 100.0)
        net = max(0.0, gross - (tax_amt + prof.health_deduction))

        # Check existing item to avoid unique constraint conflict on re-run
        existing_stmt = select(PayrollItem).where(
            PayrollItem.user_id == prof.user_id,
            PayrollItem.year == payload.year,
            PayrollItem.month == payload.month,
        )
        item = (await db.execute(existing_stmt)).scalar_one_or_none()

        if not item:
            item = PayrollItem(
                calculation_batch_id=batch_id,
                user_id=prof.user_id,
                year=payload.year,
                month=payload.month,
            )
            db.add(item)

        item.calculation_batch_id = batch_id
        item.approved_hours = round(approved_hrs, 2)
        item.base_pay = round(prof.base_salary, 2)
        item.hourly_pay = round(hourly_earnings, 2)
        item.bonus = round(bonus_amt, 2)
        item.gross_pay = round(gross, 2)
        item.tax_deduction = round(tax_amt, 2)
        item.health_deduction = round(prof.health_deduction, 2)
        item.net_pay = round(net, 2)

        total_gross += gross
        total_net += net
        total_tax += tax_amt
        items.append(item)

    await db.flush()
    await log_audit_event(
        db=db,
        actor_id=current_user.id,
        action=AuditAction.PAYROLL_BATCH_CALCULATED,
        target_entity="payroll_items",
        target_id=str(batch_id),
        details={
            "year": payload.year,
            "month": payload.month,
            "records": len(items),
            "total_gross": round(total_gross, 2),
            "total_net": round(total_net, 2),
        },
    )
    await db.commit()

    # Query with eager-loaded user associations
    result_stmt = (
        select(PayrollItem)
        .options(selectinload(PayrollItem.user))
        .where(PayrollItem.calculation_batch_id == batch_id)
    )
    computed_items = (await db.execute(result_stmt)).scalars().all()

    return PayrollBatchResponse(
        calculation_batch_id=batch_id,
        year=payload.year,
        month=payload.month,
        total_gross_disbursed=round(total_gross, 2),
        total_net_disbursed=round(total_net, 2),
        total_tax_withheld=round(total_tax, 2),
        records_processed=len(computed_items),
        items=list(computed_items),
    )


# -----------------------------------------------------------------------------
# 3. CSV Payslip Export Streamer
# -----------------------------------------------------------------------------
@router.get("/payroll/export")
async def export_payroll_csv(
    year: int = Query(...),
    month: int = Query(...),
    current_user: Annotated[User, Depends(get_current_user)] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
) -> Response:
    """
    Streams a CSV export of the payroll breakdown for accounting.
    """
    if current_user.role not in HR_ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Cannot export company payroll registries.",
        )

    stmt = (
        select(PayrollItem)
        .options(selectinload(PayrollItem.user))
        .where(PayrollItem.year == year, PayrollItem.month == month)
        .order_by(PayrollItem.calculated_at.desc())
    )
    items = (await db.execute(stmt)).scalars().all()

    output = io.StringIO()
    output.write(
        "Employee Code,Full Name,Email,Department,Year,Month,Approved Hours,Base Pay,Hourly Pay,Bonus,Gross Pay,Tax Deduction,Health Deduction,Net Pay\n"
    )

    for i in items:
        u = i.user
        output.write(
            f'"{u.employee_code}","{u.first_name} {u.last_name}","{u.email}","{u.department}",'
            f"{i.year},{i.month},{i.approved_hours:.2f},{i.base_pay:.2f},{i.hourly_pay:.2f},"
            f"{i.bonus:.2f},{i.gross_pay:.2f},{i.tax_deduction:.2f},{i.health_deduction:.2f},{i.net_pay:.2f}\n"
        )

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=payroll_{year}_{month:02d}.csv"
        },
    )


# -----------------------------------------------------------------------------
# 4. Immutable Audit Logs Endpoint
# -----------------------------------------------------------------------------
@router.get("/audit-logs", response_model=list[AuditLogResponse])
async def list_audit_logs(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    action: AuditAction | None = None,
    target_entity: str | None = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
) -> list[AuditLogResponse]:
    """
    Superadmins and Admins can query system audit logs.
    """
    if current_user.role not in {RoleEnum.SUPERADMIN, RoleEnum.ADMIN}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access Denied: Audit trails can only be examined by System Administrators.",
        )

    stmt = (
        select(AuditLog)
        .options(selectinload(AuditLog.actor))
        .order_by(AuditLog.timestamp.desc())
    )
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if target_entity:
        stmt = stmt.where(AuditLog.target_entity == target_entity)

    stmt = stmt.offset(skip).limit(limit)
    res = await db.execute(stmt)
    return list(res.scalars().all())