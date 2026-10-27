"""Account and authentication rules for DocMate v1.18."""

from __future__ import annotations

import hashlib


ADMIN_EMAILS = frozenset(
    {
        "krishna@docmate.store",
        "krishna@visualbusinessretail.com",
        "tillsupport@visualbusinessretail.com",
    }
)
DEVELOPER_EMAILS = frozenset({"krishna@docmate.store"})


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def hash_password(password: str) -> str:
    return hashlib.sha256((password or "").encode("utf-8")).hexdigest()


def is_admin_user(email: str) -> bool:
    return normalize_email(email) in ADMIN_EMAILS


def is_developer_user(email: str) -> bool:
    return normalize_email(email) in DEVELOPER_EMAILS


def validate_user_login(email: str, password: str) -> bool:
    """Validate the legacy local/POC accounts without changing their rules."""
    email = normalize_email(email)
    password = (password or "").strip()
    if not email or not password:
        return False

    default_users = {
        "user@docmate.store": "user",
        "admin@docmate.store": "admin",
        "krishna@docmate.store": "Vbr2025#",
    }
    if email in ("krishna@visualbusinessretail.com", "tillsupport@visualbusinessretail.com"):
        return password == "admin"
    if email == "krishna@docmate.store":
        return password in {"Vbr2025#", "vbr2025#", "VBR2025#"}
    return default_users.get(email) == password

