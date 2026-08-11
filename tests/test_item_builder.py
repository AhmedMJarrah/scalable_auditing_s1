from __future__ import annotations

from src.core.spec import load_specs
from src.ingest.reflection_source import parse_legislation
from src.ingest.synthetic import generate_legislation
from src.sampling.item_builder import (
    build_article_integrity_items, build_chain_items, build_metadata_items,
    build_reflection_items,
)
from src.sampling.legislation_sampler import LegislationRecord, sample_population

CONFIGS_DIR = None  # set by conftest-free fixture below


def _load_specs():
    from pathlib import Path
    configs = Path(__file__).resolve().parents[1] / "configs"
    return load_specs(configs)


def _build_world(n=60, seed=7):
    raws = generate_legislation(n, "law", seed=seed)
    records, reflection_by_leg, articles_by_leg = [], {}, {}
    for raw in raws:
        base_ref, base_articles, chain_item, _meta, refl, _integrity = parse_legislation(raw)
        records.append(LegislationRecord(
            legislation_id=base_ref.legislation_id, leg_type="law",
            has_amendments=len(chain_item.amendment_ids) > 0,
            article_count=len(base_articles),
        ))
        reflection_by_leg[base_ref.legislation_id] = refl
        articles_by_leg[base_ref.legislation_id] = [a.article_number for a in base_articles]
    return records, reflection_by_leg, articles_by_leg


def test_metadata_one_item_per_sampled_legislation() -> None:
    specs = _load_specs()
    records, _, _ = _build_world()
    samples = [sample_population(records, "law", 20, seed=1)]
    build = build_metadata_items(specs["metadata"], samples, seed=1)
    assert len(build.candidates) == 20
    assert all(c.unit == "legislation" for c in build.candidates)
    assert all(c.amendment_id is None and c.article_number is None for c in build.candidates)


def test_chain_only_for_amended_legislation() -> None:
    specs = _load_specs()
    records, _, _ = _build_world()
    samples = [sample_population(records, "law", 30, seed=1)]
    build = build_chain_items(specs["chain"], samples, seed=1)

    amended_count = sum(1 for r in samples[0].selected if r.has_amendments)
    assert len(build.candidates) == amended_count
    assert build.skipped_no_amendments == 30 - amended_count
    assert all(c.unit == "chain" for c in build.candidates)


def test_reflection_is_census_of_touched_articles() -> None:
    specs = _load_specs()
    records, reflection_by_leg, _ = _build_world()
    samples = [sample_population(records, "law", 30, seed=1)]
    build = build_reflection_items(specs["reflection"], samples, reflection_by_leg, seed=1)

    expected = sum(
        len(reflection_by_leg.get(r.legislation_id, []))
        for r in samples[0].selected
    )
    assert len(build.candidates) == expected
    assert all(c.unit == "article" and c.amendment_id is not None for c in build.candidates)


def test_article_integrity_only_for_unamended_legislation() -> None:
    specs = _load_specs()
    records, _, articles_by_leg = _build_world()
    samples = [sample_population(records, "law", 40, seed=1)]
    build = build_article_integrity_items(specs["article_integrity"], samples, articles_by_leg, seed=1)

    touched_legs = {c.legislation_id for c in build.candidates}
    for leg_id in touched_legs:
        rec = next(r for r in samples[0].selected if r.legislation_id == leg_id)
        assert not rec.has_amendments


def test_article_integrity_respects_census_threshold() -> None:
    specs = _load_specs()
    records, _, articles_by_leg = _build_world(n=100)
    samples = [sample_population(records, "law", 60, seed=3)]
    build = build_article_integrity_items(specs["article_integrity"], samples, articles_by_leg, seed=3)

    by_leg: dict[str, int] = {}
    for c in build.candidates:
        by_leg[c.legislation_id] = by_leg.get(c.legislation_id, 0) + 1

    for leg_id, count in by_leg.items():
        total = len(articles_by_leg[leg_id])
        if total <= 6:
            assert count == total          # census
        else:
            assert count <= 4              # segment-random cap


def test_golden_fraction_approximately_respected() -> None:
    specs = _load_specs()
    records, _, _ = _build_world(n=300)
    samples = [sample_population(records, "law", 200, seed=1)]
    build = build_metadata_items(specs["metadata"], samples, seed=1)

    golden = sum(1 for c in build.candidates if c.is_golden)
    expected = round(len(build.candidates) * specs["metadata"].sampling.golden_fraction)
    assert golden == expected


def test_deduplication_collapses_repeated_identity() -> None:
    from src.sampling.item_builder import ItemCandidate
    from src.core.spec import load_specs as _ls
    specs = _load_specs()
    from src.sampling.item_builder import BuildResult
    result = BuildResult(spec_key="metadata")
    result.candidates = [
        ItemCandidate("metadata", "legislation", "10/2000"),
        ItemCandidate("metadata", "legislation", "10/2000"),   # duplicate
        ItemCandidate("metadata", "legislation", "11/2001"),
    ]
    deduped = result.deduplicated()
    assert len(deduped) == 2


def test_no_amendments_means_no_reflection_items() -> None:
    specs = _load_specs()
    records, reflection_by_leg, _ = _build_world()
    unamended_ids = {r.legislation_id for r in records if not r.has_amendments}
    for leg_id in unamended_ids:
        assert reflection_by_leg.get(leg_id, []) == []
