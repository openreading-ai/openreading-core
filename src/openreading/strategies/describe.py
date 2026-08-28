"""Plain-English description of a strategy — one sentence saying what it does, printed under each
`strategy validate` badge (the openreading.strategies docstring).

Pure and deterministic: it reads only a NORMALIZED strategy tree (the five-node longhand the loader
produces), so it works for Plain and advanced strategies alike. It describes MECHANICS — the order
backends run, when the strategy moves on, the escalation target, the time/attempt limit — never an
inferred *intent* it cannot read from the tree (no "cheap-first" guessing).

Gate phrasing collapses predicates to their Plain criterion word via `ADVANCED_TO_PLAIN` (the same
mapping the desugar provenance uses), so a `looks_bad`-family bundle reads as "the result looks
bad" rather than a list of four predicates. Gates are OR by nature (signals.evaluate_gate); an
`all_of` that mixes criterion families is summarized as OR too — a known simplification, exact only
in `strategy show --longhand`, which never applies to a Plain strategy (Plain's one `all_of` is the
same-family scanned+low-text pair).
"""

from __future__ import annotations

from typing import Any

from openreading.strategies.plain import ADVANCED_TO_PLAIN

# A criterion family (the Plain word) -> its clause. `subject` is "the result" normally, "the
# winner" inside a compare duel. `disagree` is compare-only, so it names the two branches directly.
_CRITERION_CLAUSE = {
    "looks_bad": lambda subject: f"{subject} looks bad",
    "low_confidence": lambda subject: "confidence is low",
    "missing": lambda subject: "a requested field is missing",
    "disagree": lambda subject: "they disagree",
}
# One-line gloss per Plain criterion word, for the `validate` glossary (only the words that appear
# in a file are printed). Kept short on purpose — an explanation, not a novel.
CRITERION_GLOSS: dict[str, tuple[str, str]] = {
    "looks_bad": (
        "looks bad",
        "openreading's quality probe flags the result: garbled text, over 20% near-empty "
        "pages, or an image-only page it got almost no text from",
    ),
    "disagree": (
        "disagree",
        "how far the two results' text differs — 1 minus their shared-word overlap (Jaccard). "
        "Trips past 0.3 by default: they share under 70% of their combined words (tunable).",
    ),
    "low_confidence": (
        "low confidence",
        "the backend's own confidence score is below your threshold (e.g. 0.8; the default "
        "quality bundle uses 0.6)",
    ),
    "missing": ("missing", "a field you named didn't come back in the result"),
}

# Advanced predicates with no Plain word get a literal readable phrase; anything unlisted falls back
# to a generic clause so the describer never fabricates specificity it can't justify.
_LITERAL_CLAUSE = {
    "garble_score_over": "the text looks garbled",
    "table_sanity_below": "table quality is low",
    "zero_blocks": "nothing was extracted",
    "text_source": "the text came from an unexpected source",
    "doc_type_confidence_below": "the document type is uncertain",
    "field_confidence_below": "a field's confidence is low",
    "warning_code": "a specific warning fired",
    "matches_regex": "the text matches a pattern",
    "sample_percent": "the audit sample selects it",
}


def describe_strategy(tree: dict[str, Any]) -> str:
    """One plain-English sentence describing what the normalized strategy `tree` does."""
    return _describe_node(tree) + _budget_sentence(tree)


# --------------------------------------------------------------------------- node dispatch


def _describe_node(node: dict[str, Any]) -> str:
    if "steps" in node:
        return _describe_cascade(node)
    if "parallel" in node:
        return _describe_parallel(node)
    if "route" in node:
        return _describe_route(node)
    if "decide" in node:
        return _describe_decide(node)
    if "use" in node:
        return f"Runs {_name(node)}."
    if "backend" in node:
        return f"Runs {_name(node)}."
    return "Does something this version cannot describe."


def _describe_cascade(node: dict[str, Any]) -> str:
    steps = node["steps"]
    # compare + then: a 2+-step cascade whose first step is a gated `pick: best` duel (the Plain
    # `compare:`/`then:` idiom, spec §6.3).
    if (
        len(steps) >= 2
        and _is_pick_best(steps[0])
        and isinstance(steps[0].get("escalate_if"), dict)
    ):
        duel = _describe_parallel(steps[0]).rstrip(".")
        gate = _describe_gate(steps[0]["escalate_if"], subject="the winner")
        targets = _join([_name(s) for s in steps[1:]])
        tail = f"; if {gate}, sends the document to {targets}." if gate else "."
        return duel + tail

    names = _rung_names(steps)
    gate = _first_gate(steps)
    if gate:
        return f"Tries {_ordered(names)}, moving on when a step fails, or {gate}."
    return f"Tries {_ordered(names)}, moving to the next when one fails."


def _describe_parallel(node: dict[str, Any]) -> str:
    names = _join([_name(b) for b in node["parallel"]])
    pick = node.get("pick", "best")
    if pick == "fastest":
        return f"Runs {names} at once and keeps the first to finish, cancelling the rest."
    if pick == "merge":
        return f"Runs {names} at once and merges their fields into one result."
    base = f"Runs {names} at once and keeps the better result"
    return base + (" (an LLM picks the winner)." if node.get("judge") else ".")


def _describe_route(node: dict[str, Any]) -> str:
    r = node["route"]
    rules = "; ".join(
        f"{_describe_when(rule['when'])} -> {_name(rule['use'])}" for rule in r["rules"]
    )
    return f"Routes by the document: {rules}; otherwise {_name(r['default'])}."


def _describe_decide(node: dict[str, Any]) -> str:
    d = node["decide"]
    names = _join([_name(a) for a in d["among"]])
    return (
        f"Chooses among {names} — an LLM decides when enabled, otherwise {_name(d['otherwise'])}."
    )


# --------------------------------------------------------------------------- gates


def _describe_gate(gate: dict[str, Any], *, subject: str = "the result") -> str:
    """The 'when X or Y' clause for an escalate_if gate, collapsed to criterion words."""
    seen: list[str] = []
    for pred in _predicate_names(gate):
        family = ADVANCED_TO_PLAIN.get(pred)
        clause = (
            _CRITERION_CLAUSE[family](subject)
            if family
            else _LITERAL_CLAUSE.get(pred, "a custom quality check fires")
        )
        if clause not in seen:
            seen.append(clause)
    if not seen:
        return ""
    if len(seen) == 1:
        return seen[0]
    return ", ".join(seen[:-1]) + " or " + seen[-1]


def criteria_used(tree: dict[str, Any]) -> set[str]:
    """The Plain criterion families (`looks_bad`, `disagree`, `low_confidence`, `missing`) whose
    gates appear anywhere in `tree` — drives the `validate` glossary (explain only what showed up)."""
    fams: set[str] = set()
    for gate in _all_gates(tree):
        for pred in _predicate_names(gate):
            fam = ADVANCED_TO_PLAIN.get(pred)
            if fam in CRITERION_GLOSS:
                fams.add(fam)
    return fams


def _all_gates(node: Any):
    if not isinstance(node, dict):
        return
    if isinstance(node.get("escalate_if"), dict):
        yield node["escalate_if"]
    for step in node.get("steps") or []:
        yield from _all_gates(step)
    for branch in node.get("parallel") or []:
        yield from _all_gates(branch)
    if isinstance(node.get("route"), dict):
        for rule in node["route"].get("rules") or []:
            yield from _all_gates(rule.get("use"))
        yield from _all_gates(node["route"].get("default"))
    if isinstance(node.get("decide"), dict):
        for among in node["decide"].get("among") or []:
            yield from _all_gates(among)
        yield from _all_gates(node["decide"].get("otherwise"))


def _is_pick_best(node: Any) -> bool:
    return isinstance(node, dict) and "parallel" in node and node.get("pick") == "best"


def _predicate_names(gate: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for key, value in gate.items():
        if key in ("any_of", "all_of"):
            for sub in value:
                out.extend(_predicate_names(sub))
        else:
            out.append(key)
    return out


def _first_gate(steps: list[dict[str, Any]]) -> str:
    for step in steps:
        g = step.get("escalate_if")
        if isinstance(g, dict):
            return _describe_gate(g)
    return ""


def _describe_when(when: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in when.items():
        if key == "doc_type":
            parts.append(f"{_join(list(value))} documents")
        elif key == "pages_over":
            parts.append(f"documents over {value} pages")
        elif key == "mime":
            parts.append(f"{value} files")
        elif key == "compliance":
            parts.append("compliance-constrained documents")
        else:
            parts.append(f"documents matching {key}")
    return " and ".join(parts) if parts else "any document"


# --------------------------------------------------------------------------- names, joins, budget


def _name(node: Any) -> str:
    if isinstance(node, str):
        return "the best available backend" if node == "auto" else node
    if not isinstance(node, dict):
        return "a sub-strategy"
    if "backend" in node:
        b = node["backend"]
        return "the best available backend" if b == "auto" else str(b)
    if "use" in node:
        return f"the {node['use']} strategy"
    if "parallel" in node:
        return "a race" if node.get("pick") == "fastest" else "a comparison"
    if "steps" in node:
        return "a sub-cascade"
    return "a sub-strategy"


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return names[0] if names else ""
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def _ordered(names: list[str]) -> str:
    return ", then ".join(names)


def _rung_names(steps: list[Any]) -> list[str]:
    """Name each cascade rung; a second/later `auto` rung reads as "the next best available
    backend" (it picks among what the earlier autos didn't try) rather than repeating."""
    names: list[str] = []
    autos = 0
    for step in steps:
        if isinstance(step, dict) and step.get("backend") == "auto":
            autos += 1
            names.append(
                "the best available backend" if autos == 1 else "the next best available backend"
            )
        else:
            names.append(_name(step))
    return names


def _budget_sentence(tree: dict[str, Any]) -> str:
    budget = tree.get("budget") or {}
    bits: list[str] = []
    if budget.get("max_duration"):
        bits.append(f"stops after {budget['max_duration']}")
    if budget.get("max_attempts"):
        bits.append(f"makes at most {budget['max_attempts']} attempts")
    if not bits:
        return ""
    joined = "; ".join(bits)
    return " " + joined[0].upper() + joined[1:] + "."
