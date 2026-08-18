"""
Populate synthetic responses by driving the REAL checkout/lease/submit flow
— not by inserting rows directly. This means testing scoring today also
exercises leasing, overlap, and orphan-forced double-review exactly as a
real volunteer session would.

    python scripts\\seed_synthetic_responses.py --spec metadata --accuracy 0.9

Creates a handful of synthetic auditor accounts if they don't already
exist, then has them work through every available item for the given spec
until the pool is exhausted for all of them (respecting required_reviews —
overlap and orphan_suspected items naturally get two DIFFERENT synthetic
auditors, exactly like a real deployment).

This is a demo/testing tool, not something you run against real auditor
data — never point --accuracy at anything meaningful once real responses
exist; this only makes sense against synthetic items.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select                                          # noqa: E402

from src.assignment.auth import AuthError, create_auditor              # noqa: E402
from src.assignment.leasing import checkout_next_item_for_spec, submit_response  # noqa: E402
from src.core.config import get_settings                               # noqa: E402
from src.core.logging_setup import get_logger, setup_logging, stage    # noqa: E402
from src.core.spec import AuditSpec, load_specs                        # noqa: E402
from src.db import models                                              # noqa: E402
from src.db.session import session_scope                               # noqa: E402

SYNTHETIC_AUDITOR_USERNAMES = [f"synthetic_auditor_{i}" for i in range(1, 6)]
SYNTHETIC_PASSWORD = "synthetic_test_pw_only"


def ensure_synthetic_auditors(db) -> list[str]:
    ids = []
    for username in SYNTHETIC_AUDITOR_USERNAMES:
        existing = db.execute(
            select(models.Auditor).where(models.Auditor.username == username)
        ).scalar_one_or_none()
        if existing is None:
            try:
                a = create_auditor(db, username, SYNTHETIC_PASSWORD)
            except AuthError:
                continue
            ids.append(a.id)
        else:
            ids.append(existing.id)
    db.commit()
    return ids


def fake_answers(spec: AuditSpec, rng: random.Random, accuracy: float) -> dict:
    answers = {}
    for f in spec.scored_fields():
        roll = rng.random()
        if roll < accuracy:
            answers[f.key] = "correct"
        elif roll < accuracy + (1 - accuracy) * 0.7:
            answers[f.key] = "incorrect"
        else:
            answers[f.key] = "cannot_determine"

    for f in spec.fields:
        if f.type == "multi_select" and rng.random() < 0.15:
            if f.options:
                answers[f.key] = [rng.choice(f.options).key]
    return answers


def seed(db, spec: AuditSpec, auditor_ids: list[str], settings, accuracy: float, log) -> int:
    rng = random.Random(settings.random_seed)
    submitted = 0
    progress = True
    while progress:
        progress = False
        for auditor_id in auditor_ids:
            while True:
                assignment = checkout_next_item_for_spec(
                    db, auditor_id, spec, settings.random_seed, settings.assignment_lease_minutes,
                )
                if assignment is None:
                    break
                answers = fake_answers(spec, rng, accuracy)
                submit_response(
                    db, assignment.id, auditor_id, spec.key, spec.spec_version, answers,
                )
                db.commit()
                submitted += 1
                progress = True
    log.info("seed.done", extra={"spec": spec.key, "submitted": submitted})
    return submitted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--accuracy", type=float, default=0.9,
                         help="probability a verdict field is answered 'correct' (0-1)")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings)
    log = get_logger("s1.seed_synthetic_responses")
    specs = load_specs(settings.configs_dir)

    if args.spec not in specs:
        print(f"Unknown spec {args.spec!r}. Available: {sorted(specs)}")
        return 1
    spec = specs[args.spec]

    with stage("seed_synthetic_responses", logger=log, spec=args.spec, accuracy=args.accuracy) as counters:
        with session_scope() as db:
            auditor_ids = ensure_synthetic_auditors(db)
            submitted = seed(db, spec, auditor_ids, settings, args.accuracy, log)
        counters.update(submitted=submitted, auditors=len(auditor_ids))

    print(f"\nSubmitted {submitted} synthetic responses for {spec.key!r} "
          f"using {len(auditor_ids)} synthetic auditors.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
