from __future__ import annotations

from src.sampling.legislation_sampler import LegislationRecord, sample_population


def _population(n: int, leg_type: str, amend_every: int = 3) -> list[LegislationRecord]:
    return [
        LegislationRecord(
            legislation_id=f"{i}/{2000 + i % 20}", leg_type=leg_type,
            has_amendments=(i % amend_every == 0), article_count=10 + i % 15,
        )
        for i in range(n)
    ]


def test_sample_size_respected_when_population_sufficient() -> None:
    pop = _population(500, "law")
    result = sample_population(pop, "law", 100, seed=1)
    assert len(result.selected) == 100
    assert not result.under_populated


def test_under_population_returns_everything_and_flags_it() -> None:
    pop = _population(40, "law")
    result = sample_population(pop, "law", 100, seed=1)
    assert len(result.selected) == 40
    assert result.under_populated


def test_deterministic_with_fixed_seed() -> None:
    pop = _population(500, "law")
    r1 = sample_population(pop, "law", 100, seed=20260804)
    r2 = sample_population(pop, "law", 100, seed=20260804)
    assert [r.legislation_id for r in r1.selected] == [r.legislation_id for r in r2.selected]


def test_different_seeds_produce_different_samples() -> None:
    pop = _population(500, "law")
    r1 = sample_population(pop, "law", 100, seed=1)
    r2 = sample_population(pop, "law", 100, seed=2)
    assert [r.legislation_id for r in r1.selected] != [r.legislation_id for r in r2.selected]


def test_leg_type_filtering_is_independent() -> None:
    """A law and a bylaw with the same number/year must not cross-pollute
    each other's sample — each leg_type is an independent draw."""
    pop = _population(200, "law") + _population(200, "bylaw")
    laws = sample_population(pop, "law", 50, seed=1)
    bylaws = sample_population(pop, "bylaw", 50, seed=1)
    assert all(r.leg_type == "law" for r in laws.selected)
    assert all(r.leg_type == "bylaw" for r in bylaws.selected)


def test_no_duplicates_in_sample() -> None:
    pop = _population(500, "law")
    result = sample_population(pop, "law", 100, seed=1)
    ids = [r.legislation_id for r in result.selected]
    assert len(ids) == len(set(ids))


def test_order_of_input_population_does_not_affect_result() -> None:
    pop = _population(500, "law")
    shuffled = list(reversed(pop))
    r1 = sample_population(pop, "law", 100, seed=42)
    r2 = sample_population(shuffled, "law", 100, seed=42)
    assert [r.legislation_id for r in r1.selected] == [r.legislation_id for r in r2.selected]
