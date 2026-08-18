"""
Compute and print the accuracy/data-quality report for one audit type, and
save a defect-breakdown chart.

    python scripts\\compute_report.py --spec metadata --mode sample
    python scripts\\compute_report.py --spec reflection --mode full

--mode sample   The fixed 100/100 draw (same seed as run_sampling.py) —
                shows a confidence interval, since this is a statistical
                sample. This is the number for the manager's accuracy report.
--mode full     Every item currently in the database for this spec,
                sample AND any batch-released items combined — NO
                confidence interval, since a census has no sampling error.
                This is "how much of everything we've covered so far."
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import matplotlib                                                      # noqa: E402
matplotlib.use("Agg")                                                  # headless — no display needed
import matplotlib.pyplot as plt                                        # noqa: E402

from src.core.config import get_settings                               # noqa: E402
from src.core.logging_setup import get_logger, setup_logging, stage    # noqa: E402
from src.core.spec import load_specs                                   # noqa: E402
from src.db.session import session_scope                               # noqa: E402
from src.scoring.compute import ScoreReport                            # noqa: E402
from src.scoring.report_builder import build_full_report               # noqa: E402
from src.sampling.pipeline import build_candidates, load_population    # noqa: E402


def render_chart(report: ScoreReport, spec_key: str, out_path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))

    ax = axes[0]
    score_pct = (report.mean_score or 0) * 100
    ax.barh([spec_key], [score_pct], color="#2e7d32" if score_pct >= 80 else "#c62828")
    ax.set_xlim(0, 100)
    ax.set_xlabel("Score (%)")
    ax.set_title(f"{spec_key} — overall score")
    if report.ci_low is not None:
        ax.errorbar(
            [score_pct], [spec_key],
            xerr=[[score_pct - report.ci_low * 100], [report.ci_high * 100 - score_pct]],
            fmt="none", ecolor="black", capsize=6,
        )

    ax2 = axes[1]
    if report.defect_counts:
        labels, values = zip(*report.defect_counts.most_common())
        ax2.barh(labels, values, color="#c62828")
        ax2.set_xlabel("Occurrences")
        ax2.set_title("Issues captured")
        ax2.invert_yaxis()
    else:
        ax2.text(0.5, 0.5, "No defects recorded", ha="center", va="center")
        ax2.set_axis_off()

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def print_report(report: ScoreReport, mode: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"AUDIT TYPE: {report.spec_key}  (mode: {mode})")
    print("=" * 60)

    if report.mean_score is None:
        print("No scored items yet — nothing has been reviewed.")
        return

    pct = report.mean_score * 100
    print(f"Score: {pct:.1f}%", end="")
    if report.ci_low is not None:
        print(f"  (95% CI: {report.ci_low * 100:.1f}% - {report.ci_high * 100:.1f}%)")
    else:
        print("  (full census — no confidence interval)")

    print(f"Items scored: {report.n_scored_items} / {report.n_items}")
    print(f"cannot_determine rate: {report.cannot_determine_rate * 100:.1f}%", end="")
    if report.over_cannot_determine_threshold:
        print("  *** ABOVE THRESHOLD — treat this score with caution ***")
    else:
        print()

    if report.kappa is not None:
        print(f"Inter-auditor agreement (kappa): {report.kappa:.2f} "
              f"({report.n_agreement_pairs} paired judgements)")
    else:
        print("Inter-auditor agreement: not enough double-reviewed items yet")

    if report.golden_accuracy:
        print("\nGolden-set accuracy per auditor:")
        for auditor_id, acc in sorted(report.golden_accuracy.items(), key=lambda x: x[1]):
            flag = "  <-- check this auditor" if acc < 0.7 else ""
            print(f"  {auditor_id}: {acc * 100:.0f}%{flag}")

    if report.defect_counts:
        print("\nIssues captured:")
        for defect, count in report.defect_counts.most_common():
            print(f"  {defect}: {count}")

    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--mode", choices=["sample", "full"], default="sample")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging(settings)
    log = get_logger("s1.compute_report")
    specs = load_specs(settings.configs_dir)

    if args.spec not in specs:
        print(f"Unknown spec {args.spec!r}. Available: {sorted(specs)}")
        return 1
    spec = specs[args.spec]

    with stage("compute_report", logger=log, spec=args.spec, mode=args.mode) as counters:
        identity_keys = None
        is_sample = args.mode == "sample"
        if is_sample:
            records, reflection_by_leg, articles_by_leg, _ = load_population(settings.random_seed)
            build = build_candidates(
                spec, records, reflection_by_leg, articles_by_leg,
                settings.random_seed, full=False,
            )
            identity_keys = {c.identity_key for c in build.deduplicated()}

        with session_scope() as db:
            report = build_full_report(db, spec, identity_keys, is_sample)

        counters.update(
            mean_score=report.mean_score, n_items=report.n_items,
            n_scored=report.n_scored_items,
        )

    print_report(report, args.mode)

    if report.mean_score is not None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        chart_path = settings.data_dir.parent / "reports" / f"{spec.key}_{args.mode}.png"
        chart_path.parent.mkdir(parents=True, exist_ok=True)
        render_chart(report, spec.key, chart_path)
        print(f"Chart saved to {chart_path}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
