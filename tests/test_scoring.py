from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from src.core.spec import load_specs
from src.scoring.compute import (
    ItemScore, aggregate, build_report, cannot_determine_rate,
    cohens_kappa, confidence_interval, defect_breakdown, golden_accuracy,
    score_item, score_single_response,
)


@pytest.fixture(scope="module")
def specs():
    configs = Path(__file__).resolve().parents[1] / "configs"
    return load_specs(configs)


# --- score_single_response --------------------------------------------
def test_all_correct_scores_one(specs) -> None:
    spec = specs["metadata"]
    answers = {"status": "correct", "name": "correct", "number": "correct",
               "year": "correct", "publication_date": "correct"}
    score, cd, total = score_single_response(answers, spec)
    assert score == 1.0
    assert cd == 0


def test_all_incorrect_scores_zero(specs) -> None:
    spec = specs["metadata"]
    answers = {"status": "incorrect", "name": "incorrect", "number": "incorrect",
               "year": "incorrect", "publication_date": "incorrect"}
    score, cd, total = score_single_response(answers, spec)
    assert score == 0.0


def test_weighted_field_dominates_score(specs) -> None:
    """status has weight 0.45, the highest — getting only it wrong should
    hurt more than getting a 0.05-weight field wrong."""
    spec = specs["metadata"]
    status_wrong = {"status": "incorrect", "name": "correct", "number": "correct",
                     "year": "correct", "publication_date": "correct"}
    minor_wrong = {"status": "correct", "name": "correct", "number": "correct",
                   "year": "correct", "publication_date": "incorrect"}
    s1, _, _ = score_single_response(status_wrong, spec)
    s2, _, _ = score_single_response(minor_wrong, spec)
    assert s1 < s2
    assert s1 == pytest.approx(0.55)
    assert s2 == pytest.approx(0.95)


def test_cannot_determine_excluded_redistributes_remaining_weight(specs) -> None:
    """status (0.45) is cannot_determine and excluded; the other 4 fields
    (summing to 0.55) are all correct -> score should be 1.0 among what
    WAS judged, not penalized for the abstention."""
    spec = specs["metadata"]
    answers = {"status": "cannot_determine", "name": "correct", "number": "correct",
               "year": "correct", "publication_date": "correct"}
    score, cd, total = score_single_response(answers, spec)
    assert score == pytest.approx(1.0)
    assert cd == 1


def test_all_cannot_determine_with_exclude_policy_is_none(specs) -> None:
    spec = specs["metadata"]
    assert spec.scoring.cannot_determine_policy == "exclude"
    answers = {k: "cannot_determine" for k in
               ("status", "name", "number", "year", "publication_date")}
    score, cd, total = score_single_response(answers, spec)
    assert score is None
    assert cd == 5


def test_missing_field_treated_as_cannot_determine(specs) -> None:
    spec = specs["metadata"]
    answers = {"status": "correct", "name": "correct", "number": "correct", "year": "correct"}
    # publication_date entirely absent from the response
    score, cd, total = score_single_response(answers, spec)
    assert cd == 1
    assert score == pytest.approx(1.0)   # remaining weight all correct


def test_count_as_incorrect_policy_penalizes_abstention() -> None:
    """A spec with count_as_incorrect should score an all-cannot_determine
    response as 0.0, not None."""
    from src.core.spec import AuditSpec, Sampling, Scoring, AuditField
    spec = AuditSpec(
        key="t", spec_version=1, title_ar="t", title_en="t", unit="legislation",
        applies_to=["law"], sheet_env_key="SHEET_T",
        sampling=Sampling(sample_size={"law": 10}),
        scoring=Scoring(cannot_determine_policy="count_as_incorrect"),
        fields=[AuditField(key="x", label_ar="x", type="verdict", weight=1.0)],
    )
    score, cd, total = score_single_response({"x": "cannot_determine"}, spec)
    assert score == 0.0
    assert cd == 1


# --- score_item (combining multiple responses) --------------------------
def test_two_responses_averaged(specs) -> None:
    spec = specs["metadata"]
    r1 = {"status": "correct", "name": "correct", "number": "correct",
          "year": "correct", "publication_date": "correct"}
    r2 = {"status": "incorrect", "name": "correct", "number": "correct",
          "year": "correct", "publication_date": "correct"}
    score, _, _ = score_item([r1, r2], spec)
    # r1=1.0, r2=0.55 -> average 0.775
    assert score == pytest.approx((1.0 + 0.55) / 2)


def test_single_response_item_unaffected(specs) -> None:
    spec = specs["metadata"]
    r1 = {"status": "correct", "name": "correct", "number": "correct",
          "year": "correct", "publication_date": "correct"}
    score, _, _ = score_item([r1], spec)
    assert score == 1.0


def test_no_responses_is_none(specs) -> None:
    spec = specs["metadata"]
    score, cd, total = score_item([], spec)
    assert score is None


# --- aggregate ------------------------------------------------------------
def test_mean_per_unit_prevents_one_legislation_dominating(specs) -> None:
    spec = specs["metadata"]
    # legislation A: one perfect item. legislation B: ten items, all zero.
    # flat mean would be dominated by B; mean_per_unit treats A and B equally.
    scores = [ItemScore("k_a", "A", 1.0, 1, 0, 5)]
    scores += [ItemScore(f"k_b{i}", "B", 0.0, 1, 0, 5) for i in range(10)]
    mean, n = aggregate(scores, spec)
    assert mean == pytest.approx(0.5)   # (1.0 + 0.0) / 2 legislations, not 1/11


def test_mean_per_decision_flat_average() -> None:
    from src.core.spec import AuditSpec, Sampling, Scoring, AuditField
    spec = AuditSpec(
        key="t", spec_version=1, title_ar="t", title_en="t", unit="legislation",
        applies_to=["law"], sheet_env_key="SHEET_T2",
        sampling=Sampling(sample_size={"law": 10}),
        scoring=Scoring(aggregate="mean_per_decision"),
        fields=[AuditField(key="x", label_ar="x", type="verdict", weight=1.0)],
    )
    scores = [ItemScore("k_a", "A", 1.0, 1, 0, 1)]
    scores += [ItemScore(f"k_b{i}", "B", 0.0, 1, 0, 1) for i in range(10)]
    mean, n = aggregate(scores, spec)
    assert mean == pytest.approx(1 / 11)   # flat average, B dominates


def test_none_scores_excluded_from_aggregate(specs) -> None:
    spec = specs["metadata"]
    scores = [ItemScore("k1", "A", 1.0, 1, 0, 5), ItemScore("k2", "B", None, 0, 5, 5)]
    mean, n = aggregate(scores, spec)
    assert mean == 1.0
    assert n == 1


def test_all_none_aggregate_returns_none(specs) -> None:
    spec = specs["metadata"]
    scores = [ItemScore("k1", "A", None, 0, 5, 5)]
    mean, n = aggregate(scores, spec)
    assert mean is None
    assert n == 0


# --- confidence_interval -------------------------------------------------
def test_ci_brackets_the_mean() -> None:
    values = [0.9, 0.85, 0.95, 0.8, 0.9, 1.0, 0.75, 0.9, 0.85, 0.95] * 5
    low, high = confidence_interval(values)
    mean = sum(values) / len(values)
    assert low < mean < high


def test_ci_clipped_to_zero_one() -> None:
    values = [1.0] * 20
    low, high = confidence_interval(values)
    assert high <= 1.0
    assert low <= 1.0


def test_ci_none_for_tiny_sample() -> None:
    assert confidence_interval([0.9]) == (None, None)
    assert confidence_interval([]) == (None, None)


def test_ci_narrower_with_more_data() -> None:
    small = [0.7, 0.9, 0.8, 0.85, 0.75]
    large = small * 20
    low_s, high_s = confidence_interval(small)
    low_l, high_l = confidence_interval(large)
    assert (high_l - low_l) < (high_s - low_s)


# --- defect_breakdown -----------------------------------------------------
def test_defect_breakdown_counts_multi_select(specs) -> None:
    field = specs["chain"].field_by_key("defect_types")
    responses = [
        {"defect_types": ["taadil_mafqud", "tartib_khati"]},
        {"defect_types": ["taadil_mafqud"]},
        {"defect_types": []},
        {},  # missing entirely
    ]
    counts = defect_breakdown(responses, field)
    assert counts["taadil_mafqud"] == 2
    assert counts["tartib_khati"] == 1
    assert sum(counts.values()) == 3


def test_defect_breakdown_handles_string_not_list(specs) -> None:
    field = specs["chain"].field_by_key("defect_types")
    counts = defect_breakdown([{"defect_types": "taadil_mafqud"}], field)
    assert counts["taadil_mafqud"] == 1


# --- cohens_kappa -----------------------------------------------------
def test_kappa_perfect_agreement() -> None:
    pairs = [("correct", "correct"), ("incorrect", "incorrect")] * 10
    assert cohens_kappa(pairs) == pytest.approx(1.0)


def test_kappa_systematic_disagreement_is_low() -> None:
    pairs = [("correct", "incorrect"), ("incorrect", "correct")] * 10
    k = cohens_kappa(pairs)
    assert k is not None
    assert k < 0.1


def test_kappa_excludes_cannot_determine() -> None:
    pairs = [("correct", "correct"), ("cannot_determine", "correct"),
             ("correct", "correct")]
    k = cohens_kappa(pairs)
    assert k == pytest.approx(1.0)   # only the 2 usable pairs count


def test_kappa_none_with_insufficient_data() -> None:
    assert cohens_kappa([]) is None
    assert cohens_kappa([("correct", "correct")]) is None


def test_kappa_handles_all_same_answer_without_crashing() -> None:
    pairs = [("correct", "correct")] * 5
    k = cohens_kappa(pairs)
    assert k == 1.0


# --- golden_accuracy -----------------------------------------------------
def test_golden_accuracy_computed_per_auditor() -> None:
    by_auditor = {
        "a1": [
            ({"status": "correct", "name": "correct"}, {"status": "correct", "name": "correct"}),
            ({"status": "incorrect"}, {"status": "correct"}),
        ],
        "a2": [
            ({"status": "correct"}, {"status": "correct"}),
        ],
    }
    result = golden_accuracy(by_auditor)
    assert result["a1"] == pytest.approx(2 / 3)   # 2 of 3 field-comparisons correct
    assert result["a2"] == 1.0


def test_golden_accuracy_empty_for_no_overlap_keys() -> None:
    by_auditor = {"a1": [({"other_field": "x"}, {"status": "correct"})]}
    result = golden_accuracy(by_auditor)
    assert "a1" not in result


# --- cannot_determine_rate -------------------------------------------------
def test_cannot_determine_rate_basic() -> None:
    assert cannot_determine_rate(10, 100) == 0.1
    assert cannot_determine_rate(0, 0) == 0.0


# --- build_report (integration of the pieces) ------------------------------
def test_build_report_sample_mode_includes_ci(specs) -> None:
    spec = specs["metadata"]
    scores = [ItemScore(f"k{i}", f"leg{i}", 0.9, 1, 0, 5) for i in range(20)]
    report = build_report(spec, scores, is_sample=True)
    assert report.ci_low is not None
    assert report.mean_score == pytest.approx(0.9)


def test_build_report_census_mode_omits_ci(specs) -> None:
    spec = specs["metadata"]
    scores = [ItemScore(f"k{i}", f"leg{i}", 0.9, 1, 0, 5) for i in range(20)]
    report = build_report(spec, scores, is_sample=False)
    assert report.ci_low is None
    assert report.ci_high is None


def test_build_report_flags_high_cannot_determine_rate(specs) -> None:
    spec = specs["metadata"]
    # max_cannot_determine_rate is 0.20 for metadata; push it over.
    scores = [ItemScore(f"k{i}", f"leg{i}", 0.9, 1, cd, 5) for i, cd in
              enumerate([4] * 20)]  # 4 of 5 fields cannot_determine every time -> 80%
    report = build_report(spec, scores, is_sample=True)
    assert report.over_cannot_determine_threshold is True
