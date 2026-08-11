"""
Ingestion adapter for the legislation JSON structure (Toqa's colleague, Aug 2026).

Source shape, per legislation:
    { Leg_Name, Leg_Number, Year, ..., Base_Articles: [...],
      Mod_Legs: [ { Leg_Name, ..., Base_Articles: [...], Reflected_Articles: [...] }, ... ] }
Mod_Legs is ordered oldest -> newest.

This module flattens that tree into three candidate tables the sampling and
scoring layers actually consume:

    chain_items       one row per legislation  -> feeds chain.yaml
    metadata_items     one row per legislation
                        + one row per amendment -> feeds metadata.yaml
    reflection_items   one row per (legislation, mod, article) touched by
                        that amendment                -> feeds reflection.yaml

Article-integrity candidates (legislation with NO Mod_Legs) are produced by
article_integrity_items().

Known gap (flag to the source team before building the audit UI screen):
this structure carries Base_Articles/Reflected_Articles per amendment, i.e.
the observed diff, but not the amendment's *instruction text*. The
reflection audit as specified can therefore verify "did the text change from
before to after," but not "does the change match what the amendment legally
ordered," unless that instruction text is supplied separately.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.core.logging_setup import get_logger, stage

log = get_logger(__name__)

REQUIRED_LEG_KEYS = ("Leg_Name", "Leg_Number", "Year")
REQUIRED_ARTICLE_KEYS = ("article_number", "text")


class SourceDataError(Exception):
    """Raised when the input JSON does not match the expected shape."""


@dataclass
class Article:
    article_number: str
    text: str
    title: str | None = None
    enforcement_date: str | None = None


@dataclass
class LegislationRef:
    """Identity of one legislation or amendment, independent of its articles."""
    leg_name: str
    leg_number: str
    year: str
    raw: dict[str, Any] = field(repr=False)

    @property
    def legislation_id(self) -> str:
        # number+year+name is the identity key agreed for laws; reused here
        # for bylaws so ids stay comparable across audit types.
        return f"{self.leg_number}/{self.year}"


@dataclass
class ChainItem:
    legislation_id: str
    leg_name: str
    amendment_ids: list[str]          # oldest -> newest, excludes the base


@dataclass
class MetadataItem:
    legislation_id: str
    leg_name: str
    role: str                         # 'base' or 'amendment'
    raw: dict[str, Any] = field(repr=False)


@dataclass
class ReflectionItem:
    legislation_id: str               # base legislation this chain belongs to
    amendment_id: str                 # the Mod_Leg that produced this change
    article_number: str
    base_text: str | None             # None if the article is newly introduced
    reflected_text: str


@dataclass
class ArticleIntegrityItem:
    legislation_id: str
    article_number: str
    text: str


def _require(d: dict, keys: tuple[str, ...], where: str) -> None:
    missing = [k for k in keys if k not in d or d[k] in (None, "")]
    if missing:
        raise SourceDataError(f"{where}: missing required field(s) {missing}")


def _parse_articles(raw_list: list[dict], where: str) -> list[Article]:
    articles: list[Article] = []
    seen_numbers: set[str] = set()
    for i, raw in enumerate(raw_list):
        _require(raw, REQUIRED_ARTICLE_KEYS, f"{where}[{i}]")
        num = str(raw["article_number"]).strip()
        if num in seen_numbers:
            raise SourceDataError(f"{where}: duplicate article_number {num!r}")
        seen_numbers.add(num)
        articles.append(Article(
            article_number=num,
            text=raw["text"],
            title=raw.get("title"),
            enforcement_date=raw.get("enforcement_date") or None,
        ))
    return articles


def parse_legislation(raw: dict[str, Any]) -> tuple[
    LegislationRef, list[Article], ChainItem, list[MetadataItem],
    list[ReflectionItem], list[ArticleIntegrityItem],
]:
    """Parse one top-level legislation record into every candidate table."""
    _require(raw, REQUIRED_LEG_KEYS, "legislation")
    base_ref = LegislationRef(
        leg_name=raw["Leg_Name"], leg_number=str(raw["Leg_Number"]),
        year=str(raw["Year"]), raw=raw,
    )
    base_articles = _parse_articles(raw.get("Base_Articles", []), "Base_Articles")
    base_by_number = {a.article_number: a for a in base_articles}

    mods_raw = raw.get("Mod_Legs", [])
    metadata_items = [MetadataItem(base_ref.legislation_id, base_ref.leg_name, "base", raw)]
    reflection_items: list[ReflectionItem] = []
    amendment_ids: list[str] = []

    for i, mod in enumerate(mods_raw):
        _require(mod, REQUIRED_LEG_KEYS, f"Mod_Legs[{i}]")
        mod_ref = LegislationRef(
            leg_name=mod["Leg_Name"], leg_number=str(mod["Leg_Number"]),
            year=str(mod["Year"]), raw=mod,
        )
        amendment_ids.append(mod_ref.legislation_id)
        metadata_items.append(MetadataItem(mod_ref.legislation_id, mod_ref.leg_name, "amendment", mod))

        reflected = _parse_articles(mod.get("Reflected_Articles", []), f"Mod_Legs[{i}].Reflected_Articles")
        mod_base = _parse_articles(mod.get("Base_Articles", []), f"Mod_Legs[{i}].Base_Articles")
        mod_base_by_number = {a.article_number: a for a in mod_base}

        if not reflected:
            log.warning("mod.no_reflected_articles", extra={
                "legislation_id": base_ref.legislation_id, "mod_id": mod_ref.legislation_id,
            })

        for art in reflected:
            before = mod_base_by_number.get(art.article_number)
            reflection_items.append(ReflectionItem(
                legislation_id=base_ref.legislation_id,
                amendment_id=mod_ref.legislation_id,
                article_number=art.article_number,
                base_text=before.text if before else None,
                reflected_text=art.text,
            ))
        # advance the running snapshot so a later amendment's "before" state
        # reflects earlier amendments, not just the original base text.
        for art in reflected:
            base_by_number[art.article_number] = art

    chain_item = ChainItem(
        legislation_id=base_ref.legislation_id,
        leg_name=base_ref.leg_name,
        amendment_ids=amendment_ids,
    )

    article_integrity_items: list[ArticleIntegrityItem] = []
    if not mods_raw:
        article_integrity_items = [
            ArticleIntegrityItem(base_ref.legislation_id, a.article_number, a.text)
            for a in base_articles
        ]

    return (
        base_ref, base_articles, chain_item, metadata_items,
        reflection_items, article_integrity_items,
    )


def load_source_file(path: Path) -> list[dict[str, Any]]:
    import json
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SourceDataError(f"{path.name}: expected a top-level JSON array")
    return raw


def ingest(path: Path) -> dict[str, list]:
    """
    Parse a full source file. Collects per-record errors rather than failing
    on the first bad legislation — one malformed record should not block the
    other 6,000.
    """
    records = load_source_file(path)
    chain_items, metadata_items, reflection_items, integrity_items = [], [], [], []
    errors: list[str] = []

    with stage("ingest.reflection_source", log, path=str(path), records=len(records)) as counters:
        for i, raw in enumerate(records):
            try:
                _, _, chain_item, meta, refl, integ = parse_legislation(raw)
            except SourceDataError as exc:
                errors.append(f"record[{i}]: {exc}")
                continue
            chain_items.append(chain_item)
            metadata_items.extend(meta)
            reflection_items.extend(refl)
            integrity_items.extend(integ)

        counters.update(
            legislations=len(chain_items),
            metadata_rows=len(metadata_items),
            reflection_candidates=len(reflection_items),
            article_integrity_candidates=len(integrity_items),
            errors=len(errors),
        )

    if errors:
        log.warning("ingest.record_errors", extra={"count": len(errors), "sample": errors[:5]})

    return {
        "chain_items": chain_items,
        "metadata_items": metadata_items,
        "reflection_items": reflection_items,
        "article_integrity_items": integrity_items,
        "errors": errors,
    }
