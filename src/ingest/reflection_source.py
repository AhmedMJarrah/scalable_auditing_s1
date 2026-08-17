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

Matching before/after state per article: "before" always comes from the
source's own per-amendment Base_Articles field (trusted as authoritative —
per the source, it already represents "the articles at that stage"). When
an article in Reflected_Articles has no match there, match_status
distinguishes a genuinely new article (number beyond anything known to
exist before) from a suspected renumbering orphan (number that plausibly
existed but wasn't found — e.g. an earlier deletion shifted everything
after it, or the article uses non-numeric numbering like "5 مكرر"). See
ReflectionItem.match_status and _classify_match().

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
    # 'matched'            — article_number found in this amendment's own
    #                        declared Base_Articles; ordinary case.
    # 'new_article'        — not found, and its number is beyond every
    #                        number known to exist before this amendment —
    #                        consistent with a genuinely new article
    #                        appended at the end.
    # 'orphan_suspected'   — not found, but its number falls WITHIN the
    #                        range that existed before this amendment (or
    #                        isn't a plain integer at all, e.g. "5 مكرر").
    #                        This is the renumbering case: the article
    #                        plausibly existed under a different number and
    #                        the by-number match silently failed to find
    #                        it. Needs a human to verify, not an automatic
    #                        pass — do not treat base_text=None here the
    #                        same as a confirmed new article.
    match_status: str = "matched"


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


def _classify_match(
    article_number: str, before: Article | None, cumulative_known_numbers: set[int],
) -> str:
    """
    Decide whether a reflected article with no exact-number match in this
    amendment's own declared Base_Articles is a genuinely new article, or a
    suspected renumbering orphan.

    cumulative_known_numbers is every numeric article number seen ANYWHERE
    in this legislation's history up to (not including) this amendment —
    the original Base_Articles plus every prior amendment's Reflected_Articles.
    Using the full history here, not just this one amendment's own base
    list, is what makes this catch a renumbering case: if article "7"
    existed two amendments ago and now can't be matched, that is exactly
    the situation worth flagging, even though it isn't in THIS amendment's
    own (possibly incomplete or shifted) base list.
    """
    if before is not None:
        return "matched"

    try:
        this_number = int(article_number)
    except ValueError:
        # Non-numeric numbering (e.g. "5 مكرر" — an inserted article that
        # deliberately avoids renumbering everything after it). Magnitude
        # comparison doesn't apply; flag for a human rather than guessing.
        return "orphan_suspected"

    if this_number in cumulative_known_numbers:
        # This number existed at some point in the legislation's history,
        # yet has no match right here — the renumbering case.
        return "orphan_suspected"

    highest_known = max(cumulative_known_numbers, default=0)
    return "new_article" if this_number > highest_known else "orphan_suspected"


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

    mods_raw = raw.get("Mod_Legs", [])
    metadata_items = [MetadataItem(base_ref.legislation_id, base_ref.leg_name, "base", raw)]
    reflection_items: list[ReflectionItem] = []
    amendment_ids: list[str] = []

    def _as_int(n: str) -> int | None:
        try:
            return int(n)
        except ValueError:
            return None

    cumulative_known_numbers: set[int] = {
        n for n in (_as_int(a.article_number) for a in base_articles) if n is not None
    }

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
            status = _classify_match(art.article_number, before, cumulative_known_numbers)
            if status == "orphan_suspected":
                log.warning("reflection.orphan_suspected", extra={
                    "legislation_id": base_ref.legislation_id,
                    "amendment_id": mod_ref.legislation_id,
                    "article_number": art.article_number,
                })
            reflection_items.append(ReflectionItem(
                legislation_id=base_ref.legislation_id,
                amendment_id=mod_ref.legislation_id,
                article_number=art.article_number,
                base_text=before.text if before else None,
                reflected_text=art.text,
                match_status=status,
            ))

        # Extend the cumulative set with this amendment's article numbers —
        # so the NEXT amendment's classification sees everything up to now.
        cumulative_known_numbers |= {
            n for n in (_as_int(a.article_number) for a in reflected) if n is not None
        }

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
