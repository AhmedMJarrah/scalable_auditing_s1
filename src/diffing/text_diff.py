"""
Word-level diff between an article's text before and after an amendment.

Feeds the two-panel reflection UI: base legislation on one side, the
selected amendment's changed articles on the other, with changes colored.

Comparison direction: each amendment against the state IMMEDIATELY BEFORE
IT — not the original base text. This matches what reflection_source.py
already tracks (its "running snapshot"): item.base_text for a legislation's
2nd amendment is the 1st amendment's result, not the original. That's also
the more useful comparison for an auditor — it isolates what THIS amendment
did, rather than mixing in drift from earlier amendments.

Tokenization is whitespace-based, which is a deliberate simplification: good
enough to highlight moved/changed words in Arabic legal text, not a full
linguistic tokenizer. Punctuation attached to a word (a comma, a closing
paren) travels with that word rather than being split out — acceptable for
this use case, worth knowing if diffs look coarser than expected around
punctuation-heavy sentences.
"""

from __future__ import annotations

import difflib
import html
from dataclasses import dataclass
from typing import Literal

SegmentKind = Literal["equal", "insert", "delete"]


@dataclass
class DiffSegment:
    text: str
    kind: SegmentKind


def word_diff(before: str | None, after: str) -> list[DiffSegment]:
    """
    Diff two article texts at word level.

    before=None means the article did not exist before this amendment (a
    newly introduced article) — the whole text is a single insert segment,
    since there is nothing to diff against.
    """
    if before is None:
        return [DiffSegment(after, "insert")] if after else []
    if before == after:
        return [DiffSegment(after, "equal")] if after else []

    before_words = before.split()
    after_words = after.split()
    matcher = difflib.SequenceMatcher(a=before_words, b=after_words, autojunk=False)

    segments: list[DiffSegment] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            segments.append(DiffSegment(" ".join(after_words[j1:j2]), "equal"))
        elif tag == "delete":
            segments.append(DiffSegment(" ".join(before_words[i1:i2]), "delete"))
        elif tag == "insert":
            segments.append(DiffSegment(" ".join(after_words[j1:j2]), "insert"))
        elif tag == "replace":
            segments.append(DiffSegment(" ".join(before_words[i1:i2]), "delete"))
            segments.append(DiffSegment(" ".join(after_words[j1:j2]), "insert"))
    return segments


def similarity_ratio(before: str | None, after: str) -> float:
    """0.0-1.0 — how much of the text is unchanged. Cheap signal for
    surfacing "barely changed" vs "heavily rewritten" articles in a list
    view before an auditor opens the full diff."""
    if before is None:
        return 0.0
    if before == after:
        return 1.0
    matcher = difflib.SequenceMatcher(a=before.split(), b=after.split(), autojunk=False)
    return matcher.ratio()


# CSS classes are deliberately generic (diff-insert / diff-delete /
# diff-equal), not colors — actual colors belong in the UI's stylesheet
# (step 9), not hardcoded into backend-generated HTML.
_CLASS_BY_KIND: dict[SegmentKind, str] = {
    "equal": "diff-equal", "insert": "diff-insert", "delete": "diff-delete",
}


def render_html(segments: list[DiffSegment]) -> str:
    """
    Render diff segments as inline-safe HTML: <span class="diff-*">text</span>
    per segment, each piece escaped individually. RTL and font choices are
    the template/stylesheet's job, not this function's.
    """
    parts = []
    for seg in segments:
        css_class = _CLASS_BY_KIND[seg.kind]
        escaped = html.escape(seg.text)
        parts.append(f'<span class="{css_class}">{escaped}</span>')
    return " ".join(parts)
