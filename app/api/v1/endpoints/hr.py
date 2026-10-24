import uuid
from datetime import datetime, timezone
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.hr import ChecklistType, OKR, OKRCycle, PerformanceReview, UserChecklist
from app.models.user import RoleEnum, User
from app.schemas.hr import (
    ChecklistBatchCreate,
    ChecklistStepCreate,
    ChecklistStepResponse,
    ChecklistStepUpdate,
    ManagerEvaluationUpdate,
    OKRCreate,
    OKRResponse,
    OKRUpdate,
    PerformanceReviewCreate,
    PerformanceReviewResponse,
    SelfEvaluationUpdate,
)

router = APIRouter()

HR_ADMIN_ROLES = {RoleEnum.SUPERADMIN, RoleEnum.ADMIN, RoleEnum.HR}


# -----------------------------------------------------------------------------
# 1. Onboarding & Offboarding Checklist Engine
# -----------------------------------------------------------------------------
@router.get("/checklists/{user_id}", response_model=list[ChecklistStepResponse])
async def get_user_checklists(
    user_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    checklist_type: ChecklistType | None = Query(None, alias="type"),
) -> list[ChecklistStepResponse]:
    """
    Access: Restricted to HR, Superadmin, Admin, or the target user themselves.
    """
    if current_user.id != user_id and current_user.role not in HR_ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Cannot view employee checklists outside HR management scope.",
        )

    stmt = select(UserChecklist).where(UserChecklist.user_id == user_id)
    if checklist_type:
        stmt = stmt.where(UserChecklist.type == checklist_type)

    stmt = stmt.order_by(UserChecklist.created_at.asc())
    res = await db.execute(stmt)
    return list(res.scalars().all())


@router.post("/checklists/{user_id}", response_model=list[ChecklistStepResponse], status_code=status.HTTP_201_CREATED)
async def assign_checklist_template(
    user_id: uuid.UUID,
    payload: ChecklistBatchCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ChecklistStepResponse]:
    """
    Assigns batch onboarding or offboarding steps to an employee.
    Restricted to HR, Admin, Superadmin.
    """
    if current_user.role not in HR_ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Only HR or Admins can assign onboarding/offboarding templates.",
        )

    # Verify user exists
    target_res = await db.execute(select(User).where(User.id == user_id))
    if not target_res.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Target user not found.")

    created_steps: list[UserChecklist] = []
    for step_title in payload.steps:
        entry = UserChecklist(
            user_id=user_id,
            type=payload.type,
            task_title=step_title,
            is_completed=False,
            assigned_by=current_user.id,
        )
        db.add(entry)
        created_steps.append(entry)

    await db.commit()
    for s in created_steps:
        await db.refresh(s)
    return created_steps


@router.patch("/checklists/{user_id}/steps/{step_id}", response_model=ChecklistStepResponse)
async def update_checklist_step(
    user_id: uuid.UUID,
    step_id: uuid.UUID,
    payload: ChecklistStepUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ChecklistStepResponse:
    """
    Marks a milestone step complete or incomplete.
    Allowed by the employee or HR/Admin.
    """
    if current_user.id != user_id and current_user.role not in HR_ADMIN_ROLES:
        raise HTTPException(status_code=403, detail="Not authorized to modify this checklist.")

    stmt = select(UserChecklist).where(
        UserChecklist.id == step_id, UserChecklist.user_id == user_id
    )
    res = await db.execute(stmt)
    step = res.scalar_one_or_none()
    if not step:
        raise HTTPException(status_code=404, detail="Checklist step not found.")

    step.is_completed = payload.is_completed
    step.completed_at = datetime.now(timezone.utc) if payload.is_completed else None
    await db.commit()
    await db.refresh(step)
    return step


# -----------------------------------------------------------------------------
# 2. OKRs (Objectives & Key Results)
# -----------------------------------------------------------------------------
@router.get("/okrs", response_model=list[OKRResponse])
async def list_okrs(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    user_id: uuid.UUID | None = None,
    cycle: OKRCycle | None = None,
    year: int | None = None,
) -> list[OKRResponse]:
    """
    List OKRs scoped to self or reporting subtree. HR and Superadmin view all.
    """
    stmt = select(OKR).options(selectinload(OKR.user)).join(User, OKR.user_id == User.id)

    if current_user.role not in HR_ADMIN_ROLES:
        subtree_prefix = f"{current_user.path}{current_user.id}/"
        stmt = stmt.where(
            (OKR.user_id == current_user.id)
            | (User.path.startswith(subtree_prefix))
        )

    if user_id:
        stmt = stmt.where(OKR.user_id == user_id)
    if cycle:
        stmt = stmt.where(OKR.cycle == cycle)
    if year:
        stmt = stmt.where(OKR.year == year)

    res = await db.execute(stmt)
    okrs = res.scalars().all()

    output = []
    for o in okrs:
        item = OKRResponse.model_validate(o)
        item.progress_percentage = (
            round(min(100.0, (o.current_value / o.target_value) * 100.0), 2)
            if o.target_value > 0
            else 0.0
        )
        output.append(item)
    return output


@router.post("/okrs", response_model=OKRResponse, status_code=status.HTTP_201_CREATED)
async def create_okr(
    payload: OKRCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OKRResponse:
    target_user_id = payload.user_id or current_user.id

    # If setting for someone else, ensure target is in reporting subtree or caller is HR/Admin
    if target_user_id != current_user.id:
        if current_user.role not in HR_ADMIN_ROLES:
            target_res = await db.execute(select(User).where(User.id == target_user_id))
            target_user = target_res.scalar_one_or_none()
            if not target_user:
                raise HTTPException(status_code=404, detail="Target user not found.")
            required_prefix = f"{current_user.path}{current_user.id}/"
            if not target_user.path.startswith(required_prefix):
                raise HTTPException(
                    status_code=403,
                    detail="Cannot create OKR for employees outside your reporting subtree.",
                )

    okr = OKR(
        user_id=target_user_id,
        title=payload.title,
        description=payload.description,
        target_value=payload.target_value,
        current_value=payload.current_value,
        unit=payload.unit,
        cycle=payload.cycle,
        year=payload.year,
    )
    db.add(okr)
    await db.commit()

    stmt = select(OKR).options(selectinload(OKR.user)).where(OKR.id == okr.id)
    res = await db.execute(stmt)
    saved = res.scalar_one()

    resp = OKRResponse.model_validate(saved)
    resp.progress_percentage = (
        round(min(100.0, (saved.current_value / saved.target_value) * 100.0), 2)
        if saved.target_value > 0
        else 0.0
    )
    return resp


@router.patch("/okrs/{okr_id}", response_model=OKRResponse)
async def update_okr_progress(
    okr_id: uuid.UUID,
    payload: OKRUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OKRResponse:
    stmt = select(OKR).options(selectinload(OKR.user)).where(OKR.id == okr_id)
    res = await db.execute(stmt)
    okr = res.scalar_one_or_none()
    if not okr:
        raise HTTPException(status_code=404, detail="OKR not found.")

    is_owner = okr.user_id == current_user.id
    is_hr = current_user.role in HR_ADMIN_ROLES
    is_manager = str(current_user.id) in okr.user.path

    if not (is_owner or is_hr or is_manager):
        raise HTTPException(status_code=403, detail="Not authorized to update this OKR.")

    if payload.title is not None:
        okr.title = payload.title
    if payload.description is not None:
        okr.description = payload.description
    if payload.current_value is not None:
        okr.current_value = payload.current_value
    if payload.target_value is not None:
        okr.target_value = payload.target_value
    if payload.unit is not None:
        okr.unit = payload.unit

    await db.commit()
    await db.refresh(okr)

    resp = OKRResponse.model_validate(okr)
    resp.progress_percentage = (
        round(min(100.0, (okr.current_value / okr.target_value) * 100.0), 2)
        if okr.target_value > 0
        else 0.0
    )
    return resp


@router.delete("/okrs/{okr_id}", status_code=status.HTTP_200_OK)
async def delete_okr(
    okr_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    stmt = select(OKR).options(selectinload(OKR.user)).where(OKR.id == okr_id)
    res = await db.execute(stmt)
    okr = res.scalar_one_or_none()
    if not okr:
        raise HTTPException(status_code=404, detail="OKR not found.")

    is_owner = okr.user_id == current_user.id
    is_hr = current_user.role in HR_ADMIN_ROLES
    if not (is_owner or is_hr):
        raise HTTPException(status_code=403, detail="Not authorized to delete this OKR.")

    await db.delete(okr)
    await db.commit()
    return {"detail": "OKR deleted successfully."}


# -----------------------------------------------------------------------------
# 3. Performance Review Cycles & Appraisals
# -----------------------------------------------------------------------------
@router.get("/reviews", response_model=list[PerformanceReviewResponse])
async def list_performance_reviews(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    year: int | None = None,
    cycle: OKRCycle | None = None,
) -> list[PerformanceReviewResponse]:
    """
    Associates/Interns view reviews where they are reviewee.
    Managers view reviews where they are reviewer or where reviewee is in reporting subtree.
    HR/Admin view all reviews.
    """
    stmt = (
        select(PerformanceReview)
        .options(
            selectinload(PerformanceReview.reviewee),
            selectinload(PerformanceReview.reviewer),
        )
        .join(User, PerformanceReview.reviewee_id == User.id)
    )

    if current_user.role not in HR_ADMIN_ROLES:
        subtree_prefix = f"{current_user.path}{current_user.id}/"
        stmt = stmt.where(
            (PerformanceReview.reviewee_id == current_user.id)
            | (PerformanceReview.reviewer_id == current_user.id)
            | (User.path.startswith(subtree_prefix))
        )

    if year:
        stmt = stmt.where(PerformanceReview.year == year)
    if cycle:
        stmt = stmt.where(PerformanceReview.cycle == cycle)

    stmt = stmt.order_by(PerformanceReview.created_at.desc())
    res = await db.execute(stmt)
    return list(res.scalars().all())


@router.post("/reviews", response_model=PerformanceReviewResponse, status_code=status.HTTP_201_CREATED)
async def initiate_performance_review(
    payload: PerformanceReviewCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PerformanceReviewResponse:
    """
    Initiates a review cycle for an employee.
    Restricted to HR, Admin, Superadmin, or the direct manager of the reviewee.
    """
    # Fetch reviewee
    reviewee_res = await db.execute(select(User).where(User.id == payload.reviewee_id))
    reviewee = reviewee_res.scalar_one_or_none()
    if not reviewee:
        raise HTTPException(status_code=404, detail="Reviewee user not found.")

    # Designated reviewer defaults to reviewee's direct manager, or the initiator if supervisor
    reviewer_id = reviewee.manager_id or current_user.id

    is_hr = current_user.role in HR_ADMIN_ROLES
    is_supervisor = str(current_user.id) in reviewee.path
    if not (is_hr or is_supervisor):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot initiate reviews for employees outside your supervisory reporting line.",
        )

    review = PerformanceReview(
        reviewee_id=payload.reviewee_id,
        reviewer_id=reviewer_id,
        cycle=payload.cycle,
        year=payload.year,
        is_finalized=False,
    )
    db.add(review)
    await db.commit()

    stmt = (
        select(PerformanceReview)
        .options(
            selectinload(PerformanceReview.reviewee),
            selectinload(PerformanceReview.reviewer),
        )
        .where(PerformanceReview.id == review.id)
    )
    res = await db.execute(stmt)
    return res.scalar_one()


@router.patch("/reviews/{review_id}/self", response_model=PerformanceReviewResponse)
async def submit_self_evaluation(
    review_id: uuid.UUID,
    payload: SelfEvaluationUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PerformanceReviewResponse:
    """
    Self-Appraisal: Only the reviewee can submit their self-score and notes.
    Locked if review is already finalized.
    """
    stmt = select(PerformanceReview).where(PerformanceReview.id == review_id)
    res = await db.execute(stmt)
    review = res.scalar_one_or_none()
    if not review:
        raise HTTPException(status_code=404, detail="Review record not found.")

    if review.reviewee_id != current_user.id:
        raise HTTPException(status_code=403, detail="Only the reviewee can submit self-evaluations.")

    if review.is_finalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Locked: Finalized reviews cannot be modified.",
        )

    review.self_score = payload.self_score
    review.self_notes = payload.self_notes
    await db.commit()
    await db.refresh(review)
    return review


@router.patch("/reviews/{review_id}/manager", response_model=PerformanceReviewResponse)
async def submit_manager_evaluation(
    review_id: uuid.UUID,
    payload: ManagerEvaluationUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PerformanceReviewResponse:
    """
    Manager Appraisal: Only the designated reviewer or HR/Admin can submit manager rating.
    Locked if review is already finalized.
    """
    stmt = (
        select(PerformanceReview)
        .options(selectinload(PerformanceReview.reviewee))
        .where(PerformanceReview.id == review_id)
    )
    res = await db.execute(stmt)
    review = res.scalar_one_or_none()
    if not review:
        raise HTTPException(status_code=404, detail="Review record not found.")

    is_reviewer = review.reviewer_id == current_user.id
    is_hr = current_user.role in HR_ADMIN_ROLES
    if not (is_reviewer or is_hr):
        raise HTTPException(status_code=403, detail="Not authorized to submit manager evaluation for this review.")

    if review.is_finalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Locked: Finalized reviews cannot be modified.",
        )

    review.manager_score = payload.manager_score
    review.feedback = payload.feedback
    await db.commit()
    await db.refresh(review)
    return review


@router.post("/reviews/{review_id}/finalize", response_model=PerformanceReviewResponse)
async def finalize_performance_review(
    review_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PerformanceReviewResponse:
    """
    Formal Lock: HR or Superadmin seals the review, making it permanently immutable.
    """
    if current_user.role not in HR_ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Only HR or Admins possess authority to finalize performance reviews.",
        )

    stmt = select(PerformanceReview).where(PerformanceReview.id == review_id)
    res = await db.execute(stmt)
    review = res.scalar_one_or_none()
    if not review:
        raise HTTPException(status_code=404, detail="Review not found.")

    if not review.self_score or not review.manager_score:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot finalize review: Both self-evaluation and manager-evaluation must be submitted first.",
        )

    review.is_finalized = True
    review.finalized_by = current_user.id
    review.finalized_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(review)
    return review