"""
Scoring — turns raw responses into the percentage, confidence interval, and
issue breakdown the accuracy report needs.

Design decisions this module enforces, all agreed earlier in the project:

- 'cannot_determine' answers are EXCLUDED from a field's denominator by
  default (spec.scoring.cannot_determine_policy), not counted as failures —
  they are missing evidence, not a defect. If the rate gets too high
  (spec.scoring.max_cannot_determine_rate), that is reported as its own
  warning, not silently folded into the score.
- Aggregation is 'mean_per_unit' by default: score each legislation first,
  then average across legislations — so one heavily-amended law (many
  article-level responses) cannot outweigh a simple one in the headline
  number.
- A confidence interval is only meaningful for a SAMPLE, never for a full
  census — this module computes the interval whenever asked, but the
  CALLER (the report script) decides whether to show it, based on whether
  the data came from run_sampling.py (sample) or release_batch.py (census).
- match_status ('orphan_suspected' etc.) never enters the score — see
  models.AuditItem.match_status and leasing.required_reviews(). It forces a
  second review; it does not change what "correct" means.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from src.core.spec import AuditField, AuditSpec

Verdict = Literal["correct", "incorrect", "cannot_determine"]
VERDICT_POINTS: dict[str, float] = {"correct": 1.0, "incorrect": 0.0}


@dataclass
class ItemScore:
    """One item's score, after combining every response submitted for it
    (normally 1; 2 for overlap/orphan-forced items)."""
    identity_key: str
    legislation_id: str
    score: float | None          # None if every relevant field was excluded
    n_responses: int
    cannot_determine_fields: int  # count of (response, field) pairs excluded
    total_scoreable_fields: int


@dataclass
class ScoreReport:
    spec_key: str
    mean_score: float | None
    n_items: int
    n_scored_items: int          # items that produced a real score (not None)
    ci_low: float | None = None
    ci_high: float | None = None
    cannot_determine_rate: float = 0.0
    over_cannot_determine_threshold: bool = False
    defect_counts: Counter = field(default_factory=Counter)
    kappa: float | None = None
    n_agreement_pairs: int = 0
    golden_accuracy: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------
def score_single_response(answers: dict, spec: AuditSpec) -> tuple[float | None, int, int]:
    """
    Weighted score for ONE response against ONE spec's verdict fields.

    Returns (score, cannot_determine_count, total_verdict_fields). score is
    None if every verdict field on this response was cannot_determine (or
    missing) AND the spec excludes them — nothing left to score.
    """
    scored_fields = spec.scored_fields()
    total = len(scored_fields)
    cannot_determine_count = 0
    weighted_sum = 0.0
    weight_used = 0.0

    for f in scored_fields:
        verdict = answers.get(f.key)
        if verdict == "cannot_determine" or verdict is None:
            cannot_determine_count += 1
            if spec.scoring.cannot_determine_policy == "count_as_incorrect":
                weighted_sum += 0.0 * f.weight
                weight_used += f.weight
            # else 'exclude': contributes nothing to either sum — the
            # field's weight is redistributed proportionally among the
            # fields that DID get an answer, via weight_used below.
            continue
        points = VERDICT_POINTS.get(verdict)
        if points is None:
            continue  # malformed answer — treated as excluded, not a crash
        weighted_sum += points * f.weight
        weight_used += f.weight

    if weight_used <= 0:
        return None, cannot_determine_count, total
    return weighted_sum / weight_used, cannot_determine_count, total


def score_item(responses: list[dict], spec: AuditSpec) -> tuple[float | None, int, int]:
    """
    Combine every response submitted for ONE item into a single score.
    Normally one response; two for overlap/orphan-forced items, in which
    case their individual scores are averaged.
    """
    scores = []
    total_cannot_determine = 0
    total_fields = 0
    for answers in responses:
        s, cd, tf = score_single_response(answers, spec)
        total_cannot_determine += cd
        total_fields += tf
        if s is not None:
            scores.append(s)

    if not scores:
        return None, total_cannot_determine, total_fields
    return sum(scores) / len(scores), total_cannot_determine, total_fields


# ---------------------------------------------------------------------
def aggregate(item_scores: list[ItemScore], spec: AuditSpec) -> tuple[float | None, int]:
    """
    Roll up item scores per spec.scoring.aggregate.

    'mean_per_unit'     — average per-legislation (each legislation's own
                          items are averaged first, THEN legislations are
                          averaged) so one heavily-amended law can't
                          dominate.
    'mean_per_decision' — flat average across every scored item.

    Returns (mean, n_scored_items_used).
    """
    scored = [s for s in item_scores if s.score is not None]
    if not scored:
        return None, 0

    if spec.scoring.aggregate == "mean_per_decision":
        return sum(s.score for s in scored) / len(scored), len(scored)

    per_legislation: dict[str, list[float]] = {}
    for s in scored:
        per_legislation.setdefault(s.legislation_id, []).append(s.score)
    leg_means = [sum(v) / len(v) for v in per_legislation.values()]
    return sum(leg_means) / len(leg_means), len(scored)


def confidence_interval(
    values: list[float], confidence: float = 0.95,
) -> tuple[float | None, float | None]:
    """
    Normal-approximation CI on the mean of a list of per-unit scores in
    [0, 1]. Only meaningful when `values` came from a SAMPLE — the caller
    decides whether to display this; a full census has no sampling error
    and this function does not know which situation it's in.

    Returns (low, high), clipped to [0, 1]. (None, None) for n < 2.
    """
    n = len(values)
    if n < 2:
        return None, None
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1)
    std_err = math.sqrt(variance / n)
    z = _z_for_confidence(confidence)
    low = max(0.0, mean - z * std_err)
    high = min(1.0, mean + z * std_err)
    return low, high


def _z_for_confidence(confidence: float) -> float:
    # Avoids a scipy dependency for the one value we actually need.
    table = {0.90: 1.645, 0.95: 1.960, 0.99: 2.576}
    return table.get(round(confidence, 2), 1.960)


# ---------------------------------------------------------------------
def defect_breakdown(responses: list[dict], field: AuditField) -> Counter:
    """Count multi_select defect codes across responses — this IS the
    chart your manager asked for; do not average or weight it."""
    counts: Counter = Counter()
    for answers in responses:
        values = answers.get(field.key) or []
        if isinstance(values, str):
            values = [values]
        counts.update(values)
    return counts


def cohens_kappa(pairs: list[tuple[Verdict, Verdict]]) -> float | None:
    """
    Inter-auditor agreement for double-reviewed items (overlap + forced
    orphan review). pairs is [(auditor_A_verdict, auditor_B_verdict), ...]
    for the SAME field on the SAME item. cannot_determine entries are
    excluded — kappa measures agreement on a judgement, and
    cannot_determine is an abstention, not a judgement.

    Returns None if fewer than 2 usable pairs (kappa is not meaningful on
    almost no data — do not report a number that looks precise but isn't).
    """
    usable = [
        (a, b) for a, b in pairs
        if a in ("correct", "incorrect") and b in ("correct", "incorrect")
    ]
    n = len(usable)
    if n < 2:
        return None

    agree = sum(1 for a, b in usable if a == b) / n
    p_a_correct = sum(1 for a, _ in usable if a == "correct") / n
    p_b_correct = sum(1 for _, b in usable if b == "correct") / n
    expected = p_a_correct * p_b_correct + (1 - p_a_correct) * (1 - p_b_correct)

    if expected >= 1.0:
        return 1.0 if agree >= 1.0 else 0.0
    return (agree - expected) / (1 - expected)


def golden_accuracy(
    responses_by_auditor: dict[str, list[tuple[dict, dict]]],
) -> dict[str, float]:
    """
    responses_by_auditor: auditor_id -> list of (submitted_answers, golden_answers)
    pairs, for items that auditor answered which happened to be golden.

    Returns auditor_id -> fraction of verdict fields matching the known
    correct answer. An auditor scoring far below their peers here is the
    signal for "clicking through without reading," not a judgment this
    function makes on its own — surface the number, let a human decide.
    """
    result: dict[str, float] = {}
    for auditor_id, pairs in responses_by_auditor.items():
        total, correct = 0, 0
        for submitted, golden in pairs:
            for key, golden_value in golden.items():
                if key not in submitted:
                    continue
                total += 1
                if submitted[key] == golden_value:
                    correct += 1
        if total > 0:
            result[auditor_id] = correct / total
    return result


def cannot_determine_rate(total_cannot_determine: int, total_fields: int) -> float:
    if total_fields == 0:
        return 0.0
    return total_cannot_determine / total_fields


def build_report(
    spec: AuditSpec,
    item_scores: list[ItemScore],
    is_sample: bool,
    defect_counts: Counter | None = None,
    kappa: float | None = None,
    n_agreement_pairs: int = 0,
    golden: dict[str, float] | None = None,
) -> ScoreReport:
    mean, n_scored = aggregate(item_scores, spec)

    ci_low = ci_high = None
    if is_sample and mean is not None:
        scored = [s.score for s in item_scores if s.score is not None]
        ci_low, ci_high = confidence_interval(scored)

    total_cd = sum(s.cannot_determine_fields for s in item_scores)
    total_fields = sum(s.total_scoreable_fields for s in item_scores)
    cd_rate = cannot_determine_rate(total_cd, total_fields)

    return ScoreReport(
        spec_key=spec.key,
        mean_score=mean,
        n_items=len(item_scores),
        n_scored_items=n_scored,
        ci_low=ci_low, ci_high=ci_high,
        cannot_determine_rate=cd_rate,
        over_cannot_determine_threshold=cd_rate > spec.scoring.max_cannot_determine_rate,
        defect_counts=defect_counts or Counter(),
        kappa=kappa, n_agreement_pairs=n_agreement_pairs,
        golden_accuracy=golden or {},
    )
