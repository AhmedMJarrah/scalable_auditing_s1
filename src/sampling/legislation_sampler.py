"""
Legislation-level sampling — the "100 laws + 100 bylaws" draw itself.

One independent draw per (audit_type, leg_type) pair, per the agreed design:
each of metadata / chain / reflection / article_integrity gets its own
independent 100-per-type sample, not a shared pool.

Determinism: population is sorted by legislation_id before sampling, so the
same seed always produces the same sample regardless of the order records
happened to arrive from ingestion (dict/JSON order is not guaranteed to be
stable across runs or across a re-export from the source system).
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from src.core.logging_setup import get_logger

log = get_logger(__name__)


@dataclass
class LegislationRecord:
    """Minimal shape the sampler needs — populated from real ingestion or
    the synthetic generator, never constructed by hand elsewhere."""
    legislation_id: str
    leg_type: str                 # "law" | "bylaw"
    has_amendments: bool
    article_count: int


@dataclass
class SampleResult:
    leg_type: str
    requested: int
    population_size: int
    selected: list[LegislationRecord]

    @property
    def under_populated(self) -> bool:
        return self.population_size < self.requested


def sample_population(
    population: list[LegislationRecord],
    leg_type: str,
    sample_size: int,
    seed: int,
) -> SampleResult:
    """One independent, seeded draw for one leg_type."""
    subset = sorted(
        (r for r in population if r.leg_type == leg_type),
        key=lambda r: r.legislation_id,
    )
    rng = random.Random(seed)

    if len(subset) <= sample_size:
        if subset:
            log.warning("sampling.population_below_target", extra={
                "leg_type": leg_type, "requested": sample_size, "available": len(subset),
            })
        return SampleResult(leg_type, sample_size, len(subset), list(subset))

    selected = rng.sample(subset, k=sample_size)
    selected.sort(key=lambda r: r.legislation_id)   # stable, readable output
    return SampleResult(leg_type, sample_size, len(subset), selected)
