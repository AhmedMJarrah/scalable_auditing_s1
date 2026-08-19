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
import arabic_reshaper                                                 # noqa: E402

# matplotlib's core text renderer does NOT reliably shape Arabic (letter
# joining) on its own — behavior confirmed on Windows 10 / this font stack.
# NOTE: bidi.get_display() was removed after diagnostic testing showed
# Windows/matplotlib already renders reshaped Arabic in correct visual
# order — applying get_display() on top double-reorders it. See
# rtl_diagnostic.py. Do not re-add get_display() without re-running that
# diagnostic first.
plt.rcParams["font.family"] = ["Arial", "Tahoma", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False


def ar(text: str) -> str:
    """Shape Arabic text for matplotlib rendering (Windows-verified: reshape only, no bidi)."""
    if not text:
        return text
    return arabic_reshaper.reshape(text)

from src.core.config import get_settings                               # noqa: E402
from src.core.logging_setup import get_logger, setup_logging, stage    # noqa: E402
from src.core.spec import AuditSpec, load_specs                        # noqa: E402
from src.db.session import session_scope                               # noqa: E402
from src.scoring.compute import ScoreReport                            # noqa: E402
from src.scoring.report_builder import build_full_report               # noqa: E402
from src.sampling.pipeline import build_candidates, load_population    # noqa: E402


def render_chart(report: ScoreReport, spec: "AuditSpec", out_path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))

    ax = axes[0]
    score_pct = (report.mean_score or 0) * 100
    ax.barh([ar(spec.title_ar)], [score_pct], color="#2e7d32" if score_pct >= 80 else "#c62828")
    ax.set_xlim(0, 100)
    ax.set_xlabel(ar("النتيجة (%)"))
    ax.set_title(ar(f"{spec.title_ar} — النتيجة الإجمالية"))
    if report.ci_low is not None:
        ax.errorbar(
            [score_pct], [ar(spec.title_ar)],
            xerr=[[score_pct - report.ci_low * 100], [report.ci_high * 100 - score_pct]],
            fmt="none", ecolor="black", capsize=6,
        )

    ax2 = axes[1]
    translated = translate_defects(report.defect_counts, spec)
    if translated:
        labels, values = zip(*translated)
        ax2.barh([ar(l) for l in labels], values, color="#c62828")
        ax2.set_xlabel(ar("عدد التكرارات"))
        ax2.set_title(ar("المشاكل المرصودة"))
        ax2.invert_yaxis()
    else:
        ax2.text(0.5, 0.5, ar("لا توجد مشاكل مسجّلة"), ha="center", va="center")
        ax2.set_axis_off()

    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def defect_label_map(spec: "AuditSpec") -> dict[str, str]:
    """Map every multi_select option key to its Arabic label — so charts
    and reports show 'تعديل مكرر', not the internal key 'taadil_mukarrar'."""
    labels: dict[str, str] = {}
    for f in spec.fields:
        if f.type == "multi_select" and f.options:
            for opt in f.options:
                labels[opt.key] = opt.label_ar
    return labels


def translate_defects(
    defect_counts, spec: "AuditSpec",
) -> list[tuple[str, int]]:
    """Counter[key] -> [(label_ar, count), ...], most common first. Falls
    back to the raw key (never crashes) if a key isn't found in any
    field's options — e.g. after a spec edit removed an option that older
    responses still reference."""
    labels = defect_label_map(spec)
    return [(labels.get(key, key), count) for key, count in defect_counts.most_common()]


MODE_LABELS_AR = {"sample": "عيّنة إحصائية", "full": "تعداد كامل"}


def print_report(report: ScoreReport, spec: "AuditSpec", mode: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"نوع التدقيق: {spec.title_ar}  (الوضع: {MODE_LABELS_AR.get(mode, mode)})")
    print("=" * 60)

    if report.mean_score is None:
        print("لا توجد عناصر مقيّمة بعد — لم تتم أي مراجعة.")
        return

    pct = report.mean_score * 100
    print(f"النتيجة: {pct:.1f}%", end="")
    if report.ci_low is not None:
        print(f"  (فترة الثقة 95%: {report.ci_low * 100:.1f}% - {report.ci_high * 100:.1f}%)")
    else:
        print("  (تعداد كامل — بدون فترة ثقة)")

    print(f"عدد العناصر المقيّمة: {report.n_scored_items} من {report.n_items}")
    print(f"نسبة 'غير قادر على التحديد': {report.cannot_determine_rate * 100:.1f}%", end="")
    if report.over_cannot_determine_threshold:
        print("   *** تجاوزت الحد المسموح — تعامل مع هذه النتيجة بحذر ***")
    else:
        print()

    if report.kappa is not None:
        print(f"اتفاق المدققين (كابا): {report.kappa:.2f} "
              f"({report.n_agreement_pairs} زوج تقييم)")
    else:
        print("اتفاق المدققين: لا توجد عناصر كافية بمراجعتين بعد")

    if report.golden_accuracy:
        print("\nدقة كل مدقق على العناصر المرجعية:")
        for auditor_id, acc in sorted(report.golden_accuracy.items(), key=lambda x: x[1]):
            flag = "  <-- راجع هذا المدقق" if acc < 0.7 else ""
            print(f"  {auditor_id}: {acc * 100:.0f}%{flag}")

    translated = translate_defects(report.defect_counts, spec)
    if translated:
        print("\nالمشاكل المرصودة:")
        for label_ar, count in translated:
            print(f"  {label_ar}: {count}")

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

    print_report(report, spec, args.mode)

    if report.mean_score is not None:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        chart_path = settings.data_dir.parent / "reports" / f"{spec.key}_{args.mode}.png"
        chart_path.parent.mkdir(parents=True, exist_ok=True)
        render_chart(report, spec, chart_path)
        print(f"Chart saved to {chart_path}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
