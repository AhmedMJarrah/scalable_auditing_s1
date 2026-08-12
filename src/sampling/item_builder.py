"""
Turns a legislation sample into AuditItem candidates for one audit type.

This is where the per-audit-type unit rules actually get applied:
    metadata           -> one item per sampled legislation
    chain               -> one item per sampled legislation that HAS
                            amendments (a legislation with none has nothing
                            to check a chain for — see note below)
    reflection          -> census: one item per amendment-touched article,
                            for sampled legislation that has amendments
    article_integrity   -> census or segment-random-4 (segment.py), for
                            sampled legislation with NO amendments

Note on chain: legislation with zero amendments produces no chain item.
This is a default, not a settled design decision — flag it back if you'd
rather audit "confirmed no amendments exist" as a chain item too; it changes
the chain sample's effective population.

Golden-candidate marking: a seeded random subset of each spec's items is
flagged is_golden=True. This only marks WHICH items become golden checks —
the actual known-correct answer (GoldenAnswer row) is filled in separately
by whoever curates them; sampling cannot invent ground truth.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from src.core.logging_setup import get_logger, stage
from src.core.spec import AuditSpec
from src.db.models import build_item_identity_key
from src.ingest.reflection_source import (
    ArticleIntegrityItem, ChainItem, MetadataItem, ReflectionItem,
)
from src.sampling.legislation_sampler import LegislationRecord, SampleResult
from src.sampling.segment import decide_article_sample

log = get_logger(__name__)


@dataclass
class ItemCandidate:
    spec_key: str
    unit: str
    legislation_id: str
    amendment_id: str | None = None
    article_number: str | None = None
    is_golden: bool = False

    @property
    def identity_key(self) -> str:
        return build_item_identity_key(
            self.spec_key, self.legislation_id, self.amendment_id, self.article_number,
        )


@dataclass
class BuildResult:
    spec_key: str
    candidates: list[ItemCandidate] = field(default_factory=list)
    skipped_no_amendments: int = 0     # chain items skipped (see module docstring)

    def deduplicated(self) -> list[ItemCandidate]:
        """
        Two independent leg_type draws (law, bylaw) never overlap in
        practice, but ingestion re-runs could hand back the same candidate
        twice — collapse by identity_key defensively before this reaches
        the database, where the unique constraint would otherwise reject
        the whole batch on the second occurrence.
        """
        seen: dict[str, ItemCandidate] = {}
        for c in self.candidates:
            seen[c.identity_key] = c
        return list(seen.values())


def _mark_golden(candidates: list[ItemCandidate], fraction: float, seed: int) -> None:
    if not candidates or fraction <= 0:
        return
    ordered = sorted(candidates, key=lambda c: c.identity_key)   # deterministic
    k = round(len(ordered) * fraction)
    rng = random.Random(seed)
    for c in rng.sample(ordered, k=k):
        c.is_golden = True


def build_metadata_items(
    spec: AuditSpec, samples: list[SampleResult], seed: int,
) -> BuildResult:
    result = BuildResult(spec_key=spec.key)
    for sample in samples:
        for rec in sample.selected:
            result.candidates.append(
                ItemCandidate(spec.key, "legislation", rec.legislation_id)
            )
    _mark_golden(result.candidates, spec.sampling.golden_fraction, seed)
    return result


def build_chain_items(
    spec: AuditSpec, samples: list[SampleResult], seed: int,
) -> BuildResult:
    result = BuildResult(spec_key=spec.key)
    for sample in samples:
        for rec in sample.selected:
            if not rec.has_amendments:
                result.skipped_no_amendments += 1
                continue
            result.candidates.append(
                ItemCandidate(spec.key, "chain", rec.legislation_id)
            )
    _mark_golden(result.candidates, spec.sampling.golden_fraction, seed)
    return result


def build_reflection_items(
    spec: AuditSpec,
    samples: list[SampleResult],
    reflection_items_by_leg: dict[str, list[ReflectionItem]],
    seed: int,
) -> BuildResult:
    result = BuildResult(spec_key=spec.key)
    # sorted(), not a bare set iteration: PYTHONHASHSEED randomizes set
    # iteration order between process runs, so without this, batch-release
    # ordering would silently differ every time this script runs, even with
    # the identical seed and identical sampled legislation.
    sampled_ids = sorted({rec.legislation_id for s in samples for rec in s.selected})

    for leg_id in sampled_ids:
        for item in reflection_items_by_leg.get(leg_id, []):
            result.candidates.append(ItemCandidate(
                spec.key, "article", item.legislation_id,
                amendment_id=item.amendment_id, article_number=item.article_number,
            ))
    _mark_golden(result.candidates, spec.sampling.golden_fraction, seed)
    return result


def build_article_integrity_items(
    spec: AuditSpec,
    samples: list[SampleResult],
    articles_by_leg: dict[str, list[str]],   # legislation_id -> article_number list, in order
    seed: int,
) -> BuildResult:
    result = BuildResult(spec_key=spec.key)
    rng = random.Random(seed)

    for sample in samples:
        for rec in sample.selected:
            if rec.has_amendments:
                continue
            numbers = articles_by_leg.get(rec.legislation_id, [])
            decision = decide_article_sample(len(numbers), rng)
            for idx in decision.selected_indices:
                result.candidates.append(ItemCandidate(
                    spec.key, "article", rec.legislation_id,
                    article_number=numbers[idx],
                ))

    _mark_golden(result.candidates, spec.sampling.golden_fraction, seed)
    return result


def summarize(results: list[BuildResult]) -> dict:
    return {
        r.spec_key: {
            "items": len(r.deduplicated()),
            "golden": sum(1 for c in r.candidates if c.is_golden),
            "skipped_no_amendments": r.skipped_no_amendments,
        }
        for r in results
    }
