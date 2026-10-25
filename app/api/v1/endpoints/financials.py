import calendar
import io
import uuid
from datetime import date as py_date
from typing import Annotated
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response, status
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user
from app.core.database import get_db
from app.core.redis import get_redis
from app.models.audit import AuditAction, AuditLog  # <--- Added AuditLog here
from app.models.financials import (
    EMIStatus,
    EmployeeLoan,
    LoanEMI,
    LoanStatus,
    PayrollItem,
    PayrollRecord,
)
from app.models.timesheet import Timesheet
from app.models.user import RoleEnum, User
from app.schemas.financials import (
    AuditLogResponse,
    CustomEMIScheduleItem,
    EmployeeLoanResponse,
    LoanCreateRequest,
    LoanEMIResponse,
    PayrollBatchResponse,
    PayrollCalculateRequest,
    PayrollRecordCreateOrUpdate,
    PayrollRecordResponse,
)
from app.services.audit_service import log_audit_event
from app.services.loan_service import (
    create_employee_loan_with_schedule,
    process_loan_deductions_for_cycle,
)

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
    if current_user.role not in HR_ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Only HR or Admins can configure compensation terms.",
        )

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
# 2. Advance Salary, Loans & Custom EMI System
# -----------------------------------------------------------------------------
@router.post("/loans", response_model=EmployeeLoanResponse, status_code=status.HTTP_201_CREATED)
async def create_loan_or_advance(
    payload: LoanCreateRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> EmployeeLoanResponse:
    """
    Grants an advance salary or company loan with custom EMI schedule.
    Restricted to HR, Admin, Superadmin.
    """
    if current_user.role not in HR_ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Only HR or Admins can disburse salary advances and loans.",
        )

    target_user = await db.get(User, payload.user_id)
    if not target_user:
        raise HTTPException(status_code=404, detail="Target employee not found.")

    loan = await create_employee_loan_with_schedule(
        payload=payload,
        approver_id=current_user.id,
        db=db,
    )
    return loan


@router.get("/loans", response_model=list[EmployeeLoanResponse])
async def list_loans(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user_id: uuid.UUID | None = None,
    loan_status: LoanStatus | None = None,
) -> list[EmployeeLoanResponse]:
    """
    View loan/advance salary records.
    Employees see only their own; HR/Admin see company-wide records.
    """
    stmt = (
        select(EmployeeLoan)
        .options(selectinload(EmployeeLoan.emis), selectinload(EmployeeLoan.user))
        .order_by(EmployeeLoan.created_at.desc())
    )

    if current_user.role not in HR_ADMIN_ROLES:
        stmt = stmt.where(EmployeeLoan.user_id == current_user.id)
    elif user_id:
        stmt = stmt.where(EmployeeLoan.user_id == user_id)

    if loan_status:
        stmt = stmt.where(EmployeeLoan.status == loan_status)

    res = await db.execute(stmt)
    return list(res.scalars().all())


@router.patch("/loans/emis/{emi_id}", response_model=LoanEMIResponse)
async def update_custom_emi_date(
    emi_id: uuid.UUID,
    new_date: py_date | None = None,
    new_amount: float | None = None,
    current_user: Annotated[User, Depends(get_current_user)] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
) -> LoanEMIResponse:
    """
    Allows HR/Admin to reschedule an EMI or adjust the amount before deduction.
    """
    if current_user.role not in HR_ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Forbidden: Only HR can modify EMI schedules.")

    emi = await db.get(LoanEMI, emi_id)
    if not emi:
        raise HTTPException(status_code=404, detail="EMI record not found.")

    if emi.status == EMIStatus.PAID:
        raise HTTPException(status_code=400, detail="Cannot alter an already paid EMI installment.")

    if new_date:
        emi.custom_deduction_date = new_date
        emi.deduction_year = new_date.year
        emi.deduction_month = new_date.month

    if new_amount is not None:
        emi.amount = new_amount

    await db.commit()
    await db.refresh(emi)
    return emi


# -----------------------------------------------------------------------------
# 3. Automated Payroll Calculation Engine with EMI Deductions & Zero Tax
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
    Computes monthly payroll across all active employees.
    Gross Pay = Base + (Approved Hours * Hourly Rate) + Bonus
    Net Pay = Gross Pay - Active EMI Deductions
    """
    if current_user.role not in HR_ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Insufficient authority to execute company-wide payroll calculations.",
        )

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
    start_date = py_date(payload.year, payload.month, 1)
    end_day = calendar.monthrange(payload.year, payload.month)[1]
    end_date = py_date(payload.year, payload.month, end_day)

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
    total_emi_collected = 0.0

    for prof in profiles:
        ts_stmt = select(func.coalesce(func.sum(Timesheet.hours_spent), 0.0)).where(
            Timesheet.user_id == prof.user_id,
            Timesheet.is_approved.is_(True),
            Timesheet.date >= start_date,
            Timesheet.date <= end_date,
        )
        approved_hrs = float((await db.execute(ts_stmt)).scalar() or 0.0)

        bonus_amt = float(payload.bonuses.get(prof.user_id, 0.0))
        hourly_earnings = approved_hrs * prof.hourly_rate

        # 1. Gross Salary = Base + Hourly + Bonus
        gross = prof.base_salary + hourly_earnings + bonus_amt

        # 2. Collect EMI / Advance deductions for this specific month
        loan_deduction = await process_loan_deductions_for_cycle(
            user_id=prof.user_id,
            year=payload.year,
            month=payload.month,
            batch_id=batch_id,
            db=db,
        )

        # 3. Net Salary = Gross Salary - Loan EMI (Zero Tax / Zero Health)
        net = max(0.0, gross - loan_deduction)

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
        item.loan_deduction = round(loan_deduction, 2)
        item.tax_deduction = 0.0
        item.health_deduction = 0.0
        item.net_pay = round(net, 2)

        total_gross += gross
        total_net += net
        total_emi_collected += loan_deduction
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
            "total_emi_collected": round(total_emi_collected, 2),
            "total_net": round(total_net, 2),
        },
    )
    await db.commit()

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
        total_loan_deductions_collected=round(total_emi_collected, 2),
        total_net_disbursed=round(total_net, 2),
        records_processed=len(computed_items),
        items=list(computed_items),
    )


# -----------------------------------------------------------------------------
# 4. CSV Payslip Export Streamer (With Loans & Zero Tax)
# -----------------------------------------------------------------------------
@router.get("/payroll/export")
async def export_payroll_csv(
    year: int = Query(...),
    month: int = Query(...),
    current_user: Annotated[User, Depends(get_current_user)] = None,
    db: Annotated[AsyncSession, Depends(get_db)] = None,
) -> Response:
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
        "Employee Code,Full Name,Email,Department,Year,Month,Approved Hours,Base Pay,Hourly Pay,Bonus,Gross Pay,Loan EMI Deduction,Net Pay\n"
    )

    for i in items:
        u = i.user
        output.write(
            f'"{u.employee_code}","{u.first_name} {u.last_name}","{u.email}","{u.department}",'
            f"{i.year},{i.month},{i.approved_hours:.2f},{i.base_pay:.2f},{i.hourly_pay:.2f},"
            f"{i.bonus:.2f},{i.gross_pay:.2f},{i.loan_deduction:.2f},{i.net_pay:.2f}\n"
        )

    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=payroll_{year}_{month:02d}.csv"
        },
    )


# -----------------------------------------------------------------------------
# 5. Immutable Audit Logs Endpoint
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