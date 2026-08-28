"""Line-kind characterization — the deterministic judgment behind `compare --format diffs`'
"the story:" block and the per-side header annotations.

`content_deltas` says WHICH lines each subject uniquely captured; this module says WHAT KIND of
lines they are, in a four-word closed vocabulary: **readable text · numbers & codes · filler ·
garbled fragments**. That turns "only A captured (412)" from a mute count into a judgment ("mostly
readable text" vs "mostly filler") — the difference between a diff and a story.

No LLM anywhere: character-class ratios plus the same vowel-plausibility idea the engine's
`garbled` signal uses (real latin-script words carry vowels). Deterministic, pure, and honest —
phrases state observed proportions ("mostly X", "a mix of …"), never an editorial verdict, and the
corpus story is two factual sentences ordered by how much readable text each side uniquely caught.
"""

from __future__ import annotations

import re
from collections import Counter

KINDS = ("readable text", "numbers & codes", "filler", "garbled fragments")

_VOWELS = set("aeiouAEIOU")
_FILLER_CHARS = set("_-.=~*|•· \t")


def classify_line(line: str) -> str:
    """One of KINDS for a content line. Order of tests matters: filler (no real payload) first,
    then digit-dominated codes, then word plausibility (garbled vs readable)."""
    s = line.strip()
    alnum = sum(1 for c in s if c.isalnum())
    if alnum < 2 or sum(1 for c in s if c in _FILLER_CHARS) >= 0.6 * len(s):
        return "filler"
    tokens = re.findall(r"\S+", s)
    word_toks = [t for t in tokens if any(c.isalpha() for c in t)]
    digit_heavy = sum(1 for t in tokens if sum(c.isdigit() for c in t) > 0.5 * len(t))
    if not word_toks or digit_heavy >= 0.6 * len(tokens):
        return "numbers & codes"
    plausible = sum(
        1
        for t in word_toks
        if any(c in _VOWELS for c in t) and sum(c.isalpha() for c in t) >= 0.6 * len(t)
    )
    if plausible < 0.5 * len(word_toks):
        return "garbled fragments"
    return "readable text"


def characterize(lines: list[str]) -> Counter:
    """Kind counts for a batch of lines."""
    return Counter(classify_line(ln) for ln in lines)


def phrase(counts: Counter) -> str:
    """The counts as a short honest phrase: 'mostly X' (≥70%), 'mostly X and Y' (top two ≥70%),
    else 'a mix of …'; 'nothing' for empty."""
    total = sum(counts.values())
    if not total:
        return "nothing"
    top = counts.most_common(2)
    if top[0][1] >= 0.7 * total:
        return f"mostly {top[0][0]}"
    if len(top) > 1 and top[0][1] + top[1][1] >= 0.7 * total:
        return f"mostly {top[0][0]} and {top[1][0]}"
    return "a mix of " + ", ".join(k for k, _ in counts.most_common(3))


def corpus_story(per_side: dict[str, Counter]) -> str:
    """Two factual sentences summarizing a corpus compare: each side's unique-line count and
    character, ordered by readable-text count (then total) so the side that caught more real
    content leads. Empty when neither side captured anything unique. Never editorializes beyond
    the counts — the ordering and the numbers carry the judgment."""
    sides = [(lbl, c) for lbl, c in per_side.items() if sum(c.values())]
    if not sides:
        return ""
    sides.sort(key=lambda kv: (kv[1].get("readable text", 0), sum(kv[1].values())), reverse=True)
    zero = [lbl for lbl, c in per_side.items() if not sum(c.values())]

    sentences: list[str] = []
    first_label, first_counts = sides[0]
    others = [lbl for lbl in per_side if lbl != first_label]
    other = others[0] if len(others) == 1 else "the others"
    n = sum(first_counts.values())
    sentences.append(f"{first_label} captured {n} lines {other} missed — {phrase(first_counts)}.")
    for lbl, c in sides[1:]:
        sentences.append(f"{lbl}'s {sum(c.values())} extras are {phrase(c)}.")
    for lbl in zero:
        vs = [o for o in per_side if o != lbl]
        sentences.append(
            f"{lbl} captured nothing {vs[0] if len(vs) == 1 else 'the others'} missed."
        )
    return " ".join(sentences)
