"""
Release the next batch of items from the FULL population, for audit types
where you want volunteers to eventually cover everything — not just the
fixed 100/100 accuracy-report sample.

    python scripts\\release_batch.py --spec reflection --batch-size 300

Run it again whenever you want to open up more work; it always picks up
where the last release left off (release_state tracks the position). Order
is deterministic by item identity_key, so nothing is skipped and nothing
repeats across runs, regardless of how many times this is called.

An item that was already inserted by run_sampling.py (the 100/100 sample)
is simply skipped here rather than duplicated — a volunteer who already
covered it via the accuracy-report queue never sees it twice.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.config import get_settings                              # noqa: E402
from src.core.logging_setup import get_logger, setup_logging, stage   # noqa: E402
from src.core.spec import load_specs                                  # noqa: E402
from src.db import models                                             # noqa: E402
from src.db.session import session_scope                              # noqa: E402
from src.sampling.pipeline import (                                   # noqa: E402
    build_candidates, insert_items, load_population, upsert_legislation,
)


def get_or_create_release_state(db, spec_key: str) -> models.ReleaseState:
    state = db.get(models.ReleaseState, spec_key)
    if state is None:
        state = models.ReleaseState(spec_key=spec_key, released_count=0)
        db.add(state)
        db.flush()
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True, help="audit type key, e.g. reflection")
    parser.add_argument("--batch-size", type=int, default=300)
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings)
    log = get_logger("s1.release_batch")
    specs = load_specs(settings.configs_dir)

    if args.spec not in specs:
        print(f"Unknown spec {args.spec!r}. Available: {sorted(specs)}")
        return 1
    spec = specs[args.spec]

    with stage("release_batch", logger=log, spec=args.spec, batch_size=args.batch_size) as counters:
        records, reflection_by_leg, articles_by_leg, amendment_records = load_population(
            settings.random_seed
        )
        build = build_candidates(
            spec, records, reflection_by_leg, articles_by_leg,
            settings.random_seed, full=True,
        )
        # Deterministic order — release_state.released_count is a position
        # into THIS exact ordering, so it must be stable across runs.
        all_candidates = sorted(build.deduplicated(), key=lambda c: c.identity_key)

        with session_scope() as db:
            upsert_legislation(db, records, amendment_records)
            state = get_or_create_release_state(db, spec.key)
            start = state.released_count
            end = min(start + args.batch_size, len(all_candidates))
            batch = all_candidates[start:end]

            if not batch:
                counters.update(released=0, remaining=0, total=len(all_candidates))
                print(f"\nNothing left to release for {spec.key!r} — "
                      f"full population ({len(all_candidates)} items) already released.\n")
                return 0

            inserted, skipped = insert_items(db, batch)
            state.released_count = end
            db.flush()

        counters.update(
            released_this_batch=len(batch), inserted=inserted, skipped_existing=skipped,
            position=end, total=len(all_candidates),
        )

    print(f"\nReleased items {start + 1}-{end} of {len(all_candidates)} for {spec.key!r} "
          f"({inserted} new, {skipped} already existed).")
    remaining = len(all_candidates) - end
    print(f"{remaining} remaining."
          + (" Run this again to release the next batch." if remaining else " Full population now released."))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
