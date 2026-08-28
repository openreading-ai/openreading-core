"""Docs-truth test. Every fenced ```yaml strategy block in the `openreading.strategies` module
docstrings (the cookbook in `presets.py` above all) must parse and pass `strategy validate` with
NO errors — the cookbook is executable truth, not prose, so a docstring edit that breaks the
grammar fails `make verify`. Documentation lives in code (AGENTS.md); this is what keeps the
strategy documentation honest now that it lives next to the engine.

Blocks come in several shapes: full configs (`version` + `strategies`), a bare `strategies:` map,
a single node body, a deployment block, or a preset showcase. Each config-like block is wrapped
into a full config and validated against the real builtin registry; referenced-but-undefined
strategy names are stubbed (the analog of GOAL3's "registry stub containing the referenced backend
ids"). Genuinely partial fragments — a bare gate/error map, a `{pick, judge}` options snippet, the
preset-definition showcase — are not standalone configs and are skipped (a floor assertion guards
against a classifier bug that would silently skip everything).
"""

from __future__ import annotations

import importlib
import re

import pytest
import yaml

from openreading.strategies import StrategyConfig, validate_config
from openreading.strategies.plain import desugar_config
from openreading.strategies.presets import PRESET_NAMES

# Every module whose docstring carries strategy YAML. A module missing here silently escapes the
# gate, so the floor assertion below (>= _MIN_BLOCKS) also guards against an emptied docstring.
_MODULES = (
    "openreading.strategies",
    "openreading.strategies.model",
    "openreading.strategies.engine",
    "openreading.strategies.signals",
    "openreading.strategies.decider",
    "openreading.strategies.presets",
    "openreading.strategies.plain",
)
_NODE_KEYS = {"steps", "parallel", "route", "decide"}
_PLAIN_KEYS = {"try", "race", "compare"}  # Plain map-body discriminators (v0.7)
_LEAFISH = {"backend", "use", "extends"}
_DEPLOY = {"decider", "policy", "limits", "defaults"}


def _blocks() -> list[tuple[str, int, str]]:
    out = []
    for name in _MODULES:
        doc = importlib.import_module(name).__doc__ or ""
        for i, b in enumerate(re.findall(r"```yaml\n(.*?)```", doc, re.DOTALL)):
            out.append((name, i, b))
    return out


def _wrap(d) -> dict | str | None:
    """Wrap a parsed block into a full config, or return a SKIP sentinel / None (not a config)."""
    if not isinstance(d, dict):
        return None
    if "version" in d and isinstance(
        d["version"], int
    ):  # a config version is an int (leaf pins are strings)
        d = dict(d)
        d.setdefault("strategies", {})
        return d
    if "strategies" in d:
        return {"version": 1, **d}
    if (_NODE_KEYS & set(d)) or (_LEAFISH & set(d)) or (_PLAIN_KEYS & set(d)):  # a single node body
        return {"version": 1, "strategies": {"_doc": d}}
    if _DEPLOY & set(d) and "strategies" not in d:  # a deployment-only block
        return {"version": 1, "strategies": {}, **{k: v for k, v in d.items() if k in _DEPLOY}}
    vals = list(d.values())  # a bare strategies map (names -> node bodies)?
    if (
        d
        and all(isinstance(v, dict) for v in vals)
        and all(
            (_NODE_KEYS & set(v)) or (_LEAFISH & set(v)) or (_PLAIN_KEYS & set(v)) for v in vals
        )
    ):
        if set(d) & PRESET_NAMES:
            return "SKIP"  # a preset-definition showcase (collides with the builtins by design)
        return {"version": 1, "strategies": d}
    return None  # a bare gate/error/options fragment — not a standalone config


def _collect_refs(node, out: set[str]) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "use" and isinstance(v, str):
                out.add(v)
            elif k == "among" and isinstance(v, list):
                out.update(x for x in v if isinstance(x, str))
            elif k == "otherwise" and isinstance(v, str):
                out.add(v)
            _collect_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_refs(v, out)
    elif isinstance(node, str) and node.startswith("strategy:"):
        out.add(node)


def _stub_referenced_strategies(cfg: dict) -> dict:
    """Register a trivial stub for every referenced-but-undefined strategy name, so a fragment that
    references strategies defined in OTHER doc blocks resolves (analogous to the backend-id stub)."""
    resolved: set[str] = set()
    for _ in range(20):
        refs: set[str] = set()
        _collect_refs(cfg, refs)
        names = {r.split("strategy:")[-1] for r in refs}
        undef = {
            n
            for n in names
            if n and n != "none" and n not in cfg["strategies"] and n not in PRESET_NAMES
        }
        if undef <= resolved:
            break
        resolved |= undef
        for n in undef:
            cfg["strategies"][n] = {"steps": ["pymupdf"]}
    return cfg


_CONFIG_BLOCKS = [
    (name, i, cfg)
    for (name, i, body) in _blocks()
    if isinstance((cfg := _wrap(yaml.safe_load(body))), dict)
]


def test_enough_blocks_are_validated():
    # a floor so a classifier regression can't silently skip every block and pass vacuously; raised
    # from 25 with the Plain (v0.7) docs (simple.md + cookbook Plain spellings) — §14 P6.
    assert len(_CONFIG_BLOCKS) >= 40, f"only {len(_CONFIG_BLOCKS)} doc blocks classified as configs"


@pytest.mark.parametrize(
    ("name", "i", "cfg"), _CONFIG_BLOCKS, ids=[f"{n}#{i}" for (n, i, _) in _CONFIG_BLOCKS]
)
def test_doc_yaml_passes_strategy_validate(name, i, cfg):
    cfg = _stub_referenced_strategies(dict(cfg, strategies=dict(cfg["strategies"])))
    # mirror the loader: desugar Plain map bodies to canonical longhand before validating (a
    # strict no-op on advanced blocks, so pre-Plain docs are unaffected — §7).
    desugared, plain_info = desugar_config(cfg)
    issues = validate_config(
        StrategyConfig.model_validate(desugared), raw=desugared, plain_info=plain_info
    )
    errors = [x for x in issues if x.level == "error"]
    assert not errors, f"{name}#{i} has validate errors: " + "; ".join(
        f"{e.path}: {e.message}" for e in errors
    )
