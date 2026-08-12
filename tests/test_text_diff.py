from __future__ import annotations

from src.diffing.text_diff import DiffSegment, render_html, similarity_ratio, word_diff

ORIGINAL = "يسمى هذا القانون قانون التجارة لسنة 2000."
AMENDED = ORIGINAL + " المعدل"                                   # pure append after the existing period
REPLACED = "يسمى هذا النظام نظام التجارة لسنة 2000."             # two words changed


def test_identical_text_is_all_equal() -> None:
    segments = word_diff(ORIGINAL, ORIGINAL)
    assert all(s.kind == "equal" for s in segments)
    assert " ".join(s.text for s in segments) == ORIGINAL


def test_new_article_with_no_before_is_a_single_insert() -> None:
    segments = word_diff(None, ORIGINAL)
    assert len(segments) == 1
    assert segments[0].kind == "insert"
    assert segments[0].text == ORIGINAL


def test_appended_word_detected_as_insert_not_full_rewrite() -> None:
    segments = word_diff(ORIGINAL, AMENDED)
    kinds = [s.kind for s in segments]
    assert "equal" in kinds
    assert "insert" in kinds
    assert "delete" not in kinds        # nothing was removed, only appended
    inserted_text = " ".join(s.text for s in segments if s.kind == "insert")
    assert "المعدل" in inserted_text


def test_word_replacement_detected_as_delete_plus_insert() -> None:
    segments = word_diff(ORIGINAL, REPLACED)
    kinds = {s.kind for s in segments}
    assert "delete" in kinds
    assert "insert" in kinds
    assert "equal" in kinds             # "لسنة 2000." is unchanged


def test_reconstructing_after_text_from_non_equal_and_equal_segments() -> None:
    """Every word in `after` must appear in either an equal or insert
    segment — nothing should be silently dropped by the diff."""
    segments = word_diff(ORIGINAL, AMENDED)
    reconstructed = " ".join(s.text for s in segments if s.kind in ("equal", "insert"))
    assert reconstructed.split() == AMENDED.split()


def test_empty_after_text_is_a_full_delete() -> None:
    """Symmetric with before=None -> single insert: before=X, after='' is a
    full deletion, and should be visible as one, not silently dropped."""
    segments = word_diff(ORIGINAL, "")
    assert len(segments) == 1
    assert segments[0].kind == "delete"
    assert segments[0].text == ORIGINAL


def test_similarity_ratio_identical_is_one() -> None:
    assert similarity_ratio(ORIGINAL, ORIGINAL) == 1.0


def test_similarity_ratio_new_article_is_zero() -> None:
    assert similarity_ratio(None, ORIGINAL) == 0.0


def test_similarity_ratio_small_change_is_high() -> None:
    assert similarity_ratio(ORIGINAL, AMENDED) >= 0.8


def test_similarity_ratio_bigger_change_is_lower() -> None:
    assert similarity_ratio(ORIGINAL, REPLACED) < similarity_ratio(ORIGINAL, AMENDED)


# --- HTML rendering -------------------------------------------------------
def test_render_html_wraps_each_segment_in_a_span() -> None:
    segments = [DiffSegment("قانون", "equal"), DiffSegment("المعدل", "insert")]
    out = render_html(segments)
    assert '<span class="diff-equal">قانون</span>' in out
    assert '<span class="diff-insert">المعدل</span>' in out


def test_render_html_escapes_special_characters() -> None:
    segments = [DiffSegment("<script>alert(1)</script>", "insert")]
    out = render_html(segments)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_render_html_round_trip_from_word_diff() -> None:
    segments = word_diff(ORIGINAL, AMENDED)
    out = render_html(segments)
    assert "diff-equal" in out
    assert "diff-insert" in out
    assert "<script" not in out
