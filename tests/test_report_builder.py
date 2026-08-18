from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.spec import load_specs
from src.db import models
from src.db.base import Base
from src.scoring.report_builder import (
    build_full_report, compute_agreement, compute_golden_accuracy,
    load_defect_counts, load_item_scores,
)


@pytest.fixture()
def db(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 't.db'}", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    session = factory()
    yield session
    session.close()


@pytest.fixture(scope="module")
def specs():
    configs = Path(__file__).resolve().parents[1] / "configs"
    return load_specs(configs)


def _seed_legislation(db, n=5):
    for i in range(n):
        db.add(models.Legislation(
            id=f"{i}/2000", leg_name=f"law {i}", leg_number=str(i),
            year="2000", leg_type="law",
        ))
    db.commit()


def test_load_item_scores_basic(db, specs) -> None:
    spec = specs["metadata"]
    _seed_legislation(db, 2)
    item = models.AuditItem(spec_key="metadata", unit="legislation", legislation_id="0/2000")
    db.add(item)
    db.flush()
    auditor = models.Auditor(username="a1", password_hash="x")
    db.add(auditor)
    db.flush()
    db.add(models.Response(
        item_id=item.id, auditor_id=auditor.id, spec_key="metadata", spec_version=1,
        answers={"status": "correct", "name": "correct", "number": "correct",
                 "year": "correct", "publication_date": "correct"},
    ))
    db.commit()

    scores = load_item_scores(db, spec)
    assert len(scores) == 1
    assert scores[0].score == 1.0


def test_load_item_scores_restricted_to_identity_keys(db, specs) -> None:
    spec = specs["metadata"]
    _seed_legislation(db, 2)
    item1 = models.AuditItem(spec_key="metadata", unit="legislation", legislation_id="0/2000")
    item2 = models.AuditItem(spec_key="metadata", unit="legislation", legislation_id="1/2000")
    db.add_all([item1, item2])
    db.commit()

    only_item1 = {item1.identity_key}
    scores = load_item_scores(db, spec, identity_keys=only_item1)
    assert len(scores) == 1
    assert scores[0].identity_key == item1.identity_key


def test_load_item_scores_no_responses_is_none_score(db, specs) -> None:
    spec = specs["metadata"]
    _seed_legislation(db, 1)
    db.add(models.AuditItem(spec_key="metadata", unit="legislation", legislation_id="0/2000"))
    db.commit()

    scores = load_item_scores(db, spec)
    assert len(scores) == 1
    assert scores[0].score is None
    assert scores[0].n_responses == 0


def test_defect_counts_aggregated_across_responses(db, specs) -> None:
    spec = specs["chain"]
    _seed_legislation(db, 1)
    item = models.AuditItem(spec_key="chain", unit="chain", legislation_id="0/2000")
    db.add(item)
    db.flush()
    a1 = models.Auditor(username="a1", password_hash="x")
    a2 = models.Auditor(username="a2", password_hash="x")
    db.add_all([a1, a2])
    db.flush()
    db.add(models.Response(
        item_id=item.id, auditor_id=a1.id, spec_key="chain", spec_version=1,
        answers={"defect_types": ["taadil_mafqud", "ukhra"]},
    ))
    db.commit()

    counts = load_defect_counts(db, spec)
    assert counts["taadil_mafqud"] == 1
    assert counts["ukhra"] == 1


def test_agreement_only_counts_items_with_exactly_two_responses(db, specs) -> None:
    spec = specs["metadata"]
    _seed_legislation(db, 2)
    item_single = models.AuditItem(spec_key="metadata", unit="legislation", legislation_id="0/2000")
    item_double = models.AuditItem(spec_key="metadata", unit="legislation", legislation_id="1/2000")
    db.add_all([item_single, item_double])
    db.flush()
    a1 = models.Auditor(username="a1", password_hash="x")
    a2 = models.Auditor(username="a2", password_hash="x")
    db.add_all([a1, a2])
    db.flush()

    # single-response item — must NOT contribute to agreement pairs
    db.add(models.Response(
        item_id=item_single.id, auditor_id=a1.id, spec_key="metadata", spec_version=1,
        answers={"status": "correct"},
    ))
    # double-response item — SHOULD contribute
    db.add(models.Response(
        item_id=item_double.id, auditor_id=a1.id, spec_key="metadata", spec_version=1,
        answers={"status": "correct", "name": "correct", "number": "correct",
                 "year": "correct", "publication_date": "correct"},
    ))
    db.add(models.Response(
        item_id=item_double.id, auditor_id=a2.id, spec_key="metadata", spec_version=1,
        answers={"status": "correct", "name": "correct", "number": "correct",
                 "year": "correct", "publication_date": "correct"},
    ))
    db.commit()

    kappa, n_pairs = compute_agreement(db, spec)
    assert n_pairs == 5    # 5 scored fields on the double-response item, 0 from the single
    assert kappa == pytest.approx(1.0)   # perfect agreement


def test_golden_accuracy_end_to_end(db, specs) -> None:
    spec = specs["metadata"]
    _seed_legislation(db, 1)
    item = models.AuditItem(spec_key="metadata", unit="legislation", legislation_id="0/2000", is_golden=True)
    db.add(item)
    db.flush()
    db.add(models.GoldenAnswer(item_id=item.id, answers={"status": "correct"}))
    auditor = models.Auditor(username="a1", password_hash="x")
    db.add(auditor)
    db.flush()
    db.add(models.Response(
        item_id=item.id, auditor_id=auditor.id, spec_key="metadata", spec_version=1,
        answers={"status": "correct"},
    ))
    db.commit()

    result = compute_golden_accuracy(db, spec)
    assert result[auditor.id] == 1.0


def test_build_full_report_integration(db, specs) -> None:
    spec = specs["metadata"]
    _seed_legislation(db, 3)
    auditor = models.Auditor(username="a1", password_hash="x")
    db.add(auditor)
    db.flush()
    for i in range(3):
        item = models.AuditItem(spec_key="metadata", unit="legislation", legislation_id=f"{i}/2000")
        db.add(item)
        db.flush()
        db.add(models.Response(
            item_id=item.id, auditor_id=auditor.id, spec_key="metadata", spec_version=1,
            answers={"status": "correct", "name": "correct", "number": "correct",
                     "year": "correct", "publication_date": "correct"},
        ))
    db.commit()

    report = build_full_report(db, spec, identity_keys=None, is_sample=True)
    assert report.mean_score == 1.0
    assert report.n_scored_items == 3
