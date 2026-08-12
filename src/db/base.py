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
    """
    Naive datetime, but always UTC by convention — deliberately, not an
    oversight. SQLite has no real timezone-aware datetime type: a value
    written as aware comes back naive after a round trip through the
    database (e.g. right after a session-expiring commit re-fetches a row).
    Comparing that against a still-aware datetime.now(timezone.utc) raises
    TypeError. Keeping every stored timestamp naive-but-UTC means every
    comparison in this codebase is naive-vs-naive and always correct,
    instead of correct only until the first round trip.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass
