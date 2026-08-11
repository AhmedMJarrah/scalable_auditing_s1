"""
Database schema.

Design rule this schema exists to enforce: adding a new audit type is a YAML
file (src/core/spec.py), never a migration. Every table here is generic —
keyed by spec_key (a string matching a spec's `key`), never by a
type-specific column. `audit_items.answers` and `responses.answers` hold a
JSON payload whose shape is validated against the spec at write time in the
scoring layer (step 4+), not by the database.

Identity:
    legislation.id is the natural key agreed for laws and reused for
    bylaws: f"{number}/{year}" — e.g. "10/2000". Stable across ingestion
    re-runs, so re-ingesting the same source file is idempotent.

Concurrency:
    assignments implements the lease model — an auditor checks an item out,
    it is theirs until lease_expires_at, then it returns to the queue. This
    is what makes "many volunteers, one pool of items" safe without a
    heavier locking scheme.

Reliability:
    audit_items.is_golden + golden_answers is the check against an auditor
    clicking "correct" on everything. assignments.overlap_group_id links the
    (small, deliberate) set of items assigned to two auditors, so agreement
    can be computed per audit type.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON, CheckConstraint, ForeignKey, Index, String, UniqueConstraint, event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.db.base import Base, new_id, utcnow

LEG_TYPES = ("law", "bylaw")
ITEM_UNITS = ("legislation", "chain", "article")
AUDITOR_ROLES = ("auditor", "admin")
ASSIGNMENT_STATUSES = ("leased", "submitted", "expired", "released")
SYNC_STATUSES = ("ok", "error")


def build_item_identity_key(
    spec_key: str,
    legislation_id: str,
    amendment_id: str | None = None,
    article_number: str | None = None,
) -> str:
    """
    Deterministic identity for one audit item, used as a real UNIQUE column.

    A composite UNIQUE constraint across nullable columns does not work in
    SQL: NULL is never equal to NULL, so two metadata items (which both have
    amendment_id=NULL, article_number=NULL) would compare as distinct and
    the constraint would silently allow duplicates. Collapsing the identity
    into one non-null string sidesteps that entirely. Callers (ingestion,
    sampling) must always build this the same way.
    """
    return "|".join([spec_key, legislation_id, amendment_id or "", article_number or ""])


class Legislation(Base):
    """One law or bylaw, base or amendment — both live here, distinguished
    by `is_amendment` and linked via `amendment_of_id`."""

    __tablename__ = "legislation"
    __table_args__ = (
        CheckConstraint(f"leg_type IN {LEG_TYPES}", name="ck_legislation_leg_type"),
        Index("ix_legislation_amendment_of", "amendment_of_id"),
    )

    # Natural key: f"{number}/{year}". Not a surrogate id, deliberately —
    # this lets ingestion re-runs upsert instead of duplicating.
    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    leg_name: Mapped[str] = mapped_column(String(500))
    leg_number: Mapped[str] = mapped_column(String(32))
    year: Mapped[str] = mapped_column(String(8))
    leg_type: Mapped[str] = mapped_column(String(16))

    is_amendment: Mapped[bool] = mapped_column(default=False)
    amendment_of_id: Mapped[str | None] = mapped_column(
        ForeignKey("legislation.id"), nullable=True
    )
    sequence_index: Mapped[int | None] = mapped_column(
        nullable=True, comment="0-based position within amendment_of's chain, oldest first"
    )

    # Everything from the source we are not modelling as a column yet
    # (Publication, Magazine_*, Issue_Date, ...) — kept so nothing is lost
    # on ingestion, queryable via JSON functions when actually needed.
    source_meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)

    amendments: Mapped[list["Legislation"]] = relationship(
        back_populates="base", remote_side=None,
        primaryjoin="Legislation.id==Legislation.amendment_of_id",
        foreign_keys=[amendment_of_id],
    )
    base: Mapped["Legislation | None"] = relationship(
        back_populates="amendments", remote_side=[id],
        foreign_keys=[amendment_of_id],
    )
    items: Mapped[list["AuditItem"]] = relationship(
        back_populates="legislation", foreign_keys="AuditItem.legislation_id"
    )


class Auditor(Base):
    """One account per volunteer. No shared logins — see project README."""

    __tablename__ = "auditors"
    __table_args__ = (
        CheckConstraint(f"role IN {AUDITOR_ROLES}", name="ck_auditor_role"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name_ar: Mapped[str | None] = mapped_column(String(200), nullable=True)
    role: Mapped[str] = mapped_column(String(16), default="auditor")
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(nullable=True)

    assignments: Mapped[list["Assignment"]] = relationship(back_populates="auditor")
    responses: Mapped[list["Response"]] = relationship(back_populates="auditor")


class AuditItem(Base):
    """
    One unit of work: one legislation, one amendment chain, or one article,
    depending on spec.unit for spec_key. Produced by the sampling layer
    (step 5+), consumed by assignment and scoring.
    """

    __tablename__ = "audit_items"
    __table_args__ = (
        CheckConstraint(f"unit IN {ITEM_UNITS}", name="ck_audit_items_unit"),
        Index("ix_audit_items_spec_key", "spec_key"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    spec_key: Mapped[str] = mapped_column(String(64))
    unit: Mapped[str] = mapped_column(String(16))

    # Prevents the sampler from silently duplicating an item across re-runs.
    # See build_item_identity_key() — a plain composite UNIQUE across
    # legislation_id/amendment_id/article_number does not work because
    # amendment_id and article_number are nullable and NULL != NULL in SQL.
    identity_key: Mapped[str] = mapped_column(String(300), unique=True)

    legislation_id: Mapped[str] = mapped_column(ForeignKey("legislation.id"))
    # Set for chain/reflection items pointing at a specific amendment;
    # NULL for a plain metadata or article-integrity item.
    amendment_id: Mapped[str | None] = mapped_column(
        ForeignKey("legislation.id"), nullable=True
    )
    article_number: Mapped[str | None] = mapped_column(String(16), nullable=True)

    is_golden: Mapped[bool] = mapped_column(default=False)
    sampled_at: Mapped[datetime] = mapped_column(default=utcnow)
    # Fixed seed the sampler used to draw this item — reproducibility trail.
    sample_seed: Mapped[int | None] = mapped_column(nullable=True)

    legislation: Mapped["Legislation"] = relationship(
        back_populates="items", foreign_keys=[legislation_id]
    )
    assignments: Mapped[list["Assignment"]] = relationship(back_populates="item")
    responses: Mapped[list["Response"]] = relationship(back_populates="item")
    golden_answer: Mapped["GoldenAnswer | None"] = relationship(
        back_populates="item", uselist=False
    )


@event.listens_for(AuditItem, "before_insert")
def _fill_identity_key(mapper, connection, target: AuditItem) -> None:  # noqa: ANN001
    if not target.identity_key:
        target.identity_key = build_item_identity_key(
            target.spec_key, target.legislation_id, target.amendment_id, target.article_number,
        )


class Assignment(Base):
    """
    The lease: one auditor holding one item for a bounded window. A row here
    is how two auditors are stopped from silently working the same item, and
    how a half-finished session returns to the pool instead of being lost.
    """

    __tablename__ = "assignments"
    __table_args__ = (
        CheckConstraint(f"status IN {ASSIGNMENT_STATUSES}", name="ck_assignment_status"),
        Index("ix_assignments_item_status", "item_id", "status"),
        Index("ix_assignments_auditor_status", "auditor_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    item_id: Mapped[str] = mapped_column(ForeignKey("audit_items.id"))
    auditor_id: Mapped[str] = mapped_column(ForeignKey("auditors.id"))

    status: Mapped[str] = mapped_column(String(16), default="leased")
    leased_at: Mapped[datetime] = mapped_column(default=utcnow)
    lease_expires_at: Mapped[datetime] = mapped_column()

    # Links the small set of items double-assigned on purpose, so agreement
    # (Cohen's kappa) can be computed per group at scoring time.
    overlap_group_id: Mapped[str | None] = mapped_column(String(32), nullable=True)

    item: Mapped["AuditItem"] = relationship(back_populates="assignments")
    auditor: Mapped["Auditor"] = relationship(back_populates="assignments")


class Response(Base):
    """
    One auditor's submitted answer for one item. answers is validated
    against the spec's fields by the scoring layer before being trusted —
    the database enforces shape (JSON, not-null), not spec conformance.
    """

    __tablename__ = "responses"
    __table_args__ = (
        # One submitted response per (item, auditor) — resubmission updates
        # the existing row rather than creating a second one.
        UniqueConstraint("item_id", "auditor_id", name="uq_responses_item_auditor"),
        Index("ix_responses_item", "item_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    item_id: Mapped[str] = mapped_column(ForeignKey("audit_items.id"))
    auditor_id: Mapped[str] = mapped_column(ForeignKey("auditors.id"))
    spec_key: Mapped[str] = mapped_column(String(64))
    spec_version: Mapped[int] = mapped_column(
        comment="spec_version at submission time — lets scoring detect a "
                 "response recorded against a since-changed rubric"
    )

    answers: Mapped[dict] = mapped_column(JSON)
    submitted_at: Mapped[datetime] = mapped_column(default=utcnow)
    duration_seconds: Mapped[int | None] = mapped_column(nullable=True)

    item: Mapped["AuditItem"] = relationship(back_populates="responses")
    auditor: Mapped["Auditor"] = relationship(back_populates="responses")


class GoldenAnswer(Base):
    """Known-correct answer for a golden item, used to detect an auditor
    clicking through without reading."""

    __tablename__ = "golden_answers"

    item_id: Mapped[str] = mapped_column(
        ForeignKey("audit_items.id"), primary_key=True
    )
    answers: Mapped[dict] = mapped_column(JSON)
    set_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    item: Mapped["AuditItem"] = relationship(back_populates="golden_answer")


class SyncLog(Base):
    """One row per Google Sheets sync attempt — the audit trail for the
    mirror, separate from the app log so it can be queried without grepping
    JSONL."""

    __tablename__ = "sync_log"
    __table_args__ = (
        CheckConstraint(f"status IN {SYNC_STATUSES}", name="ck_sync_log_status"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    spec_key: Mapped[str] = mapped_column(String(64))
    sheet_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16))
    rows_synced: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    started_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
