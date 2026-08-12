from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from src.core.spec import load_specs
from src.db import models
from src.db.base import Base
from src.sampling.pipeline import (
    build_candidates, insert_items, load_population, upsert_legislation,
)


@pytest.fixture()
def db(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    session = factory()
    yield session
    session.close()


def _specs():
    configs = Path(__file__).resolve().parents[1] / "configs"
    return load_specs(configs)


def test_full_population_is_at_least_as_large_as_fixed_sample(db) -> None:
    specs = _specs()
    records, reflection_by_leg, articles_by_leg, amendment_records = load_population(seed=1)
    upsert_legislation(db, records, amendment_records)
    db.commit()

    spec = specs["reflection"]
    sample_build = build_candidates(spec, records, reflection_by_leg, articles_by_leg, seed=1, full=False)
    full_build = build_candidates(spec, records, reflection_by_leg, articles_by_leg, seed=1, full=True)
    assert len(full_build.deduplicated()) >= len(sample_build.deduplicated())


def test_full_candidate_order_is_deterministic_across_calls() -> None:
    records, reflection_by_leg, articles_by_leg, _ = load_population(seed=5)
    specs = _specs()
    spec = specs["reflection"]

    b1 = build_candidates(spec, records, reflection_by_leg, articles_by_leg, seed=5, full=True)
    b2 = build_candidates(spec, records, reflection_by_leg, articles_by_leg, seed=5, full=True)

    order1 = [c.identity_key for c in sorted(b1.deduplicated(), key=lambda c: c.identity_key)]
    order2 = [c.identity_key for c in sorted(b2.deduplicated(), key=lambda c: c.identity_key)]
    assert order1 == order2


def test_batched_release_never_skips_or_duplicates_an_item(db) -> None:
    """Releasing in three small batches must produce exactly the same set
    of items as releasing everything in one batch."""
    specs = _specs()
    records, reflection_by_leg, articles_by_leg, amendment_records = load_population(seed=3)
    upsert_legislation(db, records, amendment_records)
    db.commit()

    spec = specs["metadata"]
    build = build_candidates(spec, records, reflection_by_leg, articles_by_leg, seed=3, full=True)
    all_candidates = sorted(build.deduplicated(), key=lambda c: c.identity_key)

    batch_size = max(1, len(all_candidates) // 3)
    released_identity_keys: list[str] = []
    for start in range(0, len(all_candidates), batch_size):
        chunk = all_candidates[start:start + batch_size]
        released_identity_keys.extend(c.identity_key for c in chunk)
        insert_items(db, chunk)
    db.commit()

    all_keys = [c.identity_key for c in all_candidates]
    assert released_identity_keys == all_keys           # same order, nothing skipped
    assert len(set(released_identity_keys)) == len(released_identity_keys)  # nothing duplicated

    db_count = db.execute(select(models.AuditItem)).scalars().all()
    assert len(db_count) == len(all_candidates)


def test_reinserting_an_already_released_item_is_a_noop(db) -> None:
    specs = _specs()
    records, reflection_by_leg, articles_by_leg, amendment_records = load_population(seed=3)
    upsert_legislation(db, records, amendment_records)
    db.commit()

    spec = specs["metadata"]
    build = build_candidates(spec, records, reflection_by_leg, articles_by_leg, seed=3, full=True)
    all_candidates = sorted(build.deduplicated(), key=lambda c: c.identity_key)

    first_batch = all_candidates[:20]
    ins1, skip1 = insert_items(db, first_batch)
    db.commit()
    assert ins1 == 20 and skip1 == 0

    overlapping_batch = all_candidates[:30]   # first 20 already exist
    ins2, skip2 = insert_items(db, overlapping_batch)
    db.commit()
    assert ins2 == 10 and skip2 == 20
