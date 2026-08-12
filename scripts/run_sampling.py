"""
Run the fixed 100-law + 100-bylaw sample per audit type and write it to the
database — the accuracy / data-quality report sample.

For "review the entire population instead", use release_batch.py — it draws
from the same underlying data but exposes it to volunteers in
admin-controlled chunks rather than all at once.

    python scripts\\run_sampling.py

Idempotent — safe to re-run.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.config import get_settings                              # noqa: E402
from src.core.logging_setup import get_logger, setup_logging, stage   # noqa: E402
from src.core.spec import load_specs                                  # noqa: E402
from src.db.session import session_scope                              # noqa: E402
from src.sampling.item_builder import summarize                       # noqa: E402
from src.sampling.pipeline import (                                   # noqa: E402
    build_candidates, insert_items, load_population, upsert_legislation,
)


def main() -> int:
    settings = get_settings()
    setup_logging(settings)
    log = get_logger("s1.run_sampling")
    specs = load_specs(settings.configs_dir)

    with stage("run_sampling", logger=log, seed=settings.random_seed) as counters:
        records, reflection_by_leg, articles_by_leg, amendment_records = load_population(
            settings.random_seed
        )

        results = []
        with session_scope() as db:
            leg_created = upsert_legislation(db, records, amendment_records)

            for spec_key in ("metadata", "chain", "reflection", "article_integrity"):
                spec = specs.get(spec_key)
                if spec is None:
                    continue
                build = build_candidates(
                    spec, records, reflection_by_leg, articles_by_leg,
                    settings.random_seed, full=False,
                )
                results.append(build)
                ins, skip = insert_items(db, build.deduplicated())
                log.info("items.inserted", extra={
                    "spec": spec_key, "inserted": ins, "skipped_existing": skip,
                })

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
