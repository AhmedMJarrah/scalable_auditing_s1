"""
Auditor authentication.

bcrypt via passlib, one account per person — no shared logins (see project
README: shared accounts silently invalidate inter-auditor agreement and
golden-set scoring, since both depend on knowing who actually answered).
"""

from __future__ import annotations

import bcrypt
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.logging_setup import get_logger
from src.db.base import utcnow
from src.db.models import Auditor

log = get_logger(__name__)

MIN_PASSWORD_BYTES = 8
# bcrypt has a hard 72-byte input limit; anything past that is silently
# ignored by some implementations. Reject explicitly instead — a password
# that stops working after the user adds a few extra characters is a
# miserable bug to trace back to this.
MAX_PASSWORD_BYTES = 72


class AuthError(Exception):
    """Raised for account-creation and login problems — never leaks which
    part (username vs password) was wrong on login, to avoid confirming a
    username exists to someone probing logins."""


def hash_password(password: str) -> str:
    raw = password.encode("utf-8")
    if len(raw) < MIN_PASSWORD_BYTES:
        raise AuthError(f"password must be at least {MIN_PASSWORD_BYTES} characters")
    if len(raw) > MAX_PASSWORD_BYTES:
        raise AuthError(f"password must be at most {MAX_PASSWORD_BYTES} bytes (bcrypt limit)")
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except (ValueError, TypeError):
        # malformed hash in the DB — treat as a failed login, not a crash
        return False


def create_auditor(
    db: Session,
    username: str,
    password: str,
    role: str = "auditor",
    display_name_ar: str | None = None,
) -> Auditor:
    username = username.strip()
    if not username:
        raise AuthError("username cannot be empty")

    existing = db.execute(
        select(Auditor).where(Auditor.username == username)
    ).scalar_one_or_none()
    if existing is not None:
        raise AuthError(f"username {username!r} is already taken")

    auditor = Auditor(
        username=username, password_hash=hash_password(password),
        role=role, display_name_ar=display_name_ar,
    )
    db.add(auditor)
    db.flush()
    log.info("auditor.created", extra={"username": username, "role": role})
    return auditor


def authenticate(db: Session, username: str, password: str) -> Auditor | None:
    """Returns the Auditor on success, None on any failure — wrong username,
    wrong password, and a disabled account all look identical to the caller,
    deliberately, so a login form cannot be used to enumerate usernames."""
    auditor = db.execute(
        select(Auditor).where(Auditor.username == username.strip())
    ).scalar_one_or_none()

    if auditor is None or not auditor.is_active:
        log.warning("auditor.login_failed", extra={"username": username})
        return None
    if not verify_password(password, auditor.password_hash):
        log.warning("auditor.login_failed", extra={"username": username})
        return None

    auditor.last_login_at = utcnow()
    db.flush()
    log.info("auditor.login_ok", extra={"username": username})
    return auditor
