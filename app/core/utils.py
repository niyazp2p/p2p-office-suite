import secrets
from datetime import datetime


def generate_employee_code() -> str:
    """Generates an organizational employee code, e.g., 'P2P-2026-7842'."""
    year = datetime.now().year
    random_digits = "".join([str(secrets.randbelow(10)) for _ in range(4)])
    return f"P2P-{year}-{random_digits}"