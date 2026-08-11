"""
Run the full sampling pass and write AuditItem rows to the database.

Uses SYNTHETIC data until real data arrives — clearly logged and printed
every run so nobody mistakes a synthetic count for a real one. Swapping to
real data later means pointing this script at the real JSON file(s) instead
of calling generate_legislation(); nothing in src/sampling or src/db changes.

    python scripts\\run_sampling.py

Idempotent: re-running upserts legislation by id and skips any AuditItem
whose identity_key already exists, so running it twice does not double the
sample.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select                                          # noqa: E402

from src.core.config import get_settings                               # noqa: E402
from src.core.logging_setup import get_logger, setup_logging, stage    # noqa: E402
from src.core.spec import load_specs                                   # noqa: E402
from src.db import models                                              # noqa: E402
from src.db.session import session_scope                               # noqa: E402
from src.ingest.reflection_source import parse_legislation             # noqa: E402
from src.ingest.synthetic import generate_legislation                  # noqa: E402
from src.sampling.item_builder import (                                # noqa: E402
    ItemCandidate, build_article_integrity_items, build_chain_items,
    build_metadata_items, build_reflection_items, summarize,
)
from src.sampling.legislation_sampler import LegislationRecord, sample_population  # noqa: E402

SYNTHETIC_COUNT_PER_TYPE = 250   # > 100 so the sampler exercises real drawing, not "take all"


def load_population(seed: int, log) -> tuple[
    list[LegislationRecord], dict[str, list], dict[str, list[str]], list[dict],
]:
    """
    Returns:
        records                    base legislation, flat list for the sampler
        reflection_items_by_leg    legislation_id -> list[ReflectionItem]
        articles_by_leg            legislation_id -> ordered article numbers
                                    (base list, for article_integrity sampling)
        amendment_records          amendments as legislation rows — required
                                    because reflection items' amendment_id is
                                    a real foreign key into legislation.id;
                                    the base legislation alone is not enough

    TODO once real data arrives: replace the two generate_legislation() calls
    below with reflection_source.load_source_file() against the real export.

    KNOWN RISK, confirmed against the LOB database project: legislation_id
    here is built as f"{number}/{year}" (see reflection_source.LegislationRef).
    That was already established as NOT globally unique — number/year alone
    can collide, both across law vs bylaw and occasionally within one type;
    the LOB work resolved this by also keying on name. This module has not
    been made to match that yet. It is safe for now only because the
    synthetic generator below is given disjoint number ranges per leg_type.
    Do not point this at real data without first deciding the identity key
    the same way the LOB project did — otherwise two different pieces of
    legislation can silently collide into one Legislation row.
    """
    log.warning("population.synthetic", extra={
        "reason": "real dataset not yet received",
        "count_per_type": SYNTHETIC_COUNT_PER_TYPE,
    })

    raw_laws = generate_legislation(SYNTHETIC_COUNT_PER_TYPE, "law", seed=seed)
    raw_bylaws = generate_legislation(
        SYNTHETIC_COUNT_PER_TYPE, "bylaw", seed=seed + 1,
        start_number=SYNTHETIC_COUNT_PER_TYPE + 1,   # disjoint from law numbers —
    )                                                  # see identity-key risk above

    records: list[LegislationRecord] = []
    reflection_by_leg: dict[str, list] = {}
    articles_by_leg: dict[str, list[str]] = {}
    amendment_records: list[dict] = []

    for leg_type, raws in (("law", raw_laws), ("bylaw", raw_bylaws)):
        for raw in raws:
            base_ref, base_articles, chain_item, meta_items, refl, _integrity = parse_legislation(raw)
            records.append(LegislationRecord(
                legislation_id=base_ref.legislation_id,
                leg_type=leg_type,
                has_amendments=len(chain_item.amendment_ids) > 0,
                article_count=len(base_articles),
            ))
            reflection_by_leg[base_ref.legislation_id] = refl
            articles_by_leg[base_ref.legislation_id] = [a.article_number for a in base_articles]

            amendments = [m for m in meta_items if m.role == "amendment"]
            for idx, mi in enumerate(amendments):
                amendment_records.append({
                    "id": mi.legislation_id, "leg_name": mi.leg_name, "leg_type": leg_type,
                    "amendment_of_id": base_ref.legislation_id, "sequence_index": idx,
                })

    return records, reflection_by_leg, articles_by_leg, amendment_records


def upsert_legislation(
    db, records: list[LegislationRecord], amendment_records: list[dict],
) -> int:
    existing = {row.id for row in db.execute(select(models.Legislation.id)).all()}
    created = 0
    seen_this_run: dict[str, str] = {}   # id -> leg_type, to catch in-batch collisions

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
    db.flush()   # bases committed before amendments reference them via amendment_of_id

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


def insert_items(db, candidates: list[ItemCandidate]) -> tuple[int, int]:
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


def main() -> int:
    settings = get_settings()
    setup_logging(settings)
    log = get_logger("s1.run_sampling")
    specs = load_specs(settings.configs_dir)

    with stage("run_sampling", logger=log, seed=settings.random_seed) as counters:
        records, reflection_by_leg, articles_by_leg, amendment_records = load_population(
            settings.random_seed, log
        )

        results = []
        with session_scope() as db:
            leg_created = upsert_legislation(db, records, amendment_records)

            for spec_key, builder_fn in (
                ("metadata", build_metadata_items),
                ("chain", build_chain_items),
            ):
                spec = specs.get(spec_key)
                if spec is None:
                    continue
                samples = [
                    sample_population(records, lt, n, settings.random_seed)
                    for lt, n in spec.sampling.sample_size.items()
                ]
                for s in samples:
                    if s.under_populated:
                        log.warning("sampling.under_populated", extra={
                            "spec": spec_key, "leg_type": s.leg_type,
                            "requested": s.requested, "available": s.population_size,
                        })
                build = builder_fn(spec, samples, settings.random_seed)
                results.append(build)
                ins, skip = insert_items(db, build.deduplicated())
                log.info("items.inserted", extra={
                    "spec": spec_key, "inserted": ins, "skipped_existing": skip,
                })

            spec = specs.get("reflection")
            if spec is not None:
                samples = [
                    sample_population(records, lt, n, settings.random_seed)
                    for lt, n in spec.sampling.sample_size.items()
                ]
                build = build_reflection_items(spec, samples, reflection_by_leg, settings.random_seed)
                results.append(build)
                ins, skip = insert_items(db, build.deduplicated())
                log.info("items.inserted", extra={"spec": "reflection", "inserted": ins, "skipped_existing": skip})

            spec = specs.get("article_integrity")
            if spec is not None:
                samples = [
                    sample_population(records, lt, n, settings.random_seed)
                    for lt, n in spec.sampling.sample_size.items()
                ]
                build = build_article_integrity_items(spec, samples, articles_by_leg, settings.random_seed)
                results.append(build)
                ins, skip = insert_items(db, build.deduplicated())
                log.info("items.inserted", extra={"spec": "article_integrity", "inserted": ins, "skipped_existing": skip})

        summary = summarize(results)
        counters.update(legislation_created=leg_created, **{
            f"{k}_items": v["items"] for k, v in summary.items()
        })

    print("\n*** USING SYNTHETIC DATA — replace before any real reporting ***\n")
    header = f"{'audit type':<20} {'items':>7} {'golden':>7} {'chain skipped (no amendments)':>30}"
    print(header)
    print("-" * len(header))
    for spec_key, s in summary.items():
        print(f"{spec_key:<20} {s['items']:>7} {s['golden']:>7} {s['skipped_no_amendments']:>30}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
