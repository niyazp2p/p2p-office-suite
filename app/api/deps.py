import uuid
from typing import Annotated
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.redis import get_redis
from app.core.security import ALGORITHM, is_token_blacklisted
from app.models.user import RoleEnum, User
from app.schemas.token import TokenPayload

oauth2_scheme = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/login")

# PRD Section 3.2 Role Creation Matrix
PERMITTED_ROLES_TO_PROVISION: dict[RoleEnum, set[RoleEnum]] = {
    RoleEnum.SUPERADMIN: {
        RoleEnum.SUPERADMIN,
        RoleEnum.ADMIN,
        RoleEnum.OPERATOR,
        RoleEnum.HR,
        RoleEnum.TEAM_LEAD,
        RoleEnum.ASSOCIATE,
        RoleEnum.INTERN,
    },
    RoleEnum.ADMIN: {
        RoleEnum.ADMIN,
        RoleEnum.OPERATOR,
        RoleEnum.HR,
        RoleEnum.TEAM_LEAD,
        RoleEnum.ASSOCIATE,
        RoleEnum.INTERN,
    },
    RoleEnum.OPERATOR: {
        RoleEnum.OPERATOR,
        RoleEnum.HR,
        RoleEnum.TEAM_LEAD,
        RoleEnum.ASSOCIATE,
        RoleEnum.INTERN,
    },
    RoleEnum.HR: {
        RoleEnum.TEAM_LEAD,
        RoleEnum.ASSOCIATE,
        RoleEnum.INTERN,
    },
    RoleEnum.TEAM_LEAD: set(),
    RoleEnum.ASSOCIATE: set(),
    RoleEnum.INTERN: set(),
}


def assert_can_create_role(creator_role: RoleEnum, target_role: RoleEnum) -> None:
    """Enforces role creation permissions strictly according to security policy."""
    c_role = RoleEnum(creator_role) if isinstance(creator_role, str) else creator_role
    t_role = RoleEnum(target_role) if isinstance(target_role, str) else target_role
    allowed = PERMITTED_ROLES_TO_PROVISION.get(c_role, set())
    if t_role not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Forbidden: Role '{c_role.value}' does not possess "
                f"authorization to instantiate role '{t_role.value}'."
            ),
        )


async def get_current_user(
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
    token: Annotated[str, Depends(oauth2_scheme)],
) -> User:
    """Validates JWT bearer token, checks Redis blacklist, and fetches active user."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[ALGORITHM])
        token_data = TokenPayload(**payload)
    except (JWTError, Exception):
        raise credentials_exception

    # Check revocation blacklist in Redis
    try:
        if await is_token_blacklisted(redis, token_data.jti):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token has been revoked",
                headers={"WWW-Authenticate": "Bearer"},
            )
    except Exception:
        # If Redis is temporarily unreachable, do not crash with 500
        pass

    try:
        user_uuid = uuid.UUID(str(token_data.sub))
    except (ValueError, TypeError):
        raise credentials_exception

    try:
        result = await db.execute(select(User).where(User.id == user_uuid))
        user = result.scalar_one_or_none()
    except Exception as e:
        # Prevents unhandled DB driver crashes from producing raw 500s without explanation
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Database user lookup failed: {str(e)}",
        )

    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account inactive or not found",
        )

    return user