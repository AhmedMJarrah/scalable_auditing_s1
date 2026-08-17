from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.ingest.reflection_source import (
    SourceDataError, ingest, load_source_file, parse_legislation,
)

FIXTURE = Path(__file__).parent / "fixture_reflection_sample.json"


def test_fixture_parses_end_to_end() -> None:
    result = ingest(FIXTURE)
    assert result["errors"] == []
    assert len(result["chain_items"]) == 1
    assert len(result["metadata_items"]) == 2          # base + 1 amendment
    assert len(result["reflection_items"]) == 1         # 1 reflected article
    assert result["article_integrity_items"] == []      # has amendments -> no census


def test_chain_item_lists_amendments_oldest_first() -> None:
    result = ingest(FIXTURE)
    chain = result["chain_items"][0]
    assert chain.legislation_id == "10/2000"
    assert chain.amendment_ids == ["5/2003"]


def test_reflection_item_captures_before_and_after() -> None:
    result = ingest(FIXTURE)
    item = result["reflection_items"][0]
    assert item.legislation_id == "10/2000"
    assert item.amendment_id == "5/2003"
    assert item.article_number == "1"
    assert item.base_text == "يسمى هذا القانون قانون التجارة لسنة 2000."
    assert item.reflected_text.endswith("المعدل.")
    assert item.base_text != item.reflected_text


def test_unamended_legislation_produces_article_integrity_candidates() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw[0]["Mod_Legs"] = []
    _, _, chain, meta, refl, integrity = parse_legislation(raw[0])
    assert chain.amendment_ids == []
    assert len(meta) == 1
    assert refl == []
    assert {a.article_number for a in integrity} == {"1", "2"}


def test_new_article_introduced_by_amendment_has_no_base_text() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw[0]["Mod_Legs"][0]["Reflected_Articles"].append({
        "text": "مادة جديدة أضافها التعديل.", "title": "- المادة 3",
        "article_number": "3", "enforcement_date": "01-02-2003",
    })
    _, _, _, _, refl, _ = parse_legislation(raw[0])
    new_article = next(r for r in refl if r.article_number == "3")
    assert new_article.base_text is None


def test_missing_required_leg_field_raises() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    del raw[0]["Leg_Number"]
    with pytest.raises(SourceDataError, match="Leg_Number"):
        parse_legislation(raw[0])


def test_missing_required_article_field_raises() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    del raw[0]["Base_Articles"][0]["article_number"]
    with pytest.raises(SourceDataError, match="article_number"):
        parse_legislation(raw[0])


def test_duplicate_article_number_raises() -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    dup = dict(raw[0]["Base_Articles"][0])
    raw[0]["Base_Articles"].append(dup)
    with pytest.raises(SourceDataError, match="duplicate article_number"):
        parse_legislation(raw[0])


def test_one_bad_record_does_not_block_the_rest(tmp_path) -> None:
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    good = raw[0]
    bad = json.loads(json.dumps(good))
    del bad["Leg_Number"]
    combined = [bad, good]
    path = tmp_path / "mixed.json"
    path.write_text(json.dumps(combined, ensure_ascii=False), encoding="utf-8")

    result = ingest(path)
    assert len(result["errors"]) == 1
    assert len(result["chain_items"]) == 1


def test_top_level_must_be_a_list(tmp_path) -> None:
    path = tmp_path / "not_a_list.json"
    path.write_text(json.dumps({"oops": True}), encoding="utf-8")
    with pytest.raises(SourceDataError, match="top-level JSON array"):
        load_source_file(path)


def test_arabic_text_survives_the_round_trip() -> None:
    result = ingest(FIXTURE)
    assert "قانون التجارة" in result["reflection_items"][0].reflected_text


# --- match_status: renumbering / orphan detection --------------------------
def test_ordinary_matched_article_has_matched_status() -> None:
    result = ingest(FIXTURE)
    item = result["reflection_items"][0]
    assert item.match_status == "matched"


def test_genuinely_new_article_beyond_known_range_is_new_article() -> None:
    """An article number higher than anything that existed before this
    amendment is consistent with a real addition at the end."""
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # base article count for this fixture's amendment is 1 (article "1")
    raw[0]["Mod_Legs"][0]["Reflected_Articles"].append({
        "text": "مادة جديدة أضافها التعديل.", "title": "- المادة 3",
        "article_number": "3", "enforcement_date": "01-02-2003",
    })
    _, _, _, _, refl, _ = parse_legislation(raw[0])
    new_article = next(r for r in refl if r.article_number == "3")
    assert new_article.match_status == "new_article"
    assert new_article.base_text is None


def test_missing_article_within_legislation_history_is_orphan_suspected() -> None:
    """The renumbering case: article "2" existed in the legislation's
    ORIGINAL Base_Articles (this fixture has articles 1 and 2), so if a
    later amendment's Reflected_Articles includes "2" but that amendment's
    own declared Base_Articles does not, that is exactly the situation
    worth flagging — the number was seen before, in this legislation's
    history, and now can't be matched."""
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw[0]["Mod_Legs"][0]["Reflected_Articles"].append({
        "text": "نص المادة الثانية بعد تعديل غير موثق ترقيمه.",
        "title": "- المادة 2", "article_number": "2",
        "enforcement_date": "01-02-2003",
    })
    _, _, _, _, refl, _ = parse_legislation(raw[0])
    suspicious = next(r for r in refl if r.article_number == "2")
    assert suspicious.match_status == "orphan_suspected"


def test_second_amendment_sees_first_amendments_articles_as_known_history() -> None:
    """Cumulative history must extend across amendments, not just the
    original base — otherwise a 2nd amendment can't recognize a number
    introduced by the 1st amendment as 'previously known'."""
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    # 1st amendment (already in the fixture) introduces nothing new beyond
    # article "1". Add a 2nd amendment that reflects article "5" — brand
    # new relative to everything seen so far (max known = 2) -> new_article.
    raw[0]["Mod_Legs"].append({
        "Leg_Name": "قانون معدل ثانٍ", "Leg_Number": "6", "Year": "2005",
        "Article_Count": "1",
        "Base_Articles": [],
        "Reflected_Articles": [{
            "text": "مادة جديدة كلياً.", "title": "- المادة 5",
            "article_number": "5", "enforcement_date": "01-01-2005",
        }],
    })
    _, _, _, _, refl, _ = parse_legislation(raw[0])
    second_amendment_article = next(
        r for r in refl if r.amendment_id == "6/2005" and r.article_number == "5"
    )
    assert second_amendment_article.match_status == "new_article"


def test_non_numeric_article_number_is_orphan_suspected() -> None:
    """Legal numbering like '5 مكرر' (an inserted article that deliberately
    avoids renumbering everything after it) can't be judged new-vs-orphan by
    magnitude — must be flagged for a human, never silently resolved."""
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw[0]["Mod_Legs"][0]["Reflected_Articles"].append({
        "text": "مادة مدرجة بين مادتين.", "title": "- المادة 1 مكرر",
        "article_number": "1 مكرر", "enforcement_date": "01-02-2003",
    })
    _, _, _, _, refl, _ = parse_legislation(raw[0])
    item = next(r for r in refl if r.article_number == "1 مكرر")
    assert item.match_status == "orphan_suspected"


def test_orphan_suspected_logs_a_warning(caplog) -> None:
    import logging
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    raw[0]["Mod_Legs"][0]["Reflected_Articles"].append({
        "text": "مادة مشبوهة.", "title": "- المادة 1 مكرر",
        "article_number": "1 مكرر", "enforcement_date": "01-02-2003",
    })
    with caplog.at_level(logging.WARNING):
        parse_legislation(raw[0])
    assert any("orphan_suspected" in r.message for r in caplog.records)
