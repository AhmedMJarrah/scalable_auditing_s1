"""
Tests for the database schema.

Run against a throwaway SQLite file per test (pytest tmp_path), never the
project's real data/s1.db.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from src.db.base import Base
from src.db import models

ARABIC = "قانون التجارة لسنة 2000"


@pytest.fixture()
def db(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", future=True)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON;")
        cur.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    session = factory()
    yield session
    session.close()


def _legislation(id_="10/2000", **kw) -> models.Legislation:
    defaults = dict(leg_name=ARABIC, leg_number="10", year="2000", leg_type="law")
    defaults.update(kw)
    return models.Legislation(id=id_, **defaults)


def _auditor(username="a1") -> models.Auditor:
    return models.Auditor(username=username, password_hash="hash", role="auditor")


# --- basic shape --------------------------------------------------------
def test_tables_created(db: Session) -> None:
    names = set(Base.metadata.tables.keys())
    assert names == {
        "legislation", "auditors", "audit_items", "assignments",
        "responses", "golden_answers", "sync_log",
    }


def test_arabic_text_round_trips(db: Session) -> None:
    db.add(_legislation())
    db.commit()
    back = db.execute(select(models.Legislation)).scalar_one()
    assert back.leg_name == ARABIC


def test_json_column_round_trips(db: Session) -> None:
    db.add(_legislation(source_meta={"Magazine_Number": "4500", "note": ARABIC}))
    db.commit()
    back = db.execute(select(models.Legislation)).scalar_one()
    assert back.source_meta["note"] == ARABIC


# --- amendment self-reference -------------------------------------------
def test_amendment_links_to_base(db: Session) -> None:
    base = _legislation()
    amendment = _legislation(id_="5/2003", is_amendment=True,
                              amendment_of_id=base.id, sequence_index=0)
    db.add_all([base, amendment])
    db.commit()

    fetched_base = db.execute(
        select(models.Legislation).where(models.Legislation.id == base.id)
    ).scalar_one()
    assert [a.id for a in fetched_base.amendments] == [amendment.id]


# --- audit items ----------------------------------------------------------
def test_audit_item_disambiguates_legislation_vs_amendment_fk(db: Session) -> None:
    """The bug caught during development: AuditItem has two FKs into
    legislation (legislation_id, amendment_id) and the ORM must not confuse
    them when loading Legislation.items."""
    base = _legislation()
    amendment = _legislation(id_="5/2003", is_amendment=True, amendment_of_id=base.id)
    db.add_all([base, amendment])
    db.flush()

    item = models.AuditItem(
        spec_key="chain", unit="chain", legislation_id=base.id,
        amendment_id=amendment.id,
    )
    db.add(item)
    db.commit()

    fetched_base = db.execute(
        select(models.Legislation).where(models.Legislation.id == base.id)
    ).scalar_one()
    assert [i.id for i in fetched_base.items] == [item.id]

    fetched_amendment = db.execute(
        select(models.Legislation).where(models.Legislation.id == amendment.id)
    ).scalar_one()
    assert fetched_amendment.items == []   # amendment_id is not legislation_id


def test_duplicate_item_identity_rejected(db: Session) -> None:
    db.add(_legislation())
    db.commit()
    db.add(models.AuditItem(spec_key="metadata", unit="legislation", legislation_id="10/2000"))
    db.commit()
    db.add(models.AuditItem(spec_key="metadata", unit="legislation", legislation_id="10/2000"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# --- assignments / lease model -------------------------------------------
def test_assignment_lease_round_trip(db: Session) -> None:
    db.add(_legislation())
    db.add(_auditor())
    db.commit()

    item = models.AuditItem(spec_key="metadata", unit="legislation", legislation_id="10/2000")
    db.add(item)
    db.flush()

    auditor = db.execute(select(models.Auditor)).scalar_one()
    lease = models.Assignment(
        item_id=item.id, auditor_id=auditor.id,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=45),
    )
    db.add(lease)
    db.commit()

    fetched = db.execute(select(models.Assignment)).scalar_one()
    assert fetched.status == "leased"
    assert fetched.auditor.username == "a1"


# --- responses --------------------------------------------------------
def test_one_response_per_item_per_auditor(db: Session) -> None:
    db.add(_legislation())
    db.add(_auditor())
    db.commit()
    item = models.AuditItem(spec_key="metadata", unit="legislation", legislation_id="10/2000")
    db.add(item)
    db.flush()
    auditor = db.execute(select(models.Auditor)).scalar_one()

    db.add(models.Response(
        item_id=item.id, auditor_id=auditor.id, spec_key="metadata",
        spec_version=1, answers={"status": "correct"},
    ))
    db.commit()

    db.add(models.Response(
        item_id=item.id, auditor_id=auditor.id, spec_key="metadata",
        spec_version=1, answers={"status": "incorrect"},
    ))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_response_answers_hold_arabic_free_text(db: Session) -> None:
    db.add(_legislation())
    db.add(_auditor())
    db.commit()
    item = models.AuditItem(spec_key="chain", unit="chain", legislation_id="10/2000")
    db.add(item)
    db.flush()
    auditor = db.execute(select(models.Auditor)).scalar_one()

    db.add(models.Response(
        item_id=item.id, auditor_id=auditor.id, spec_key="chain", spec_version=1,
        answers={"note": ARABIC, "defect_types": ["taadil_mafqud"]},
    ))
    db.commit()

    back = db.execute(select(models.Response)).scalar_one()
    assert back.answers["note"] == ARABIC
    assert back.answers["defect_types"] == ["taadil_mafqud"]


# --- foreign key enforcement --------------------------------------------
def test_orphan_audit_item_rejected(db: Session) -> None:
    db.add(models.AuditItem(spec_key="metadata", unit="legislation", legislation_id="does/not-exist"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


# --- golden answers -------------------------------------------------------
def test_golden_answer_links_one_to_one(db: Session) -> None:
    db.add(_legislation())
    db.commit()
    item = models.AuditItem(
        spec_key="metadata", unit="legislation", legislation_id="10/2000", is_golden=True,
    )
    db.add(item)
    db.flush()
    db.add(models.GoldenAnswer(item_id=item.id, answers={"status": "correct"}))
    db.commit()

    fetched = db.execute(select(models.AuditItem)).scalar_one()
    assert fetched.golden_answer.answers["status"] == "correct"
