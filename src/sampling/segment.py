"""
Segment-random sampling.

The rule agreed on for unamended legislation:
    <= 6 articles  -> audit all of them (census; cheaper than sampling logic
                       and strictly better data)
    >= 7 articles  -> take the middle 20th-80th percentile of the article
                       list, split it into 4 equal segments, draw 1 article
                       at random from each segment, with a fixed seed

Deliberately NOT first/last N: those positions are formulaic (short title,
تعريفات, boilerplate enactment clause) and would bias the score upward by
sampling almost exclusively from the part of the document least likely to
contain a real defect.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

CENSUS_THRESHOLD = 6      # <= this many articles: audit all of them
MIDDLE_SEGMENTS = 4        # segments drawn from within the middle range
MIDDLE_LOW_PCTL = 0.20
MIDDLE_HIGH_PCTL = 0.80


@dataclass
class SamplingDecision:
    mode: str                 # "census" or "segment_random"
    selected_indices: list[int]     # 0-based positions into the article list
    population_size: int


def segment_random_indices(
    start: int, end: int, k: int, rng: random.Random,
) -> list[int]:
    """
    Split the inclusive range [start, end] into k segments as evenly as
    possible and draw one random index from each. If the range holds fewer
    than k positions, every position is used instead (never invents indices
    that don't exist).
    """
    population = list(range(start, end + 1))
    if len(population) <= k:
        return sorted(population)

    segments: list[list[int]] = []
    n = len(population)
    base, extra = divmod(n, k)
    cursor = 0
    for i in range(k):
        size = base + (1 if i < extra else 0)
        segments.append(population[cursor:cursor + size])
        cursor += size

    return sorted(rng.choice(seg) for seg in segments if seg)


def decide_article_sample(article_count: int, rng: random.Random) -> SamplingDecision:
    """
    Apply the census-vs-segment rule for one unamended legislation.
    Indices are 0-based positions into that legislation's article list, in
    the order the source provides (which is document order).
    """
    if article_count <= 0:
        return SamplingDecision("census", [], article_count)

    if article_count <= CENSUS_THRESHOLD:
        return SamplingDecision("census", list(range(article_count)), article_count)

    low = int(article_count * MIDDLE_LOW_PCTL)
    high = int(article_count * MIDDLE_HIGH_PCTL) - 1
    high = max(high, low)                       # guard tiny ranges near 7
    high = min(high, article_count - 1)

    indices = segment_random_indices(low, high, MIDDLE_SEGMENTS, rng)
    return SamplingDecision("segment_random", indices, article_count)
