import asyncio
from pathlib import Path
import sys

# Ensure root directory is on Python path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select
from app.core.database import AsyncSessionLocal
from app.core.security import get_password_hash
from app.models.user import RoleEnum, User


async def seed_root_superadmin() -> None:
    email = "mohit@p2psoftwares.com"
    password = "Mohit@SA123"
    emp_code = "P2P-SA-0001"

    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(User).where(
                (User.role == RoleEnum.SUPERADMIN) | (User.email == email)
            )
        )
        existing_superadmin = result.scalar_one_or_none()

        if existing_superadmin:
            existing_superadmin.email = email
            existing_superadmin.employee_code = emp_code
            existing_superadmin.password_hash = get_password_hash(password)
            existing_superadmin.first_name = "Mohit"
            existing_superadmin.last_name = "Superadmin"
            existing_superadmin.role = RoleEnum.SUPERADMIN
            existing_superadmin.path = "/root/"
            existing_superadmin.is_active = True
            await session.commit()
            print("Superadmin credentials updated successfully!")
            print(f"Email: {email}")
            print(f"Employee Code: {emp_code}")
            print(f"Password: {password}")
            return

        root_superadmin = User(
            email=email,
            employee_code=emp_code,
            password_hash=get_password_hash(password),
            first_name="Mohit",
            last_name="Superadmin",
            role=RoleEnum.SUPERADMIN,
            department="Executive Leadership",
            designation="Superadmin",
            manager_id=None,
            path="/root/",
            is_active=True,
        )

        session.add(root_superadmin)
        await session.commit()
        print("Root Superadmin initialized successfully!")
        print(f"Email: {email}")
        print(f"Employee Code: {emp_code}")
        print(f"Password: {password}")


if __name__ == "__main__":
    asyncio.run(seed_root_superadmin())