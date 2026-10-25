import uuid
from datetime import date as py_date, datetime, timezone
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.financials import (
    EMIStatus,
    EmployeeLoan,
    LoanEMI,
    LoanStatus,
)
from app.schemas.financials import CustomEMIScheduleItem, LoanCreateRequest


async def create_employee_loan_with_schedule(
    payload: LoanCreateRequest,
    approver_id: uuid.UUID,
    db: AsyncSession,
) -> EmployeeLoan:
    """
    Creates an advance salary / loan record and generates its EMI schedule.
    Supports either custom monthly amounts/dates or equal automatic amortization.
    """
    loan = EmployeeLoan(
        user_id=payload.user_id,
        total_amount=payload.total_amount,
        total_installments=payload.total_installments,
        remaining_balance=payload.total_amount,
        reason=payload.reason,
        status=LoanStatus.ACTIVE,
        approved_by=approver_id,
        disbursed_at=datetime.now(timezone.utc),
    )
    db.add(loan)
    await db.flush()

    # 1. Custom schedule provided
    if payload.custom_schedule:
        schedule_sum = sum(item.amount for item in payload.custom_schedule)
        if round(schedule_sum, 2) != round(payload.total_amount, 2):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Custom schedule sum ({schedule_sum}) must equal total loan amount ({payload.total_amount}).",
            )
        for idx, item in enumerate(payload.custom_schedule, start=1):
            emi = LoanEMI(
                loan_id=loan.id,
                installment_number=idx,
                deduction_year=item.deduction_year,
                deduction_month=item.deduction_month,
                custom_deduction_date=item.custom_deduction_date,
                amount=round(item.amount, 2),
                status=EMIStatus.PENDING,
            )
            db.add(emi)
    else:
        # 2. Automated equal installment division starting at start_year/start_month
        equal_installment = round(payload.total_amount / payload.total_installments, 2)
        cur_year = payload.start_year
        cur_month = payload.start_month

        accumulated = 0.0
        for i in range(1, payload.total_installments + 1):
            # Adjust last installment for rounding differences
            amt = (
                equal_installment
                if i < payload.total_installments
                else round(payload.total_amount - accumulated, 2)
            )
            accumulated += amt

            emi = LoanEMI(
                loan_id=loan.id,
                installment_number=i,
                deduction_year=cur_year,
                deduction_month=cur_month,
                amount=amt,
                status=EMIStatus.PENDING,
            )
            db.add(emi)

            # Advance to next month
            if cur_month == 12:
                cur_month = 1
                cur_year += 1
            else:
                cur_month += 1

    await db.commit()

    res = await db.execute(
        select(EmployeeLoan)
        .options(selectinload(EmployeeLoan.emis), selectinload(EmployeeLoan.user))
        .where(EmployeeLoan.id == loan.id)
    )
    return res.scalar_one()


async def process_loan_deductions_for_cycle(
    user_id: uuid.UUID,
    year: int,
    month: int,
    batch_id: uuid.UUID,
    db: AsyncSession,
) -> float:
    """
    Finds and collects pending EMIs scheduled for this specific payroll month/year.
    Marks matching EMIs as PAID and decrements the remaining loan balance.
    """
    stmt = (
        select(LoanEMI)
        .join(EmployeeLoan, LoanEMI.loan_id == EmployeeLoan.id)
        .where(
            EmployeeLoan.user_id == user_id,
            EmployeeLoan.status == LoanStatus.ACTIVE,
            LoanEMI.deduction_year == year,
            LoanEMI.deduction_month == month,
            LoanEMI.status == EMIStatus.PENDING,
        )
    )
    res = await db.execute(stmt)
    pending_emis = res.scalars().all()

    total_deducted = 0.0
    now = datetime.now(timezone.utc)

    for emi in pending_emis:
        emi.status = EMIStatus.PAID
        emi.paid_in_batch_id = batch_id
        emi.paid_at = now
        total_deducted += emi.amount

        # Decrement parent loan
        loan = await db.get(EmployeeLoan, emi.loan_id)
        if loan:
            loan.remaining_balance = max(0.0, loan.remaining_balance - emi.amount)
            if loan.remaining_balance <= 0.001:
                loan.status = LoanStatus.PAID

    return total_deducted