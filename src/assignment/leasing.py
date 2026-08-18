"""
Item leasing — the mechanism that lets many volunteers share one pool of
audit items safely.

Core rules:
    - An item under an active (non-expired) lease is invisible to everyone
      except the auditor holding it.
    - An auditor never sees an item they've already answered, or already
      have an active lease on.
    - Each item needs `required_reviews` submitted responses before it is
      considered done — 1 normally, 2 for the deliberate overlap sample
      (see needs_overlap) that lets agreement be measured.
    - A lease past its expiry is treated as available again immediately,
      even before sweep_expired_leases() has run — submit_response() and
      checkout both check the timestamp directly, not just the stored
      status, so a half-finished session can never permanently strand an
      item.
"""

from __future__ import annotations

import random
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.core.logging_setup import get_logger
from src.core.spec import AuditSpec
from src.db.base import utcnow
from src.db.models import Assignment, AuditItem, Response

log = get_logger(__name__)


class LeasingError(Exception):
    """Raised for invalid assignment operations — wrong auditor, wrong
    state, expired lease. Always safe to show to the auditor; never leaks
    other auditors' identities or data."""


def needs_overlap(identity_key: str, overlap_fraction: float, seed: int) -> bool:
    """
    Deterministic per-item decision: does this item require two independent
    reviews? Seeded on (seed, identity_key), so the same item always gets
    the same answer regardless of who checks it out first or how many times
    this is called — it is a property of the item, not of any one request.
    """
    if overlap_fraction <= 0:
        return False
    rng = random.Random(f"{seed}:{identity_key}")
    return rng.random() < overlap_fraction


def required_reviews(item: AuditItem, overlap_fraction: float, seed: int) -> int:
    if item.match_status == "orphan_suspected":
        # These are exactly the items most likely to be silently wrong —
        # a low-confidence before/after match, not a normal defect. Force
        # a second independent review regardless of the usual overlap
        # draw, rather than leaving it to chance whether this one lands
        # in the random overlap_fraction sample.
        return 2
    return 2 if needs_overlap(item.identity_key, overlap_fraction, seed) else 1


def _has_active_lease(db: Session, item_id: str, now) -> bool:
    return db.execute(
        select(Assignment.id)
        .where(
            Assignment.item_id == item_id,
            Assignment.status == "leased",
            Assignment.lease_expires_at > now,
        )
        .limit(1)
    ).first() is not None


def checkout_next_item(
    db: Session,
    auditor_id: str,
    spec_key: str,
    overlap_fraction: float,
    seed: int,
    lease_minutes: int,
) -> Assignment | None:
    """Lease the next eligible item for this auditor, or None if the pool
    is exhausted for them right now (everything is either done, or
    currently leased by someone else)."""
    now = utcnow()

    already_touched = set(db.execute(
        select(Response.item_id).where(Response.auditor_id == auditor_id)
    ).scalars())
    already_touched |= set(db.execute(
        select(Assignment.item_id).where(
            Assignment.auditor_id == auditor_id,
            Assignment.status == "leased",
            Assignment.lease_expires_at > now,
        )
    ).scalars())

    response_counts = dict(db.execute(
        select(Response.item_id, func.count(Response.id)).group_by(Response.item_id)
    ).all())

    items = db.execute(
        select(AuditItem)
        .where(AuditItem.spec_key == spec_key)
        .order_by(AuditItem.sampled_at, AuditItem.id)
    ).scalars().all()

    for item in items:
        if item.id in already_touched:
            continue
        needed = required_reviews(item, overlap_fraction, seed)
        if response_counts.get(item.id, 0) >= needed:
            continue
        if _has_active_lease(db, item.id, now):
            continue

        assignment = Assignment(
            item_id=item.id, auditor_id=auditor_id, status="leased",
            leased_at=now, lease_expires_at=now + timedelta(minutes=lease_minutes),
            overlap_group_id=item.id if needed == 2 else None,
        )
        db.add(assignment)
        db.flush()
        log.info("assignment.leased", extra={
            "item_id": item.id, "auditor_id": auditor_id, "spec_key": spec_key,
            "expires_at": assignment.lease_expires_at.isoformat(),
        })
        return assignment

    log.info("assignment.none_available", extra={"spec_key": spec_key, "auditor_id": auditor_id})
    return None


def checkout_next_item_for_spec(
    db: Session, auditor_id: str, spec: AuditSpec, seed: int, lease_minutes: int,
) -> Assignment | None:
    """Convenience wrapper — pulls overlap_fraction from the spec so callers
    don't have to reach into spec.sampling themselves."""
    return checkout_next_item(
        db, auditor_id, spec.key, spec.sampling.overlap_fraction, seed, lease_minutes,
    )


def submit_response(
    db: Session,
    assignment_id: str,
    auditor_id: str,
    spec_key: str,
    spec_version: int,
    answers: dict,
    duration_seconds: int | None = None,
) -> Response:
    assignment = db.get(Assignment, assignment_id)
    if assignment is None:
        raise LeasingError("assignment not found")
    if assignment.auditor_id != auditor_id:
        raise LeasingError("this assignment does not belong to you")
    if assignment.status != "leased":
        raise LeasingError(f"assignment is {assignment.status!r}, not leased")
    if assignment.lease_expires_at <= utcnow():
        assignment.status = "expired"
        db.flush()
        raise LeasingError("your lease on this item has expired — it has returned to the pool")

    response = Response(
        item_id=assignment.item_id, auditor_id=auditor_id, spec_key=spec_key,
        spec_version=spec_version, answers=answers, duration_seconds=duration_seconds,
    )
    db.add(response)
    assignment.status = "submitted"
    db.flush()
    log.info("response.submitted", extra={
        "item_id": assignment.item_id, "auditor_id": auditor_id, "spec_key": spec_key,
    })
    return response


def release_assignment(db: Session, assignment_id: str, auditor_id: str) -> None:
    """An auditor voluntarily giving an item back before their lease expires."""
    assignment = db.get(Assignment, assignment_id)
    if assignment is None:
        raise LeasingError("assignment not found")
    if assignment.auditor_id != auditor_id:
        raise LeasingError("this assignment does not belong to you")
    if assignment.status != "leased":
        raise LeasingError(f"assignment is {assignment.status!r}, cannot release")

    assignment.status = "released"
    db.flush()
    log.info("assignment.released", extra={"item_id": assignment.item_id, "auditor_id": auditor_id})


def sweep_expired_leases(db: Session) -> int:
    """
    Flip stale 'leased' rows to 'expired' for accurate reporting. Not
    required for correctness — checkout and submit both check the
    timestamp directly — but without this, an admin dashboard counting
    "currently leased" would overstate how many items are actually stuck.
    Safe to run on a schedule or on demand.
    """
    now = utcnow()
    stale = db.execute(
        select(Assignment).where(
            Assignment.status == "leased", Assignment.lease_expires_at <= now,
        )
    ).scalars().all()
    for a in stale:
        a.status = "expired"
    if stale:
        db.flush()
    log.info("assignments.swept", extra={"count": len(stale)})
    return len(stale)
