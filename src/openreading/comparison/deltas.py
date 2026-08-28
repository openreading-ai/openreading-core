"""Content deltas — the packaging-immune "what did each subject capture that the others missed"
view over the guaranteed text channel (DESIGN §9: compare content, not structure). Powers the
content section of `compare --format diffs`.

Matching is TOKEN-COVERAGE, not line-substring: a line is a real delta only when most of its
content tokens appear NOWHERE in the other subject. This is immune to packaging (one backend puts a
whole table on one line, another splits it into rows) and to word reordering — both of which a
substring test misreports as "unique"/"missed". Content equivalence lives here; how the same
content is *structured* (table grids, types, granularity) is structure.py.
"""

from __future__ import annotations

from typing import Any

from openreading.comparison.align import normalize_block_text
from openreading.comparison.ingest import Subject

_MIN_CHARS = 4  # ignore trivial fragments (punctuation, single glyphs) that carry no content
_COVER = 0.8  # a line is "present" in a subject when ≥ this fraction of its tokens appear there


def _content_lines(subject: Subject) -> list[str]:
    """The subject's plain text (C1/C2), one entry per non-blank line; falls back to the shared
    canonical text projection when the flat text channel is absent."""
    text = (subject.response.get("document") or {}).get("text")
    if not text:
        from openreading.evals.scorers import canonical_text

        text = canonical_text(subject.response)
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _tokens(text: str) -> set[str]:
    """Content tokens of a string: normalized (casefold, punctuation→space) then split."""
    return set(normalize_block_text(text).split())


def _covered(line_tokens: set[str], subject_tokens: set[str]) -> bool:
    """Is this line's content present in a subject? True when ≥ _COVER of the line's tokens appear
    in the subject's token set — order- and packaging-independent. Empty lines count as present."""
    if not line_tokens:
        return True
    return len(line_tokens & subject_tokens) / len(line_tokens) >= _COVER


def _is_content(line: str) -> bool:
    return len(normalize_block_text(line)) >= _MIN_CHARS


def content_deltas(subjects: list[Subject]) -> dict[str, dict[str, Any]]:
    """Per subject, two content deltas over the guaranteed text channel (token-coverage matching, so
    packaging/spacing/case/order differences never create false deltas):

    * ``unique`` — lines only this subject captured (no other subject covers their tokens).
    * ``missed`` — substantive lines EVERY other subject has but this one lacks. Requiring
      consensus among the others keeps one backend's OCR noise from looking like a real miss.
    """
    lines = {s.label: _content_lines(s) for s in subjects}
    full = {label: _tokens(" ".join(ls)) for label, ls in lines.items()}
    out: dict[str, dict[str, Any]] = {}
    for s in subjects:
        others = [o.label for o in subjects if o.label != s.label]
        unique = [
            ln
            for ln in lines[s.label]
            if _is_content(ln) and not any(_covered(_tokens(ln), full[o]) for o in others)
        ]
        missed: list[str] = []
        seen: set[str] = set()
        for o in others:
            for ln in lines[o]:
                lt = _tokens(ln)
                key = " ".join(sorted(lt))
                if not _is_content(ln) or key in seen or _covered(lt, full[s.label]):
                    continue
                if all(_covered(lt, full[oo]) for oo in others):  # consensus among the others
                    missed.append(ln)
                    seen.add(key)
        out[s.label] = {"unique": unique, "missed": missed, "total": len(lines[s.label])}
    return out


def content_overlap(subjects: list[Subject]) -> float:
    """Vocabulary shared by ALL subjects over their union — the headline "how much of the content do
    they agree on" number (1.0 = identical vocabulary, 0.0 = disjoint). Token-set based, so it is
    packaging- and order-immune; for two subjects this is their token Jaccard.

    Every subject's token set participates, empty or not — a subject that genuinely extracted zero
    content must pull the score toward 0.0 (its intersection with anyone else is empty), never be
    dropped from the comparison. Dropping it would silently turn "one side captured nothing" into
    the trivially-true single-subject case below, reporting "identical vocabulary" for a pair where
    one side has real content and the other has none — self-contradictory alongside `content_deltas`
    (which does not filter empty subjects) and the DIVERGENT verdict it drives. Only an all-empty
    group (union itself empty) falls back to 1.0, matching this codebase's "two empties = identical"
    convention elsewhere (`strategies/engine.py`'s `_token_jaccard`)."""
    toks = [_tokens(" ".join(_content_lines(s))) for s in subjects]
    if len(toks) < 2:
        return 1.0
    union = set().union(*toks)
    return len(set.intersection(*toks)) / len(union) if union else 1.0
