import time
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from jose import JWTError, jwt
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, oauth2_scheme
from app.core.config import settings
from app.core.database import get_db
from app.core.redis import get_redis
from app.core.security import (
    ALGORITHM,
    blacklist_token,
    create_token_pair,
    get_password_hash,
    is_token_blacklisted,
    verify_password,
)
from app.models.user import RoleEnum, User
from app.schemas.token import LoginRequest, RefreshTokenRequest, Token
from app.schemas.user import (
    AdminResetPasswordRequest,
    ChangePasswordRequest,
    ResetPasswordWithCodeRequest,
)

router = APIRouter()


@router.post(
    "/login",
    response_model=Token,
    summary="OAuth2 Form Login (for Swagger UI)",
)
async def login(
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Token:
    """Authenticates credentials via standard OAuth2 Form specifications."""
    stmt = select(User).where(User.email == form_data.username)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if not user or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User profile is deactivated",
        )

    access_token, refresh_token, expires_in = create_token_pair(
        subject=str(user.id),
        role=user.role.value,
        path=user.path,
    )

    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=expires_in,
    )


@router.post(
    "/login/json",
    response_model=Token,
    summary="JSON Login (for Frontend Applications)",
)
async def login_json(
    payload: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Token:
    """Authenticates credentials via pure JSON payload."""
    stmt = select(User).where(User.email == payload.email)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User profile is deactivated",
        )

    access_token, refresh_token, expires_in = create_token_pair(
        subject=str(user.id),
        role=user.role.value,
        path=user.path,
    )

    return Token(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=expires_in,
    )


@router.post("/refresh", response_model=Token, summary="Rotate Access Token")
async def refresh_token(
    payload: RefreshTokenRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> Token:
    """Exchanges an active refresh token for a new token pair."""
    try:
        decoded = jwt.decode(
            payload.refresh_token, settings.SECRET_KEY, algorithms=[ALGORITHM]
        )
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid refresh token",
        )

    if decoded.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Token provided is not a refresh token",
        )

    jti = decoded.get("jti")
    if not jti or await is_token_blacklisted(redis, jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Refresh token has expired or been revoked",
        )

    user_id = decoded.get("sub")
    user = await db.scalar(select(User).where(User.id == user_id))
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User account no longer active",
        )

    # Invalidate used refresh token to prevent reuse attacks
    exp = decoded.get("exp", 0)
    ttl = max(int(exp - time.time()), 1)
    await blacklist_token(redis, jti, ttl)

    # Issue new token pair
    access_token, new_refresh_token, expires_in = create_token_pair(
        subject=str(user.id),
        role=user.role.value,
        path=user.path,
    )

    return Token(
        access_token=access_token,
        refresh_token=new_refresh_token,
        token_type="bearer",
        expires_in=expires_in,
    )


@router.post(
    "/reset-password-with-code",
    status_code=status.HTTP_200_OK,
    summary="Self-Service Reset via Onboarding Employee Code",
)
async def reset_password_with_code(
    payload: ResetPasswordWithCodeRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    """
    Direct self-service password reset.
    Validates identity using the unique Employee Code assigned during onboarding.
    Requires no email or phone verification.
    """
    stmt = select(User).where(
        User.email == payload.email,
        User.employee_code == payload.employee_code.strip(),
    )
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Invalid verification: Email and Employee Code combination not found",
        )

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User profile is deactivated. Contact HR or administrator.",
        )

    # Apply new password
    user.password_hash = get_password_hash(payload.new_password)
    await db.commit()

    return {
        "detail": f"Password for {user.email} (Employee: {user.employee_code}) has been reset successfully. Please log in with your new password."
    }


@router.post(
    "/change-password",
    status_code=status.HTTP_200_OK,
    summary="Change Password (Authenticated User)",
)
async def change_password(
    payload: ChangePasswordRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    """Authenticated users can change their password by supplying the old password."""
    if not verify_password(payload.old_password, current_user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password verification failed",
        )

    current_user.password_hash = get_password_hash(payload.new_password)
    await db.commit()
    return {"detail": "Password updated successfully"}


@router.post(
    "/reset-password/{user_id}",
    status_code=status.HTTP_200_OK,
    summary="Admin Override Reset",
)
async def admin_reset_password(
    user_id: str,
    payload: AdminResetPasswordRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    """Superadmins and Admins can force-reset a subordinate user's password."""
    if current_user.role not in [RoleEnum.SUPERADMIN, RoleEnum.ADMIN]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Privilege level insufficient to reset user passwords",
        )

    result = await db.execute(select(User).where(User.id == user_id))
    target_user = result.scalar_one_or_none()

    if not target_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Target user not found"
        )

    if (
        current_user.role == RoleEnum.ADMIN
        and target_user.role == RoleEnum.SUPERADMIN
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admins cannot modify Superadmin credentials",
        )

    target_user.password_hash = get_password_hash(payload.new_password)
    await db.commit()
    return {"detail": f"Password for {target_user.email} reset successfully"}


@router.post("/logout", status_code=status.HTTP_200_OK, summary="Revoke Session")
async def logout(
    current_user: Annotated[User, Depends(get_current_user)],
    token: Annotated[str, Depends(oauth2_scheme)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> dict[str, str]:
    """Revokes active JWT by persisting its unique identifier (JTI) in Redis."""
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[ALGORITHM]
        )
        jti = payload.get("jti")
        exp = payload.get("exp", 0)

        ttl = max(int(exp - time.time()), 1)
        if jti:
            await blacklist_token(redis, jti, ttl)
    except JWTError:
        pass

    return {"detail": "Successfully logged out"}