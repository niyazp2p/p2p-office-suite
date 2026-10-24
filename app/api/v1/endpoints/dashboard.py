from typing import Annotated
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.user import RoleEnum, User
from app.schemas.dashboard import MasterDashboardResponse
from app.services.dashboard_service import (
    build_contributor_dashboard,
    build_executive_dashboard,
    build_supervisor_dashboard,
)

router = APIRouter()


@router.get("/master", response_model=MasterDashboardResponse)
async def get_master_dashboard(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> MasterDashboardResponse:
    """
    Returns the role-adaptive master dashboard configured for the caller:
    - ASSOCIATE / INTERN -> Contributor "My Day" view (tasks, personal timesheets, OKRs, today's schedule)
    - TEAM_LEAD / OPERATOR -> Team Cockpit (downstream velocity, reviews, capacity alerts)
    - HR / ADMIN / SUPERADMIN -> Executive Enterprise Overview (headcount, lifecycle, payroll, company pulse)
    """
    user_name = f"{current_user.first_name} {current_user.last_name}"

    # 1. Executive Roles
    if current_user.role in {RoleEnum.SUPERADMIN, RoleEnum.ADMIN, RoleEnum.HR}:
        exec_view = await build_executive_dashboard(current_user, db)
        return MasterDashboardResponse(
            user_id=current_user.id,
            role=current_user.role,
            employee_code=current_user.employee_code,
            user_name=user_name,
            view_type="EXECUTIVE",
            executive_view=exec_view,
        )

    # 2. Supervisory Roles
    if current_user.role in {RoleEnum.TEAM_LEAD, RoleEnum.OPERATOR}:
        super_view = await build_supervisor_dashboard(current_user, db)
        return MasterDashboardResponse(
            user_id=current_user.id,
            role=current_user.role,
            employee_code=current_user.employee_code,
            user_name=user_name,
            view_type="SUPERVISOR",
            supervisor_view=super_view,
        )

    # 3. Individual Contributors
    contrib_view = await build_contributor_dashboard(current_user, db)
    return MasterDashboardResponse(
        user_id=current_user.id,
        role=current_user.role,
        employee_code=current_user.employee_code,
        user_name=user_name,
        view_type="CONTRIBUTOR",
        contributor_view=contrib_view,
    )