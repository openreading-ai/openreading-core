"""P1 — the Plain dialect desugar (internal/design/simple-strategies.md §2/§5/§6/§10; harness §15).

Layers:
  T1  differential no-regression corpus — desugar is a strict no-op on every advanced/shorthand
      body the repo already has (docs configs, presets, the normalize EXPANSIONS inputs).
  T2  translation goldens — one (plain body -> exact longhand) row per desugar shape; each row
      is schema-valid and (when rewritten) a normalize fixed point.
  T3  invariants over a generated space — schema-valid, fixed point, deterministic, output is
      itself advanced (desugar-of-output is a no-op), provenance paths exist.
  T4  negative space — every desugar-time §8 row: schema-valid input, ConfigError at desugar,
      with a message-content assertion.
"""

from __future__ import annotations

import itertools

import pytest

from openreading import schemas
from openreading.strategies.loader import ConfigError
from openreading.strategies.normalize import normalize_node
from openreading.strategies.plain import PlainInfo, desugar_config, is_plain_dialect
from openreading.strategies.presets import PRESETS


def _wrap(body, extra=None):
    strategies = {"s": body}
    if extra:
        strategies.update(extra)
    return {"version": 1, "strategies": strategies}


def _desugar_body(body, extra=None):
    out, info = desugar_config(_wrap(body, extra))
    return out["strategies"]["s"], info["s"]


# ------------------------------------------------------------------------- T1: no-regression


def _plain_map(body) -> bool:
    # the independent classification predicate (§15 T1) — no desugar internals
    return isinstance(body, dict) and bool(set(body) & {"try", "race", "compare"})


def test_desugar_is_a_strict_noop_on_docs_configs():
    from tests.test_docs_truth import _CONFIG_BLOCKS  # the _wrap-filtered doc config corpus

    checked = 0
    for _name, _i, cfg in _CONFIG_BLOCKS:
        # the Plain-dialect doc blocks (simple.md, cookbook Plain spellings) are rewritten by
        # design — that is T2's job. This layer proves the no-op on every ADVANCED block.
        if any(_plain_map(b) for b in (cfg.get("strategies") or {}).values()):
            continue
        out, info = desugar_config(cfg)
        assert out == cfg, f"advanced config was rewritten: {cfg}"
        assert not any(i.rewritten for i in info.values()), "advanced body marked rewritten"
        checked += 1
    assert checked >= 25  # guard: the advanced corpus is still substantial


def test_desugar_is_a_strict_noop_on_presets():
    cfg = {"version": 1, "strategies": dict(PRESETS)}
    out, info = desugar_config(cfg)
    assert out == cfg  # vendored presets are byte-identical to today (§10 — not re-vendored)
    assert not any(i.rewritten for i in info.values())


def test_desugar_is_a_strict_noop_on_normalize_expansion_inputs():
    from tests.test_strategy_normalize import EXPANSIONS

    for row in EXPANSIONS:
        node = row[0]
        if _plain_map(node):
            continue  # EXPANSIONS is the advanced table; none are Plain, but be defensive
        out, info = desugar_config(_wrap(node))
        assert out["strategies"]["s"] == node
        assert info["s"].rewritten is False


def test_config_without_plain_bodies_roundtrips_unchanged():
    cfg = {
        "version": 1,
        "policy": {"no_train_on_data": True},
        "strategies": {"a": "reducto", "b": ["pymupdf", "reducto"], "c": {"steps": ["pymupdf"]}},
    }
    out, info = desugar_config(cfg)
    assert out == cfg
    assert {n: i.rewritten for n, i in info.items()} == {"a": False, "b": False, "c": False}
    assert {n: i.dialect for n, i in info.items()} == {"a": "plain", "b": "plain", "c": "advanced"}


# ------------------------------------------------------------------------- T2: translation goldens

_SCAN_PAIR = {"all_of": [{"scanned_pages_detected": True}, {"chars_per_page_below": 100}]}
_LOOKS_BAD_ANYOF = {"any_of": [_SCAN_PAIR, {"garbled": True}, {"empty_pages_over": 0.2}]}
# the §6.3 compare default gate: disagree OR looks_bad
_COMPARE_DEFAULT_GATE = {
    "any_of": [
        {"disagreement_over": 0.3},
        _SCAN_PAIR,
        {"garbled": True},
        {"empty_pages_over": 0.2},
    ]
}

PLAIN_GOLDENS = [
    # pass-through (bare shorthand — not rewritten, normalize owns it)
    ("reducto", "reducto"),
    (["pymupdf", "reducto"], ["pymupdf", "reducto"]),
    # try -> cascade
    ({"try": "reducto"}, {"steps": [{"backend": "reducto"}]}),
    ({"try": ["pymupdf", "reducto"]}, {"steps": [{"backend": "pymupdf"}, {"backend": "reducto"}]}),
    ({"try": ["pymupdf", "auto"]}, {"steps": [{"backend": "pymupdf"}, {"backend": "auto"}]}),
    # try + escalate_when (scalar looks_bad -> any_of; gate on non-final only)
    (
        {"try": ["pymupdf", "reducto"], "escalate_when": "looks_bad"},
        {
            "steps": [
                {"backend": "pymupdf", "escalate_if": _LOOKS_BAD_ANYOF},
                {"backend": "reducto"},
            ]
        },
    ),
    # single-criterion flat gates (no scan pair -> flat OR map)
    (
        {"try": ["pymupdf", "reducto"], "escalate_when": {"low_confidence": 0.8}},
        {
            "steps": [
                {"backend": "pymupdf", "escalate_if": {"confidence_below": 0.8}},
                {"backend": "reducto"},
            ]
        },
    ),
    (
        {"try": ["pymupdf", "reducto"], "escalate_when": {"low_confidence": True}},
        {
            "steps": [
                {"backend": "pymupdf", "escalate_if": {"confidence_below": 0.6}},
                {"backend": "reducto"},
            ]
        },
    ),
    (
        {
            "try": ["reducto", "anthropic-claude"],
            "escalate_when": {"missing": ["invoice_number", "total"]},
        },
        {
            "steps": [
                {
                    "backend": "reducto",
                    "escalate_if": {"fields_required": ["invoice_number", "total"]},
                },
                {"backend": "anthropic-claude"},
            ]
        },
    ),
    # looks_bad member overlays
    (
        {
            "try": ["pymupdf", "reducto"],
            "escalate_when": {"looks_bad": {"no_text_from_images": False}},
        },
        {
            "steps": [
                {"backend": "pymupdf", "escalate_if": {"garbled": True, "empty_pages_over": 0.2}},
                {"backend": "reducto"},
            ]
        },
    ),
    (
        {"try": ["pymupdf", "reducto"], "escalate_when": {"looks_bad": {"empty_pages": 0.3}}},
        {
            "steps": [
                {
                    "backend": "pymupdf",
                    "escalate_if": {
                        "any_of": [_SCAN_PAIR, {"garbled": True}, {"empty_pages_over": 0.3}]
                    },
                },
                {"backend": "reducto"},
            ]
        },
    ),
    (
        {"try": ["pymupdf", "reducto"], "escalate_when": {"looks_bad": {"min_text_per_page": 80}}},
        {
            "steps": [
                {
                    "backend": "pymupdf",
                    "escalate_if": {
                        "any_of": [
                            {
                                "all_of": [
                                    {"scanned_pages_detected": True},
                                    {"chars_per_page_below": 80},
                                ]
                            },
                            {"garbled": True},
                            {"empty_pages_over": 0.2},
                            {"chars_per_page_below": 80},
                        ]
                    },
                },
                {"backend": "reducto"},
            ]
        },
    ),
    # walls (time only)
    (
        {"try": ["pymupdf", "reducto"], "max_time": "2m"},
        {
            "steps": [{"backend": "pymupdf"}, {"backend": "reducto"}],
            "budget": {"max_duration": "2m"},
        },
    ),
    # use-ref rung (preset name -> use:); gate never attaches to a use-ref (§6.1)
    (
        {"try": ["cost_saver", "reducto"], "escalate_when": "looks_bad"},
        {"steps": [{"use": "cost_saver"}, {"backend": "reducto"}]},
    ),
    # race
    (
        {"race": ["pymupdf", "docling"]},
        {
            "parallel": [{"backend": "pymupdf"}, {"backend": "docling"}],
            "pick": "fastest",
            "on_win": "cancel",
        },
    ),
    (
        {"race": ["pymupdf", "docling"], "max_time": "30s"},
        {
            "parallel": [{"backend": "pymupdf"}, {"backend": "docling"}],
            "pick": "fastest",
            "on_win": "cancel",
            "budget": {"max_duration": "30s"},
        },
    ),
    # compare alone
    (
        {"compare": ["docling", "aws-textract"]},
        {
            "parallel": [{"backend": "docling"}, {"backend": "aws-textract"}],
            "pick": "best",
            "require": "all",
        },
    ),
    # compare + then, default gate — the §6.3 reference tree
    (
        {"compare": ["docling", "aws-textract"], "then": "reducto"},
        {
            "steps": [
                {
                    "parallel": [{"backend": "docling"}, {"backend": "aws-textract"}],
                    "pick": "best",
                    "require": "all",
                    "escalate_if": _COMPARE_DEFAULT_GATE,
                },
                {"backend": "reducto"},
            ],
        },
    ),
    # compare + then, explicit gate replaces the default
    (
        {
            "compare": ["docling", "aws-textract"],
            "then": "reducto",
            "escalate_when": {"missing": ["total"]},
        },
        {
            "steps": [
                {
                    "parallel": [{"backend": "docling"}, {"backend": "aws-textract"}],
                    "pick": "best",
                    "require": "all",
                    "escalate_if": {"fields_required": ["total"]},
                },
                {"backend": "reducto"},
            ]
        },
    ),
    # 3-way compare
    (
        {"compare": ["docling", "aws-textract", "reducto"], "then": "auto"},
        {
            "steps": [
                {
                    "parallel": [
                        {"backend": "docling"},
                        {"backend": "aws-textract"},
                        {"backend": "reducto"},
                    ],
                    "pick": "best",
                    "require": "all",
                    "escalate_if": _COMPARE_DEFAULT_GATE,
                },
                {"backend": "auto"},
            ]
        },
    ),
]


@pytest.mark.parametrize("body,expected", PLAIN_GOLDENS)
def test_plain_desugar_golden(body, expected):
    got, info = _desugar_body(body)
    assert got == expected
    schemas.validate_strategy_config(_wrap(got))  # output is schema-valid
    if _plain_map(body):
        assert info.rewritten is True
        assert normalize_node(got) == got, "desugar output is not a normalize fixed point"
    else:
        assert info.rewritten is False


def test_section_4_file_desugars_and_validates():
    # the milestone acceptance file (§4): all five bodies, badged plain, desugar to valid longhand
    cfg = {
        "version": 1,
        "strategies": {
            "quick": {"race": ["pymupdf", "docling"]},
            "fallback": ["reducto", "azure-document-intelligence", "docling"],
            "main": {
                "try": ["pymupdf", "docling", "reducto"],
                "escalate_when": "looks_bad",
                "max_time": "2m",
            },
            "invoices": {
                "try": ["reducto", "anthropic-claude"],
                "escalate_when": {
                    "missing": ["invoice_number", "total_amount"],
                    "low_confidence": 0.8,
                },
            },
            "contracts": {
                "compare": ["docling", "aws-textract"],
                "then": "reducto",
            },
        },
    }
    out, info = desugar_config(cfg)
    assert {n: i.dialect for n, i in info.items()} == {k: "plain" for k in cfg["strategies"]}
    schemas.validate_strategy_config(out)
    for name, body in out["strategies"].items():
        if info[name].rewritten:  # bare-list `fallback` is pass-through shorthand, not longhand
            assert normalize_node(body) == body, f"{name} is not a normalize fixed point"


# ------------------------------------------------------------------------- T3: generated invariants


def _generated_plain_bodies():
    backends = ["pymupdf", "docling", "reducto", "aws-textract"]
    criteria = [
        "looks_bad",
        {"looks_bad": True},
        {"looks_bad": {"no_text_from_images": False}},
        {"looks_bad": {"empty_pages": 0.3, "min_text_per_page": 120}},
        {"low_confidence": 0.7},
        {"missing": ["total"]},
        {"missing": ["total"], "looks_bad": True},
    ]
    walls = [{}, {"max_time": "90s"}]
    for n, wall in itertools.product((2, 3), walls):
        items = backends[:n]
        yield {"try": items, **wall}
        for crit in criteria:
            yield {"try": items, "escalate_when": crit, **wall}
        yield {"race": items, **wall}
        yield {"compare": items, "then": "auto", **wall}
        for crit in criteria + [{"disagree": True}, {"disagree": 0.5, "looks_bad": True}]:
            yield {"compare": items, "then": "auto", "escalate_when": crit, **wall}


def _path_exists(tree, path):
    node = tree
    for part in path.split("."):
        key = part[: part.index("[")] if "[" in part else part
        node = node[key]
        while "[" in part:
            i = int(part[part.index("[") + 1 : part.index("]")])
            node = node[i]
            part = part[part.index("]") + 1 :]
    return node is not None


@pytest.mark.parametrize("body", list(_generated_plain_bodies()))
def test_generated_plain_invariants(body):
    cfg = _wrap(body)
    schemas.validate_strategy_config(cfg)  # input is schema-valid
    out1, info1 = desugar_config(cfg)
    out2, _ = desugar_config(cfg)
    node = out1["strategies"]["s"]

    schemas.validate_strategy_config(out1)  # (a) output schema-valid
    assert normalize_node(node) == node  # (b) normalize fixed point
    assert out1 == out2  # (c) deterministic
    reout, reinfo = desugar_config(out1)  # (d) desugar-of-output is a strict no-op (it is advanced)
    assert reout == out1
    assert not any(i.rewritten for i in reinfo.values())
    for path in info1["s"].provenance or {}:  # (e) every provenance path locates a node
        assert _path_exists(node, path)


# ------------------------------------------------------------------------- T4: desugar-time §8 rows

PLAIN_DESUGAR_ERRORS = [
    ({"try": ["pymupdf", "nonesuch-backend"]}, None, "not a known backend or strategy"),
    ({"try": ["pymupdf", "reducto", "reducto"]}, None, "appears twice"),
    ({"race": ["pymupdf", "auto"]}, None, "cannot race"),
    ({"compare": ["docling", "auto"], "then": "reducto"}, None, "cannot race or be compared"),
    (
        {"try": ["pymupdf", "reducto"], "escalate_when": {"disagree": True}},
        None,
        "only works inside compare",
    ),
    ({"try": "reducto", "escalate_when": "looks_bad"}, None, "single rung"),
    ({"compare": ["docling", "reducto"], "then": "reducto"}, None, "different backend"),
    # backend/strategy name collision at a reference site
    ({"try": ["pymupdf"]}, {"pymupdf": "reducto"}, "both a backend id and a strategy name"),
]


@pytest.mark.parametrize("body,extra,message", PLAIN_DESUGAR_ERRORS)
def test_plain_desugar_time_errors(body, extra, message):
    cfg = _wrap(body, extra)
    schemas.validate_strategy_config(cfg)  # the input is schema-valid: this is a DESUGAR error
    with pytest.raises(ConfigError, match=message):
        desugar_config(cfg)


def test_close_name_suggestion():
    with pytest.raises(ConfigError, match="did you mean 'pymupdf'"):
        desugar_config(_wrap({"try": ["pymupdff", "reducto"]}))


# ------------------------------------------------------------------------- classification + provenance


def test_is_plain_dialect_predicate():
    assert is_plain_dialect("reducto")
    assert is_plain_dialect(["a", "b"])
    assert is_plain_dialect({"try": ["a", "b"]})
    assert is_plain_dialect({"compare": ["a", "b"], "then": "c"})
    assert not is_plain_dialect({"steps": ["a"]})
    assert not is_plain_dialect({"parallel": ["a", "b"], "pick": "best"})
    assert not is_plain_dialect({})
    assert not is_plain_dialect(None)  # non-body value
    assert not is_plain_dialect(5)


def test_desugar_config_without_strategies_is_a_noop():
    cfg = {"version": 1, "policy": {"no_train_on_data": True}}
    out, info = desugar_config(cfg)
    assert out is cfg and info == {}


def test_provenance_records_plain_word_per_predicate():
    _, info = _desugar_body(
        {
            "compare": ["docling", "aws-textract"],
            "then": "reducto",
            "escalate_when": {"disagree": True, "looks_bad": True},
        }
    )
    prov = info.provenance["steps[0]"]
    assert prov["disagreement_over"] == "disagree"
    assert prov["garbled"] == "looks_bad"
    assert prov["scanned_pages_detected"] == "looks_bad"
    assert prov["chars_per_page_below"] == "looks_bad"


def test_plain_info_shape():
    _, info = _desugar_body({"try": ["pymupdf", "reducto"], "escalate_when": "looks_bad"})
    assert isinstance(info, PlainInfo)
    assert info.dialect == "plain" and info.rewritten is True
    assert info.provenance["steps[0]"]["garbled"] == "looks_bad"


def test_first_advanced_key():
    from openreading.strategies.plain import first_advanced_key

    assert first_advanced_key({"parallel": [], "pick": "best"}) == "parallel"
    assert first_advanced_key({"try": ["a"], "escalate_when": "looks_bad"}) is None  # all Plain
    assert first_advanced_key("reducto") is None  # bare string
    assert first_advanced_key(["a", "b"]) is None  # bare list


# ------------------------------------------------------------------------- P5: preset pairings (§10)
#
# Presets stay vendored as-is (§10 — not re-vendored); the docs pair each with a Plain
# near-equivalent. Each pairing desugars to the preset's longhand structure (same backends,
# order) — the gates differ only by intent: (dropped) and the §5 scan-member upgrade.

from openreading.strategies import normalize_strategy  # noqa: E402

PRESET_PAIRINGS = {
    "cost_saver": {
        "try": ["pymupdf", "docling", "auto"],
        "escalate_when": {"looks_bad": True, "low_confidence": True},
    },
    "max_accuracy": {
        "try": ["auto", "auto"],
        "escalate_when": {"looks_bad": True, "low_confidence": True},
    },
    "fast": {"race": ["pymupdf", "tesseract"]},
    "offline_first": {
        "try": ["pymupdf", "tesseract", "docling"],
        "escalate_when": {"looks_bad": True, "low_confidence": True},
    },
}


def _profile(tree):
    """The structure a Plain pairing shares with its preset: node kind, backend sequence (and pick
    for a race). Gates + intent are deliberately excluded (they differ per §5/§10)."""
    if "steps" in tree:
        backs = tuple(s.get("backend", "use:" + s.get("use", "?")) for s in tree["steps"])
        return ("cascade", backs)
    if "parallel" in tree:
        return ("parallel", tuple(b.get("backend") for b in tree["parallel"]), tree.get("pick"))
    return ("?", tree)


@pytest.mark.parametrize("preset", PRESET_PAIRINGS)
def test_preset_plain_pairing_matches_structure(preset):
    plain_tree, _ = _desugar_body(PRESET_PAIRINGS[preset])
    preset_tree = normalize_strategy(preset, None)  # the vendored preset longhand
    assert _profile(plain_tree) == _profile(preset_tree)


def test_preset_pairing_gate_is_the_scan_aware_upgrade():
    # cost_saver's Plain pairing gates on looks_bad+low_confidence — the default bundle with the
    # scan member made result-aware (§5), so it is a NEAR-equivalent, not byte-equal to the preset.
    plain_tree, _ = _desugar_body(PRESET_PAIRINGS["cost_saver"])
    assert plain_tree["steps"][0]["escalate_if"] == {
        "any_of": [
            _SCAN_PAIR,
            {"garbled": True},
            {"empty_pages_over": 0.2},
            {"confidence_below": 0.6},
        ]
    }
    # the preset itself keeps the flat default bundle (bare scanned_pages_detected) — proof they
    # differ exactly by the §5 upgrade.
    assert "any_of" not in normalize_strategy("cost_saver", None)["steps"][0]["escalate_if"]
