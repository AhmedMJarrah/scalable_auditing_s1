"""
Bridges the database to src/scoring/compute.py's pure math.

Kept separate deliberately: compute.py has zero DB dependency and is fully
unit-testable with plain dicts; this module does the I/O and assembly.
"""

from __future__ import annotations

from collections import Counter, defaultdict

from sqlalchemy import select, func
from sqlalchemy.orm import Session

from src.core.spec import AuditSpec
from src.db import models
from src.scoring.compute import (
    ItemScore, ScoreReport, build_report, cohens_kappa, defect_breakdown,
    golden_accuracy, score_item,
)


def is_sample_item(identity_key: str, sample_identity_keys: set[str]) -> bool:
    return identity_key in sample_identity_keys


def load_item_scores(
    db: Session, spec: AuditSpec, identity_keys: set[str] | None = None,
) -> list[ItemScore]:
    """
    identity_keys: restrict to this set (the fixed-sample accuracy report);
    None means every item currently in the database for this spec (the
    full-census view).
    """
    query = select(models.AuditItem).where(models.AuditItem.spec_key == spec.key)
    items = db.execute(query).scalars().all()
    if identity_keys is not None:
        items = [i for i in items if i.identity_key in identity_keys]
    if not items:
        return []

    item_ids = [i.id for i in items]
    responses = db.execute(
        select(models.Response).where(models.Response.item_id.in_(item_ids))
    ).scalars().all()

    responses_by_item: dict[str, list[dict]] = defaultdict(list)
    for r in responses:
        responses_by_item[r.item_id].append(r.answers)

    scores = []
    for item in items:
        answers_list = responses_by_item.get(item.id, [])
        score, cd, total = score_item(answers_list, spec)
        scores.append(ItemScore(
            identity_key=item.identity_key, legislation_id=item.legislation_id,
            score=score, n_responses=len(answers_list),
            cannot_determine_fields=cd, total_scoreable_fields=total,
        ))
    return scores


def load_defect_counts(db: Session, spec: AuditSpec) -> Counter:
    multi_select_fields = [f for f in spec.fields if f.type == "multi_select"]
    if not multi_select_fields:
        return Counter()

    item_ids = db.execute(
        select(models.AuditItem.id).where(models.AuditItem.spec_key == spec.key)
    ).scalars().all()
    responses = db.execute(
        select(models.Response).where(models.Response.item_id.in_(item_ids))
    ).scalars().all()
    answer_dicts = [r.answers for r in responses]

    counts: Counter = Counter()
    for f in multi_select_fields:
        counts.update(defect_breakdown(answer_dicts, f))
    return counts


def compute_agreement(db: Session, spec: AuditSpec) -> tuple[float | None, int]:
    """
    Pooled kappa across every scored verdict field, using only items that
    received exactly 2 responses (overlap sample + orphan-forced items).
    Pooled = agreement across all fields combined, not broken out per field
    — a reasonable first cut; per-field kappa is a natural refinement if
    one field turns out to be the disagreement driver.
    """
    item_ids_with_two = db.execute(
        select(models.Response.item_id)
        .join(models.AuditItem, models.AuditItem.id == models.Response.item_id)
        .where(models.AuditItem.spec_key == spec.key)
        .group_by(models.Response.item_id)
        .having(func.count(models.Response.id) == 2)
    ).scalars().all()

    pairs: list[tuple[str, str]] = []
    for item_id in item_ids_with_two:
        responses = db.execute(
            select(models.Response).where(models.Response.item_id == item_id)
        ).scalars().all()
        if len(responses) != 2:
            continue
        a, b = responses
        for f in spec.scored_fields():
            va, vb = a.answers.get(f.key), b.answers.get(f.key)
            if va is not None and vb is not None:
                pairs.append((va, vb))

    return cohens_kappa(pairs), len(pairs)


def compute_golden_accuracy(db: Session, spec: AuditSpec) -> dict[str, float]:
    golden_items = db.execute(
        select(models.AuditItem)
        .where(models.AuditItem.spec_key == spec.key, models.AuditItem.is_golden.is_(True))
    ).scalars().all()
    if not golden_items:
        return {}

    golden_by_item = {
        row.item_id: row.answers for row in db.execute(
            select(models.GoldenAnswer).where(
                models.GoldenAnswer.item_id.in_([i.id for i in golden_items])
            )
        ).scalars().all()
    }

    by_auditor: dict[str, list[tuple[dict, dict]]] = defaultdict(list)
    responses = db.execute(
        select(models.Response).where(
            models.Response.item_id.in_(list(golden_by_item))
        )
    ).scalars().all()
    for r in responses:
        golden = golden_by_item.get(r.item_id)
        if golden:
            by_auditor[r.auditor_id].append((r.answers, golden))

    return golden_accuracy(by_auditor)


def build_full_report(
    db: Session, spec: AuditSpec, identity_keys: set[str] | None, is_sample: bool,
) -> ScoreReport:
    scores = load_item_scores(db, spec, identity_keys)
    defects = load_defect_counts(db, spec)
    kappa, n_pairs = compute_agreement(db, spec)
    golden = compute_golden_accuracy(db, spec)
    return build_report(
        spec, scores, is_sample=is_sample, defect_counts=defects,
        kappa=kappa, n_agreement_pairs=n_pairs, golden=golden,
    )
