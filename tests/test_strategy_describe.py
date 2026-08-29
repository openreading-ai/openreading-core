"""`describe_strategy` — the plain-English summary printed under each `strategy validate` badge.

The describer is a pure, deterministic function over a NORMALIZED strategy tree (works for Plain
and advanced alike). These tests pin the phrasing for each node kind and the Plain idioms, and a
coverage test proves it describes every cookbook strategy + preset without crashing.
"""

from __future__ import annotations

import importlib
import re

import pytest
import yaml

from openreading.strategies import StrategyConfig, normalize_strategy
from openreading.strategies.describe import describe_strategy
from openreading.strategies.plain import desugar_config
from openreading.strategies.presets import PRESET_NAMES


def _tree(body, *, name="s", extra=None):
    raw = {"version": 1, "strategies": {name: body, **(extra or {})}}
    desugared, _ = desugar_config(raw)
    cfg = StrategyConfig.model_validate(desugared)
    return normalize_strategy(name, cfg)


def _order(text, *words):
    """Assert the words appear in this order in text."""
    idx = [text.find(w) for w in words]
    assert all(i >= 0 for i in idx), f"missing one of {words} in: {text}"
    assert idx == sorted(idx), f"{words} out of order in: {text}"


# ---- the three headline Plain shapes ----------------------------------------------------------


def test_cascade_try_with_looks_bad_and_time():
    d = describe_strategy(
        _tree(
            {
                "try": ["pymupdf", "docling", "reducto"],
                "escalate_when": "looks_bad",
                "max_time": "2m",
            }
        )
    )
    _order(d, "pymupdf", "docling", "reducto")
    assert "looks bad" in d
    assert "fails" in d  # a try moves on when a step fails OR the result looks bad
    assert "2m" in d
    assert d.endswith(".")


def test_race():
    d = describe_strategy(_tree({"race": ["pymupdf", "docling"]}))
    assert "at once" in d
    assert "first to finish" in d
    _order(d, "pymupdf", "docling")


def test_parallel_merge_advanced():
    # `pick: merge` (advanced-only) composes fields from all branches
    d = describe_strategy(_tree({"parallel": ["docling", "reducto"], "pick": "merge"}))
    assert "merges" in d and "at once" in d
    _order(d, "docling", "reducto")


def test_compare_then_default_gate():
    d = describe_strategy(_tree({"compare": ["docling", "aws-textract"], "then": "reducto"}))
    assert "keeps the better" in d
    _order(d, "docling", "aws-textract")
    assert "disagree" in d  # the default compare gate
    assert "reducto" in d and "sends the document to reducto" in d


# ---- other node kinds -------------------------------------------------------------------------


def test_bare_list_is_failure_fallback_no_quality_gate():
    d = describe_strategy(_tree(["reducto", "azure-document-intelligence", "docling"]))
    _order(d, "reducto", "azure-document-intelligence", "docling")
    assert "fail" in d
    assert "looks bad" not in d  # a bare list has no quality gate


def test_missing_and_low_confidence_criteria():
    d = describe_strategy(
        _tree(
            {
                "try": ["reducto", "anthropic-claude"],
                "escalate_when": {"missing": ["invoice_number", "total"], "low_confidence": 0.8},
            }
        )
    )
    assert "field" in d and "missing" in d
    assert "confidence" in d


def test_bare_compare_has_no_then_clause():
    d = describe_strategy(_tree({"compare": ["docling", "aws-textract"]}))
    assert "keeps the better" in d
    assert "sends the document" not in d  # no then: -> nowhere to escalate


def test_route():
    d = describe_strategy(
        _tree(
            {
                "route": {
                    "rules": [{"when": {"doc_type": ["invoice"]}, "use": "strategy:helper"}],
                    "default": "pymupdf",
                }
            },
            extra={"helper": ["pymupdf", "reducto"]},
        )
    )
    assert d.lower().startswith("routes")
    assert "invoice" in d
    assert "helper" in d
    assert "otherwise" in d and "pymupdf" in d


def test_decide():
    d = describe_strategy(
        _tree(
            {"decide": {"among": ["strategy:helper", "reducto"], "otherwise": "reducto"}},
            extra={"helper": ["pymupdf", "reducto"]},
        )
    )
    assert "hooses among" in d  # "Chooses among"
    assert "helper" in d and "reducto" in d


def test_leaf_and_auto():
    assert describe_strategy(_tree("pymupdf")) == "Runs pymupdf."
    assert "best available" in describe_strategy(_tree("auto"))


def test_max_attempts_is_never_described():
    # `max_attempts` is read by no engine code, so describing it states a ceiling the run does not
    # hold to. `strategy validate` refuses the key; this summary must not affirm it either.
    d = describe_strategy(_tree({"steps": ["pymupdf", "reducto"], "budget": {"max_attempts": 3}}))
    assert "attempt" not in d


def test_max_duration_is_still_described():
    d = describe_strategy(
        _tree({"steps": ["pymupdf", "reducto"], "budget": {"max_duration": "30s"}})
    )
    assert "stops after 30s" in d.lower()


def test_deterministic():
    body = {"compare": ["docling", "aws-textract"], "then": "reducto"}
    assert describe_strategy(_tree(body)) == describe_strategy(_tree(body))


# ---- criteria_used (drives the validate glossary) ---------------------------------------------


def test_criteria_used_compare_then_default():
    from openreading.strategies.describe import criteria_used

    assert criteria_used(_tree({"compare": ["docling", "aws-textract"], "then": "reducto"})) == {
        "looks_bad",
        "disagree",
    }


def test_criteria_used_missing_and_low_confidence():
    from openreading.strategies.describe import criteria_used

    fams = criteria_used(
        _tree(
            {
                "try": ["reducto", "anthropic-claude"],
                "escalate_when": {"missing": ["total"], "low_confidence": 0.8},
            }
        )
    )
    assert fams == {"missing", "low_confidence"}


def test_criteria_used_empty_for_gateless():
    from openreading.strategies.describe import criteria_used

    assert criteria_used(_tree({"race": ["pymupdf", "docling"]})) == set()


def test_every_used_criterion_has_a_gloss():
    from openreading.strategies.describe import CRITERION_GLOSS, criteria_used

    # every family criteria_used can return is glossed (the validate glossary can never miss a word)
    for name in PRESET_NAMES:
        for fam in criteria_used(normalize_strategy(name, None)):
            assert fam in CRITERION_GLOSS


# ---- coverage: describe every cookbook strategy + preset without crashing ----------------------


# The strategy docs live in these module docstrings (AGENTS.md: documentation lives in code);
# the cookbook in `presets.py` above all. Same list as tests/test_docs_truth.py.
_DOC_MODULES = (
    "openreading.strategies",
    "openreading.strategies.model",
    "openreading.strategies.engine",
    "openreading.strategies.signals",
    "openreading.strategies.decider",
    "openreading.strategies.presets",
    "openreading.strategies.plain",
)


def _cookbook_strategies():
    """Every named strategy from a full-config fenced YAML block in the strategy docstrings."""
    out = []
    for mod in _DOC_MODULES:
        doc = importlib.import_module(mod).__doc__ or ""
        for block in re.findall(r"```yaml\n(.*?)```", doc, re.DOTALL):
            try:
                doc = yaml.safe_load(block)
            except yaml.YAMLError:
                continue
            if not isinstance(doc, dict) or "strategies" not in doc:
                continue
            raw = {"version": 1, "strategies": doc["strategies"]}
            try:
                desugared, _ = desugar_config(raw)
                cfg = StrategyConfig.model_validate(desugared)
            except Exception:
                continue  # fragments that reference undefined strategies etc. — not our concern here
            for name in cfg.strategies:
                out.append((mod.rsplit(".", 1)[-1], name, cfg))
    return out


_COOKBOOK = _cookbook_strategies()


def test_enough_cookbook_strategies_collected():
    assert len(_COOKBOOK) >= 10, (
        f"only {len(_COOKBOOK)} strategies collected — classifier regressed"
    )


@pytest.mark.parametrize("preset", sorted(PRESET_NAMES))
def test_every_preset_describes(preset):
    d = describe_strategy(normalize_strategy(preset, None))
    assert isinstance(d, str) and d.strip() and d.endswith(".")


@pytest.mark.parametrize("fname,name,cfg", _COOKBOOK, ids=[f"{f}:{n}" for (f, n, _) in _COOKBOOK])
def test_every_cookbook_strategy_describes(fname, name, cfg):
    try:
        tree = normalize_strategy(name, cfg)
    except Exception:
        pytest.skip("references an out-of-block strategy")
    d = describe_strategy(tree)
    assert isinstance(d, str) and d.strip(), f"{fname}:{name} produced no description"
    assert d.endswith("."), f"{fname}:{name} description not a sentence: {d!r}"
