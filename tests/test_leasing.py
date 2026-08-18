from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.assignment.auth import create_auditor
from src.assignment.leasing import (
    LeasingError, checkout_next_item, needs_overlap, release_assignment,
    required_reviews, submit_response, sweep_expired_leases,
)
from src.db.base import Base, utcnow
from src.db.models import AuditItem, Legislation
from src.assignment.leasing import required_reviews


@pytest.fixture()
def db(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    session = factory()
    yield session
    session.close()


def _seed_items(db, n=10, spec_key="metadata"):
    db.add(Legislation(id="10/2000", leg_name="x", leg_number="10", year="2000", leg_type="law"))
    db.flush()
    for i in range(n):
        db.add(AuditItem(spec_key=spec_key, unit="legislation", legislation_id="10/2000",
                          article_number=str(i)))
    db.commit()


def _auditor(db, name="a1"):
    a = create_auditor(db, name, "correct horse battery")
    db.commit()
    return a


# --- overlap determinism -------------------------------------------------
def test_needs_overlap_is_deterministic() -> None:
    assert needs_overlap("k1", 0.2, seed=1) == needs_overlap("k1", 0.2, seed=1)


def test_needs_overlap_zero_fraction_never_true() -> None:
    assert needs_overlap("anything", 0.0, seed=1) is False


def test_needs_overlap_roughly_matches_fraction() -> None:
    hits = sum(needs_overlap(f"item-{i}", 0.2, seed=1) for i in range(2000))
    assert 300 < hits < 500     # ~20% of 2000, generous tolerance


# --- basic checkout --------------------------------------------------------
def test_checkout_returns_an_item(db) -> None:
    _seed_items(db, n=5)
    a1 = _auditor(db)
    assignment = checkout_next_item(db, a1.id, "metadata", overlap_fraction=0.0, seed=1, lease_minutes=45)
    assert assignment is not None
    assert assignment.status == "leased"


def test_checkout_empty_pool_returns_none(db) -> None:
    a1 = _auditor(db)
    assert checkout_next_item(db, a1.id, "metadata", 0.0, seed=1, lease_minutes=45) is None


def test_same_auditor_never_gets_same_item_twice(db) -> None:
    _seed_items(db, n=1)
    a1 = _auditor(db)
    first = checkout_next_item(db, a1.id, "metadata", 0.0, seed=1, lease_minutes=45)
    assert first is not None
    second = checkout_next_item(db, a1.id, "metadata", 0.0, seed=1, lease_minutes=45)
    assert second is None    # only item is already actively leased to them


# --- exclusivity while leased ---------------------------------------------
def test_active_lease_blocks_other_auditors(db) -> None:
    _seed_items(db, n=1)
    a1, a2 = _auditor(db, "a1"), _auditor(db, "a2")
    checkout_next_item(db, a1.id, "metadata", 0.0, seed=1, lease_minutes=45)
    assert checkout_next_item(db, a2.id, "metadata", 0.0, seed=1, lease_minutes=45) is None


def test_expired_lease_returns_item_to_pool_even_without_sweep(db) -> None:
    _seed_items(db, n=1)
    a1, a2 = _auditor(db, "a1"), _auditor(db, "a2")
    assignment = checkout_next_item(db, a1.id, "metadata", 0.0, seed=1, lease_minutes=45)
    assignment.lease_expires_at = utcnow() - timedelta(minutes=1)   # force expiry
    db.commit()

    second = checkout_next_item(db, a2.id, "metadata", 0.0, seed=1, lease_minutes=45)
    assert second is not None
    assert second.auditor_id == a2.id


def test_sweep_marks_stale_leases_expired(db) -> None:
    _seed_items(db, n=1)
    a1 = _auditor(db)
    assignment = checkout_next_item(db, a1.id, "metadata", 0.0, seed=1, lease_minutes=45)
    assignment.lease_expires_at = utcnow() - timedelta(minutes=1)
    db.commit()

    count = sweep_expired_leases(db)
    assert count == 1
    db.refresh(assignment)
    assert assignment.status == "expired"


# --- overlap requiring two distinct auditors -------------------------------
def test_overlap_item_can_be_checked_out_by_two_different_auditors(db) -> None:
    _seed_items(db, n=1)
    a1, a2 = _auditor(db, "a1"), _auditor(db, "a2")

    first = checkout_next_item(db, a1.id, "metadata", overlap_fraction=1.0, seed=1, lease_minutes=45)
    submit_response(db, first.id, a1.id, "metadata", spec_version=1, answers={"status": "correct"})
    db.commit()

    second = checkout_next_item(db, a2.id, "metadata", overlap_fraction=1.0, seed=1, lease_minutes=45)
    assert second is not None
    assert second.item_id == first.item_id


def test_overlap_item_closes_after_two_responses(db) -> None:
    _seed_items(db, n=1)
    a1, a2, a3 = _auditor(db, "a1"), _auditor(db, "a2"), _auditor(db, "a3")

    for a in (a1, a2):
        assignment = checkout_next_item(db, a.id, "metadata", 1.0, seed=1, lease_minutes=45)
        submit_response(db, assignment.id, a.id, "metadata", spec_version=1, answers={"status": "correct"})
        db.commit()

    assert checkout_next_item(db, a3.id, "metadata", 1.0, seed=1, lease_minutes=45) is None


# --- submit / release -------------------------------------------------------
def test_submit_response_wrong_auditor_rejected(db) -> None:
    _seed_items(db, n=1)
    a1, a2 = _auditor(db, "a1"), _auditor(db, "a2")
    assignment = checkout_next_item(db, a1.id, "metadata", 0.0, seed=1, lease_minutes=45)
    with pytest.raises(LeasingError, match="does not belong to you"):
        submit_response(db, assignment.id, a2.id, "metadata", 1, {"status": "correct"})


def test_submit_after_expiry_rejected_and_marks_expired(db) -> None:
    _seed_items(db, n=1)
    a1 = _auditor(db)
    assignment = checkout_next_item(db, a1.id, "metadata", 0.0, seed=1, lease_minutes=45)
    assignment.lease_expires_at = utcnow() - timedelta(minutes=1)
    db.commit()

    with pytest.raises(LeasingError, match="expired"):
        submit_response(db, assignment.id, a1.id, "metadata", 1, {"status": "correct"})
    db.refresh(assignment)
    assert assignment.status == "expired"


def test_release_returns_item_to_pool_immediately(db) -> None:
    _seed_items(db, n=1)
    a1, a2 = _auditor(db, "a1"), _auditor(db, "a2")
    assignment = checkout_next_item(db, a1.id, "metadata", 0.0, seed=1, lease_minutes=45)
    release_assignment(db, assignment.id, a1.id)
    db.commit()

    second = checkout_next_item(db, a2.id, "metadata", 0.0, seed=1, lease_minutes=45)
    assert second is not None


def test_release_wrong_auditor_rejected(db) -> None:
    _seed_items(db, n=1)
    a1, a2 = _auditor(db, "a1"), _auditor(db, "a2")
    assignment = checkout_next_item(db, a1.id, "metadata", 0.0, seed=1, lease_minutes=45)
    with pytest.raises(LeasingError, match="does not belong to you"):
        release_assignment(db, assignment.id, a2.id)


def test_required_reviews_matches_overlap_decision(db) -> None:
    _seed_items(db, n=1)
    item = db.query(AuditItem).one()
    n = required_reviews(item, overlap_fraction=1.0, seed=1)
    assert n == 2
    n0 = required_reviews(item, overlap_fraction=0.0, seed=1)
    assert n0 == 1


# --- orphan_suspected forces double review ---------------------------------
def test_orphan_suspected_forces_two_reviews_even_with_zero_overlap(db) -> None:
    db.add(Legislation(id="10/2000", leg_name="x", leg_number="10", year="2000", leg_type="law"))
    db.flush()
    item = AuditItem(spec_key="reflection", unit="article", legislation_id="10/2000",
                      article_number="7", match_status="orphan_suspected")
    db.add(item)
    db.commit()

    n = required_reviews(item, overlap_fraction=0.0, seed=1)
    assert n == 2


def test_matched_status_does_not_force_extra_review(db) -> None:
    db.add(Legislation(id="10/2000", leg_name="x", leg_number="10", year="2000", leg_type="law"))
    db.flush()
    item = AuditItem(spec_key="reflection", unit="article", legislation_id="10/2000",
                      article_number="7", match_status="matched")
    db.add(item)
    db.commit()

    n = required_reviews(item, overlap_fraction=0.0, seed=1)
    assert n == 1


def test_orphan_suspected_item_checked_out_by_two_different_auditors(db) -> None:
    db.add(Legislation(id="10/2000", leg_name="x", leg_number="10", year="2000", leg_type="law"))
    db.flush()
    db.add(AuditItem(spec_key="reflection", unit="article", legislation_id="10/2000",
                      article_number="7", match_status="orphan_suspected"))
    db.commit()

    a1, a2 = _auditor(db, "a1"), _auditor(db, "a2")

    # overlap_fraction=0.0 — under normal circumstances this item would
    # need only ONE review; orphan_suspected must override that.
    first = checkout_next_item(db, a1.id, "reflection", overlap_fraction=0.0, seed=1, lease_minutes=45)
    submit_response(db, first.id, a1.id, "reflection", spec_version=1, answers={"applied": "correct"})
    db.commit()

    second = checkout_next_item(db, a2.id, "reflection", overlap_fraction=0.0, seed=1, lease_minutes=45)
    assert second is not None
    assert second.item_id == first.item_id


def test_orphan_suspected_closes_after_two_responses_not_one(db) -> None:
    db.add(Legislation(id="10/2000", leg_name="x", leg_number="10", year="2000", leg_type="law"))
    db.flush()
    db.add(AuditItem(spec_key="reflection", unit="article", legislation_id="10/2000",
                      article_number="7", match_status="orphan_suspected"))
    db.commit()

    a1, a2, a3 = _auditor(db, "a1"), _auditor(db, "a2"), _auditor(db, "a3")
    for a in (a1, a2):
        assignment = checkout_next_item(db, a.id, "reflection", 0.0, seed=1, lease_minutes=45)
        submit_response(db, assignment.id, a.id, "reflection", 1, {"applied": "correct"})
        db.commit()

    assert checkout_next_item(db, a3.id, "reflection", 0.0, seed=1, lease_minutes=45) is None
