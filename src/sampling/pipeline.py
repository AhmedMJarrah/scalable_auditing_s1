"""
Shared pipeline pieces used by both scripts:

    run_sampling.py    the fixed 100-law + 100-bylaw sample per audit type,
                        for the accuracy / data-quality report. Runs once,
                        inserts everything immediately.

    release_batch.py   the full population, released to volunteers in
                        admin-controlled chunks — for "review everything",
                        especially reflection, without dumping the entire
                        backlog on volunteers at once.

Both draw from the same ingestion and item-building logic; they differ only
in how much of the population they ask sample_population() for, and how
much of the result they insert per run.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.logging_setup import get_logger
from src.core.spec import AuditSpec
from src.db import models
from src.ingest.reflection_source import parse_legislation
from src.ingest.synthetic import generate_legislation
from src.sampling.item_builder import (
    BuildResult, ItemCandidate, build_article_integrity_items,
    build_chain_items, build_metadata_items, build_reflection_items,
)
from src.sampling.legislation_sampler import LegislationRecord, sample_population

log = get_logger(__name__)

SYNTHETIC_COUNT_PER_TYPE = 250   # > 100 so the sampler exercises real drawing


def load_population(seed: int) -> tuple[
    list[LegislationRecord], dict[str, list], dict[str, list[str]], list[dict],
]:
    """
    Returns:
        records                    base legislation, flat list for the sampler
        reflection_items_by_leg    legislation_id -> list[ReflectionItem]
        articles_by_leg            legislation_id -> ordered article numbers
        amendment_records          amendments as legislation rows (real FK
                                    targets for reflection items)

    TODO once real data arrives: replace the two generate_legislation() calls
    with reflection_source.load_source_file() against the real export.

    KNOWN RISK (confirmed against the LOB database project): legislation_id
    is built as f"{number}/{year}", which was already established there as
    NOT globally unique. Safe here only because the synthetic generator is
    given disjoint number ranges per leg_type — do not point this at real
    data without deciding the identity key the same way the LOB project did.
    """
    log.warning("population.synthetic", extra={
        "reason": "real dataset not yet received", "count_per_type": SYNTHETIC_COUNT_PER_TYPE,
    })

    raw_laws = generate_legislation(SYNTHETIC_COUNT_PER_TYPE, "law", seed=seed)
    raw_bylaws = generate_legislation(
        SYNTHETIC_COUNT_PER_TYPE, "bylaw", seed=seed + 1,
        start_number=SYNTHETIC_COUNT_PER_TYPE + 1,
    )

    records: list[LegislationRecord] = []
    reflection_by_leg: dict[str, list] = {}
    articles_by_leg: dict[str, list[str]] = {}
    amendment_records: list[dict] = []

    for leg_type, raws in (("law", raw_laws), ("bylaw", raw_bylaws)):
        for raw in raws:
            base_ref, base_articles, chain_item, meta_items, refl, _integrity = parse_legislation(raw)
            records.append(LegislationRecord(
                legislation_id=base_ref.legislation_id, leg_type=leg_type,
                has_amendments=len(chain_item.amendment_ids) > 0,
                article_count=len(base_articles),
            ))
            reflection_by_leg[base_ref.legislation_id] = refl
            articles_by_leg[base_ref.legislation_id] = [a.article_number for a in base_articles]

            for idx, mi in enumerate(m for m in meta_items if m.role == "amendment"):
                amendment_records.append({
                    "id": mi.legislation_id, "leg_name": mi.leg_name, "leg_type": leg_type,
                    "amendment_of_id": base_ref.legislation_id, "sequence_index": idx,
                })

    return records, reflection_by_leg, articles_by_leg, amendment_records


def upsert_legislation(
    db: Session, records: list[LegislationRecord], amendment_records: list[dict],
) -> int:
    existing = {row.id for row in db.execute(select(models.Legislation.id)).all()}
    created = 0
    seen_this_run: dict[str, str] = {}

    for rec in records:
        if rec.legislation_id in seen_this_run and seen_this_run[rec.legislation_id] != rec.leg_type:
            raise ValueError(
                f"legislation_id collision: {rec.legislation_id!r} used by both "
                f"{seen_this_run[rec.legislation_id]!r} and {rec.leg_type!r} — "
                "number/year is not a safe identity key across types (see load_population docstring)"
            )
        seen_this_run[rec.legislation_id] = rec.leg_type
        if rec.legislation_id in existing:
            continue
        db.add(models.Legislation(
            id=rec.legislation_id, leg_name=f"(synthetic) {rec.legislation_id}",
            leg_number=rec.legislation_id.split("/")[0],
            year=rec.legislation_id.split("/")[1], leg_type=rec.leg_type,
        ))
        existing.add(rec.legislation_id)
        created += 1
    db.flush()

    for a in amendment_records:
        if a["id"] in existing:
            continue
        db.add(models.Legislation(
            id=a["id"], leg_name=a["leg_name"],
            leg_number=a["id"].split("/")[0], year=a["id"].split("/")[1],
            leg_type=a["leg_type"], is_amendment=True,
            amendment_of_id=a["amendment_of_id"], sequence_index=a["sequence_index"],
        ))
        existing.add(a["id"])
        created += 1
    db.flush()
    return created


def build_candidates(
    spec: AuditSpec,
    records: list[LegislationRecord],
    reflection_by_leg: dict[str, list],
    articles_by_leg: dict[str, list[str]],
    seed: int,
    full: bool,
) -> BuildResult:
    """
    full=False: the fixed sample_size from the spec's YAML (the 100/100
                accuracy-report draw).
    full=True:  every legislation of each leg_type in spec.applies_to —
                sample_population() already returns the entire population
                whenever the requested size is >= what's available, so this
                reuses the exact same, already-tested sampling code path.
    """
    if full:
        samples = [
            sample_population(
                records, lt, sum(1 for r in records if r.leg_type == lt), seed,
            )
            for lt in spec.applies_to
        ]
    else:
        samples = [
            sample_population(records, lt, n, seed)
            for lt, n in spec.sampling.sample_size.items()
        ]

    builders = {
        "metadata": lambda: build_metadata_items(spec, samples, seed),
        "chain": lambda: build_chain_items(spec, samples, seed),
        "reflection": lambda: build_reflection_items(spec, samples, reflection_by_leg, seed),
        "article_integrity": lambda: build_article_integrity_items(spec, samples, articles_by_leg, seed),
    }
    if spec.key not in builders:
        raise ValueError(f"no item builder registered for spec {spec.key!r}")
    return builders[spec.key]()


def insert_items(db: Session, candidates: list[ItemCandidate]) -> tuple[int, int]:
    existing = {row[0] for row in db.execute(select(models.AuditItem.identity_key)).all()}
    inserted, skipped = 0, 0
    for c in candidates:
        if c.identity_key in existing:
            skipped += 1
            continue
        db.add(models.AuditItem(
            spec_key=c.spec_key, unit=c.unit, legislation_id=c.legislation_id,
            amendment_id=c.amendment_id, article_number=c.article_number,
            is_golden=c.is_golden, identity_key=c.identity_key,
        ))
        existing.add(c.identity_key)
        inserted += 1
    db.flush()
    return inserted, skipped
