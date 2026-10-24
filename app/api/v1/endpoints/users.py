import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    PERMITTED_ROLES_TO_PROVISION,
    assert_can_create_role,
    get_current_user,
)
from app.core.database import get_db
from app.core.security import get_password_hash
from app.core.utils import generate_employee_code
from app.models.user import RoleEnum, User
from app.schemas.user import (
    OrgTreeNodeResponse,
    PermittedRolesResponse,
    TransferManagerRequest,
    UserCreate,
    UserResponse,
    UserUpdate,
)

router = APIRouter()


@router.get(
    "/roles/permitted-creations",
    response_model=PermittedRolesResponse,
    summary="Get Permitted Roles for Current User",
)
async def get_permitted_roles(
    current_user: Annotated[User, Depends(get_current_user)],
) -> PermittedRolesResponse:
    """
    Returns the list of roles the authenticated user is authorized to provision
    based on the strict Role Creation Matrix (PRD Section 3.2).
    Used by the frontend to dynamically populate role selection dropdowns.
    """
    permitted = list(PERMITTED_ROLES_TO_PROVISION.get(current_user.role, set()))
    return PermittedRolesResponse(
        current_role=current_user.role,
        permitted_to_create=permitted,
    )


@router.post(
    "",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Provision New User Node",
)
async def create_user(
    payload: UserCreate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """
    Provisions a new user in the organizational hierarchy:
    1. Verifies creator privileges via the Role Creation Matrix.
    2. Ensures email uniqueness.
    3. Issues a unique organizational employee_code (or verifies custom code).
    4. Computes the materialized path: parent.path + str(parent.id) + "/".
    5. Saves and returns the sanitized user record.
    """
    # 1. Enforce RBAC Role Matrix
    assert_can_create_role(current_user.role, payload.role)

    # 2. Check for duplicate email
    email_check = await db.execute(
        select(User).where(User.email == payload.email)
    )
    if email_check.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"User with email '{payload.email}' already exists.",
        )

    # 3. Determine unique employee_code
    final_emp_code = payload.employee_code.strip() if payload.employee_code else generate_employee_code()
    code_check = await db.execute(
        select(User).where(User.employee_code == final_emp_code)
    )
    if code_check.scalar_one_or_none():
        if payload.employee_code:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Employee code '{payload.employee_code}' is already assigned.",
            )
        # Collision resolution for generated code
        final_emp_code = generate_employee_code()

    # 4. Compute Materialized Path & Manager Association
    if payload.manager_id:
        parent_stmt = select(User).where(User.id == payload.manager_id)
        parent_result = await db.execute(parent_stmt)
        manager = parent_result.scalar_one_or_none()
        if not manager:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Designated manager ID '{payload.manager_id}' does not exist.",
            )
        computed_path = f"{manager.path}{manager.id}/"
    else:
        # Only SUPERADMIN can exist as a root node without an immediate manager
        if current_user.role != RoleEnum.SUPERADMIN:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A valid manager_id is required to attach the new user to an existing reporting branch.",
            )
        computed_path = "/root/"

    new_user = User(
        email=payload.email,
        employee_code=final_emp_code,
        password_hash=get_password_hash(payload.password),
        first_name=payload.first_name,
        last_name=payload.last_name,
        role=payload.role,
        department=payload.department,
        designation=payload.designation,
        avatar_url=payload.avatar_url,
        manager_id=payload.manager_id,
        path=computed_path,
        is_active=True,
    )

    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)
    return new_user


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Get Authenticated User Profile",
)
async def get_current_user_profile(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    """Returns profile data, role, employee code, and path for the currently authenticated user."""
    return current_user


@router.get(
    "/tree",
    response_model=list[OrgTreeNodeResponse],
    summary="Get Hierarchical Organization Tree",
)
async def get_organization_tree(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[OrgTreeNodeResponse]:
    """
    Constructs a nested DAG representation of the organizational tree for React Flow / D3 visualization.
    - Superadmins and Admins receive the entire corporate organization tree.
    - Team Leads and other roles receive the operational subtree rooted at their position.
    """
    if current_user.role in [RoleEnum.SUPERADMIN, RoleEnum.ADMIN]:
        stmt = (
            select(User)
            .where(User.is_active.is_(True))
            .order_by(User.created_at)
        )
    else:
        subtree_prefix = f"{current_user.path}{current_user.id}/"
        stmt = (
            select(User)
            .where(
                User.is_active.is_(True),
                or_(
                    User.id == current_user.id,
                    User.path.startswith(subtree_prefix),
                ),
            )
            .order_by(User.created_at)
        )

    result = await db.execute(stmt)
    users = result.scalars().all()

    # O(N) In-memory nested tree constructor via hash map
    nodes_map: dict[str, OrgTreeNodeResponse] = {}
    for u in users:
        nodes_map[str(u.id)] = OrgTreeNodeResponse(
            id=str(u.id),
            employee_code=u.employee_code,
            name=f"{u.first_name} {u.last_name}",
            role=u.role,
            department=u.department,
            designation=u.designation,
            children=[],
        )

    roots: list[OrgTreeNodeResponse] = []
    for u in users:
        node = nodes_map[str(u.id)]
        manager_key = str(u.manager_id) if u.manager_id else None
        if manager_key and manager_key in nodes_map:
            nodes_map[manager_key].children.append(node)
        else:
            roots.append(node)

    return roots


@router.get(
    "/{user_id}/subordinates",
    response_model=list[UserResponse],
    summary="Get Subordinates for Task Assignment",
)
async def get_user_subordinates(
    user_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    direct_only: bool = Query(
        False,
        description="If True, returns only immediate direct reports; otherwise returns the entire downstream subtree",
    ),
) -> list[User]:
    """
    Returns valid subordinate assignees for a given user.
    Used by Task Management to populate the assignee selection dropdown
    while enforcing the constraint that tasks can only be assigned downwards.
    """
    target = await db.scalar(select(User).where(User.id == user_id))
    if not target:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Target user not found.",
        )

    if direct_only:
        stmt = select(User).where(
            User.manager_id == user_id,
            User.is_active.is_(True),
        )
    else:
        subtree_prefix = f"{target.path}{target.id}/"
        stmt = select(User).where(
            User.path.startswith(subtree_prefix),
            User.is_active.is_(True),
        )

    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.patch(
    "/{user_id}/transfer-manager",
    response_model=UserResponse,
    summary="Transfer User to New Manager (Subtree Re-parenting)",
)
async def transfer_user_manager(
    user_id: uuid.UUID,
    payload: TransferManagerRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """
    Re-parents an employee to a new manager node.
    Atomically updates the user's path and recalculates the materialized paths
    of all downstream subtree subordinates inside a single transaction.
    """
    if current_user.role not in [
        RoleEnum.SUPERADMIN,
        RoleEnum.ADMIN,
        RoleEnum.OPERATOR,
        RoleEnum.HR,
    ]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Privilege level insufficient to reassign reporting hierarchy.",
        )

    user = await db.scalar(select(User).where(User.id == user_id))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Target user not found.",
        )

    new_manager = await db.scalar(
        select(User).where(User.id == payload.new_manager_id)
    )
    if not new_manager:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Designated new manager not found.",
        )

    # Prevent assigning oneself as manager
    if payload.new_manager_id == user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A user cannot be designated as their own manager.",
        )

    # Prevent cyclic hierarchy (re-parenting a manager underneath their own subordinate)
    if new_manager.path.startswith(f"{user.path}{user.id}/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid hierarchy transfer: Cannot assign a user underneath their own subordinate (cycle detected).",
        )

    old_prefix = f"{user.path}{user.id}/"
    new_path = f"{new_manager.path}{new_manager.id}/"
    new_prefix = f"{new_path}{user.id}/"

    # 1. Update target user
    user.manager_id = new_manager.id
    user.path = new_path

    # 2. Update all downstream descendants in the subtree
    descendants = (
        (
            await db.execute(
                select(User).where(User.path.startswith(old_prefix))
            )
        )
        .scalars()
        .all()
    )

    for desc in descendants:
        desc.path = desc.path.replace(old_prefix, new_prefix, 1)

    await db.commit()
    await db.refresh(user)
    return user


@router.get(
    "",
    response_model=list[UserResponse],
    summary="List Employee Directory",
)
async def list_users(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    department: str | None = Query(None, description="Filter by department"),
    role: RoleEnum | None = Query(None, description="Filter by role"),
    search: str | None = Query(
        None,
        description="Search by first name, last name, email, or employee code",
    ),
    is_active: bool | None = Query(True, description="Filter by active status"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
) -> list[User]:
    """
    Searchable, paginated employee directory indexing staff profiles.
    Supports filtering by department, role, active status, and textual search.
    """
    stmt = select(User).offset(skip).limit(limit).order_by(User.first_name)

    if is_active is not None:
        stmt = stmt.where(User.is_active == is_active)
    if department:
        stmt = stmt.where(User.department.ilike(f"%{department}%"))
    if role:
        stmt = stmt.where(User.role == role)
    if search:
        search_term = f"%{search}%"
        stmt = stmt.where(
            or_(
                User.first_name.ilike(search_term),
                User.last_name.ilike(search_term),
                User.email.ilike(search_term),
                User.employee_code.ilike(search_term),
            )
        )

    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get(
    "/{user_id}",
    response_model=UserResponse,
    summary="Get User Profile by ID",
)
async def get_user_by_id(
    user_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Retrieves profile details for a specific user ID."""
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )
    return user


@router.patch(
    "/{user_id}",
    response_model=UserResponse,
    summary="Update User Profile",
)
async def update_user(
    user_id: uuid.UUID,
    payload: UserUpdate,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """
    Updates profile details.
    - Superadmin, Admin, and HR can edit employee profiles.
    - Regular employees can only modify their own personal profile data.
    """
    if current_user.role not in [
        RoleEnum.SUPERADMIN,
        RoleEnum.ADMIN,
        RoleEnum.HR,
    ]:
        if current_user.id != user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to modify profiles outside your own account.",
            )

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    update_data = payload.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(user, field, value)

    await db.commit()
    await db.refresh(user)
    return user


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="Deactivate User (Soft Delete)",
)
async def deactivate_user(
    user_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    """
    Deactivates a user account (soft delete).
    Preserves audit trails, timesheets, and task histories while revoking platform access.
    """
    if current_user.role not in [RoleEnum.SUPERADMIN, RoleEnum.ADMIN]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only Admins and Superadmins are authorized to deactivate accounts.",
        )

    if current_user.id == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You cannot deactivate your own account.",
        )

    user = await db.scalar(select(User).where(User.id == user_id))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found.",
        )

    user.is_active = False
    await db.commit()
    return {
        "detail": f"User {user.email} (Employee: {user.employee_code}) was successfully deactivated."
    }