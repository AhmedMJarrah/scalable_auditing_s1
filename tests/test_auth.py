from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.assignment.auth import AuthError, authenticate, create_auditor, hash_password, verify_password
from src.db.base import Base


@pytest.fixture()
def db(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    session = factory()
    yield session
    session.close()


def test_password_hash_verifies() -> None:
    h = hash_password("correct horse battery")
    assert verify_password("correct horse battery", h)
    assert not verify_password("wrong password", h)


def test_short_password_rejected() -> None:
    with pytest.raises(AuthError, match="at least 8"):
        hash_password("short")


def test_create_and_authenticate(db) -> None:
    create_auditor(db, "ahmad_j", "correct horse battery")
    db.commit()

    auditor = authenticate(db, "ahmad_j", "correct horse battery")
    assert auditor is not None
    assert auditor.username == "ahmad_j"
    assert auditor.last_login_at is not None


def test_wrong_password_fails(db) -> None:
    create_auditor(db, "ahmad_j", "correct horse battery")
    db.commit()
    assert authenticate(db, "ahmad_j", "totally wrong pw") is None


def test_unknown_username_fails(db) -> None:
    assert authenticate(db, "nobody", "whatever12") is None


def test_duplicate_username_rejected(db) -> None:
    create_auditor(db, "ahmad_j", "correct horse battery")
    db.commit()
    with pytest.raises(AuthError, match="already taken"):
        create_auditor(db, "ahmad_j", "another password")


def test_inactive_account_cannot_login(db) -> None:
    auditor = create_auditor(db, "ahmad_j", "correct horse battery")
    auditor.is_active = False
    db.commit()
    assert authenticate(db, "ahmad_j", "correct horse battery") is None


def test_role_defaults_to_auditor(db) -> None:
    a = create_auditor(db, "ahmad_j", "correct horse battery")
    assert a.role == "auditor"


def test_admin_role_can_be_set(db) -> None:
    a = create_auditor(db, "admin1", "correct horse battery", role="admin")
    assert a.role == "admin"


def test_arabic_display_name_stored(db) -> None:
    a = create_auditor(db, "ahmad_j", "correct horse battery", display_name_ar="أحمد جراح")
    db.commit()
    assert a.display_name_ar == "أحمد جراح"
