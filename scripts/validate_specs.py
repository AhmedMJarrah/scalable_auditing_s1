"""
Validate every audit-type spec in configs/ and print a summary.

    py scripts\\validate_specs.py

Exit code 0 = all specs valid. 1 = at least one problem, every problem listed.

Run this after editing any spec, and wire it into CI later — a broken spec
must never reach the auditors.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.config import get_settings                              # noqa: E402
from src.core.logging_setup import get_logger, setup_logging, stage   # noqa: E402
from src.core.spec import SpecError, load_specs                       # noqa: E402


def main() -> int:
    settings = get_settings()
    setup_logging(settings)
    log = get_logger("s1.validate_specs")

    with stage("validate_specs", logger=log,
               configs_dir=str(settings.configs_dir)) as counters:
        try:
            specs = load_specs(settings.configs_dir)
        except SpecError as exc:
            for problem in exc.problems:
                log.error("spec.invalid", extra={"problem": problem})
            counters.update(specs=0, problems=len(exc.problems))
            print(f"\n{len(exc.problems)} problem(s) found:\n")
            for problem in exc.problems:
                print(f"  FAIL  {problem}")
            print()
            return 1

        counters.update(specs=len(specs), problems=0)

    print()
    header = f"{'audit type':<20} {'unit':<12} {'fields':>6} {'scored':>7} {'items':>7}"
    print(header)
    print("-" * len(header))

    total_items = 0
    for key in sorted(specs):
        spec = specs[key]
        total_items += spec.total_sample_size()
        print(
            f"{spec.key:<20} {spec.unit:<12} {len(spec.fields):>6} "
            f"{len(spec.scored_fields()):>7} {spec.total_sample_size():>7}"
        )
        log.info("spec.ok", extra=spec.summary())

    print("-" * len(header))
    print(f"{'TOTAL':<20} {'':<12} {'':>6} {'':>7} {total_items:>7}")
    print(f"\n{len(specs)} spec(s) valid.")
    print(
        "Note: 'items' counts audit units, not auditor decisions. Article-level\n"
        "      types expand once the real data arrives — recompute then before\n"
        "      promising anyone a completion date.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
