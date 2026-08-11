"""
Declarative base for the auditing pipeline schema.

Kept separate from models.py so Alembic's env.py can import just the base
and metadata without pulling in every model eagerly (models.py still needs
importing once, for its side effect of registering tables on Base.metadata —
see migrations/env.py).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import DeclarativeBase


def new_id() -> str:
    """UUID4 hex, no dashes — short, URL-safe, sorts randomly (fine here;
    we are not using it as a clustering key)."""
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass
