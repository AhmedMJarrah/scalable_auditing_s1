from __future__ import annotations

import random

from src.sampling.segment import (
    CENSUS_THRESHOLD, decide_article_sample, segment_random_indices,
)


def test_at_or_below_threshold_is_census() -> None:
    rng = random.Random(1)
    for n in range(0, CENSUS_THRESHOLD + 1):
        decision = decide_article_sample(n, rng)
        assert decision.mode == "census"
        assert decision.selected_indices == list(range(n))


def test_just_above_threshold_is_segment_random() -> None:
    rng = random.Random(1)
    decision = decide_article_sample(CENSUS_THRESHOLD + 1, rng)
    assert decision.mode == "segment_random"
    assert 1 <= len(decision.selected_indices) <= 4


def test_segment_random_never_exceeds_four_for_large_documents() -> None:
    rng = random.Random(1)
    decision = decide_article_sample(200, rng)
    assert decision.mode == "segment_random"
    assert len(decision.selected_indices) == 4
    assert len(set(decision.selected_indices)) == 4    # no duplicate positions


def test_segment_random_indices_stay_in_range() -> None:
    rng = random.Random(1)
    for n in (7, 12, 30, 100, 500):
        decision = decide_article_sample(n, rng)
        for idx in decision.selected_indices:
            assert 0 <= idx < n


def test_segment_random_favors_neither_edge() -> None:
    """The whole point of segment sampling is to avoid title/boilerplate
    articles at the very start and the enactment clause at the very end."""
    rng = random.Random(1)
    decision = decide_article_sample(100, rng)
    assert min(decision.selected_indices) >= 15   # not article 1/2
    assert max(decision.selected_indices) <= 84   # not the last article


def test_deterministic_with_fixed_seed() -> None:
    d1 = decide_article_sample(50, random.Random(20260804))
    d2 = decide_article_sample(50, random.Random(20260804))
    assert d1.selected_indices == d2.selected_indices


def test_different_seeds_produce_different_draws() -> None:
    d1 = decide_article_sample(50, random.Random(1))
    d2 = decide_article_sample(50, random.Random(2))
    assert d1.selected_indices != d2.selected_indices


def test_segment_random_indices_uses_every_position_when_range_smaller_than_k() -> None:
    rng = random.Random(1)
    result = segment_random_indices(10, 11, k=4, rng=rng)   # only 2 positions available
    assert result == [10, 11]


def test_zero_articles_is_empty_census() -> None:
    rng = random.Random(1)
    decision = decide_article_sample(0, rng)
    assert decision.mode == "census"
    assert decision.selected_indices == []
