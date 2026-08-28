"""Milestone 11.2 — shorthand->longhand normalization, extends, presets, and the strategy CLI.

The load-bearing property is the fixed point (spec §7): normalize(shorthand) == the documented
longhand, and normalize(longhand) == longhand.
"""

from __future__ import annotations

import pytest

from openreading.cli.app import main
from openreading.strategies import (
    DEFAULT_BUNDLE,
    NormalizeError,
    StrategyConfig,
    build_library,
    normalize_config,
    normalize_node,
    normalize_strategy,
)
from openreading.strategies.normalize import candidate_label

BUNDLE = dict(DEFAULT_BUNDLE)


# ---- shorthand -> longhand expansion (spec §7 rules 1-6) --------------------------------------

EXPANSIONS = [
    ("pymupdf", {"backend": "pymupdf"}),
    ("auto", {"backend": "auto"}),
    ("strategy:invoices", {"use": "invoices"}),
    (["pymupdf", "reducto"], {"steps": [{"backend": "pymupdf"}, {"backend": "reducto"}]}),
    # cascade-level default bundle distributes to every non-final step, not the last
    (
        {"steps": ["pymupdf", "docling", "reducto"], "escalate_if": "default"},
        {
            "steps": [
                {"backend": "pymupdf", "escalate_if": BUNDLE},
                {"backend": "docling", "escalate_if": BUNDLE},
                {"backend": "reducto"},
            ]
        },
    ),
    # a step's own gate is never overwritten by the cascade-level gate
    (
        {
            "steps": [
                {"backend": "pymupdf", "escalate_if": {"garbled": True}},
                "reducto",
            ],
            "escalate_if": "default",
        },
        {
            "steps": [
                {"backend": "pymupdf", "escalate_if": {"garbled": True}},
                {"backend": "reducto"},
            ]
        },
    ),
    # bare list = today's fallback chain: steps, NO quality gates
    (
        ["pymupdf", "reducto"],
        {"steps": [{"backend": "pymupdf"}, {"backend": "reducto"}]},
    ),
    # parallel branches normalize; hedged branch keeps start_after; no escalate distribution
    (
        {
            "parallel": ["reducto", {"backend": "aws-textract", "start_after": "30s"}],
            "pick": "best",
        },
        {
            "parallel": [
                {"backend": "reducto"},
                {"backend": "aws-textract", "start_after": "30s"},
            ],
            "pick": "best",
        },
    ),
    # route targets + decide members normalize; use refs stay by NAME
    (
        {
            "route": {
                "rules": [{"when": {"doc_type": ["invoice"]}, "use": "strategy:x"}],
                "default": "pymupdf",
            }
        },
        {
            "route": {
                "rules": [{"when": {"doc_type": ["invoice"]}, "use": {"use": "x"}}],
                "default": {"backend": "pymupdf"},
            }
        },
    ),
    (
        {"decide": {"among": ["strategy:a", "reducto"], "otherwise": "strategy:a"}},
        {"decide": {"among": [{"use": "a"}, {"backend": "reducto"}], "otherwise": {"use": "a"}}},
    ),
    # Plain (v0.7) desugar OUTPUTS are canonical longhand: normalize is the identity on them
    # (P1 §14; the desugar itself is proven in test_strategy_plain.py). Guarding a few here keeps
    # the fixed-point property co-located with the normalizer's own table.
    (
        {
            "steps": [
                {"backend": "pymupdf", "escalate_if": {"garbled": True, "empty_pages_over": 0.2}},
                {"backend": "reducto"},
            ]
        },
        {
            "steps": [
                {"backend": "pymupdf", "escalate_if": {"garbled": True, "empty_pages_over": 0.2}},
                {"backend": "reducto"},
            ]
        },
    ),
    (
        {
            "parallel": [{"backend": "pymupdf"}, {"backend": "docling"}],
            "pick": "fastest",
            "on_win": "cancel",
        },
        {
            "parallel": [{"backend": "pymupdf"}, {"backend": "docling"}],
            "pick": "fastest",
            "on_win": "cancel",
        },
    ),
    (
        {
            "steps": [
                {
                    "parallel": [{"backend": "docling"}, {"backend": "aws-textract"}],
                    "pick": "best",
                    "require": "all",
                    "escalate_if": {
                        "any_of": [
                            {"disagreement_over": 0.3},
                            {
                                "all_of": [
                                    {"scanned_pages_detected": True},
                                    {"chars_per_page_below": 100},
                                ]
                            },
                            {"garbled": True},
                            {"empty_pages_over": 0.2},
                        ]
                    },
                },
                {"backend": "reducto"},
            ],
        },
        {
            "steps": [
                {
                    "parallel": [{"backend": "docling"}, {"backend": "aws-textract"}],
                    "pick": "best",
                    "require": "all",
                    "escalate_if": {
                        "any_of": [
                            {"disagreement_over": 0.3},
                            {
                                "all_of": [
                                    {"scanned_pages_detected": True},
                                    {"chars_per_page_below": 100},
                                ]
                            },
                            {"garbled": True},
                            {"empty_pages_over": 0.2},
                        ]
                    },
                },
                {"backend": "reducto"},
            ],
        },
    ),
]


@pytest.mark.parametrize("shorthand,longhand", EXPANSIONS)
def test_shorthand_expands_to_longhand(shorthand, longhand):
    assert normalize_node(shorthand) == longhand


@pytest.mark.parametrize("shorthand,longhand", EXPANSIONS)
def test_normalize_is_a_fixed_point(shorthand, longhand):
    once = normalize_node(shorthand)
    twice = normalize_node(once)
    assert once == twice == longhand  # normalize(longhand) == longhand


def test_nested_cascade_step_is_skipped_by_distribution_and_idempotent():
    # a nested-cascade step owns its escalation; the outer default bundle does not attach to it,
    # so a second normalize pass never re-distributes (D-v3-7).
    node = {
        "steps": [
            {"steps": ["pymupdf", "tesseract"]},  # nested cascade, non-final
            "reducto",
        ],
        "escalate_if": "default",
    }
    once = normalize_node(node)
    assert "escalate_if" not in once["steps"][0]  # inner cascade untouched
    assert normalize_node(once) == once


def test_default_bundle_is_the_spec_bundle():
    assert BUNDLE == {
        "scanned_pages_detected": True,
        "garbled": True,
        "empty_pages_over": 0.2,
        "confidence_below": 0.6,
    }


# ---- presets (spec §2.8) ----------------------------------------------------------------------


def test_presets_normalize_to_spec_longhand():
    cost_saver = normalize_strategy("cost_saver", None)
    assert cost_saver == {
        "intent": "Local parse first; escalate to the router's best remaining pick only on bad quality.",
        "steps": [
            {"backend": "pymupdf", "escalate_if": BUNDLE},
            {"backend": "docling", "escalate_if": BUNDLE},
            {"backend": "auto"},
        ],
    }
    fast = normalize_strategy("fast", None)
    assert fast == {
        "intent": "Lowest latency: race the local parsers, keep the first success.",
        "parallel": [{"backend": "pymupdf"}, {"backend": "tesseract"}],
        "pick": "fastest",
        "on_win": "cancel",
    }


def test_all_presets_normalize_and_are_fixed_points():
    for name in ("cost_saver", "max_accuracy", "fast", "offline_first"):
        once = normalize_strategy(name, None)
        assert normalize_node(once) == once


# ---- extends ----------------------------------------------------------------------------------


def _config(strategies: dict) -> StrategyConfig:
    return StrategyConfig.model_validate({"version": 1, "strategies": strategies})


def test_extends_shallow_field_level_merge():
    cfg = _config(
        {
            "base": {"steps": ["pymupdf", "reducto"], "budget": {"max_duration": "10s"}},
            "invoices": {"extends": "base", "budget": {"max_duration": "30s"}},
        }
    )
    out = normalize_strategy("invoices", cfg)
    # base steps inherited; extending budget replaces the base's wholesale
    assert out["budget"] == {"max_duration": "30s"}
    assert [s["backend"] for s in out["steps"]] == ["pymupdf", "reducto"]


def test_extends_a_preset():
    cfg = _config({"strict": {"extends": "cost_saver", "budget": {"max_duration": "99s"}}})
    out = normalize_strategy("strict", cfg)
    assert out["budget"] == {"max_duration": "99s"}
    assert out["steps"][0]["backend"] == "pymupdf"


def test_extends_cycle_errors():
    cfg = _config({"a": {"extends": "b", "steps": ["x"]}, "b": {"extends": "a", "steps": ["y"]}})
    with pytest.raises(NormalizeError, match="cycle"):
        normalize_strategy("a", cfg)


def test_cascade_escalate_if_off_and_false_mean_no_gates():
    # YAML coerces an unquoted `off`/`no`/`false` to the boolean False; the grammar treats it (and
    # the string "off") as "no cascade gates" — the doc-as-written `escalate_if: off` must work.
    for value in ("off", False):
        cfg = _config({"s": {"steps": ["pymupdf", "reducto"], "escalate_if": value}})
        out = normalize_strategy("s", cfg)
        assert all("escalate_if" not in step for step in out["steps"])  # no gate distributed


def test_schema_accepts_escalate_if_false():
    # the vendored JSON Schema (checked in the loader) must accept the boolean form, not only "off"
    from openreading.schemas import validate_strategy_config

    validate_strategy_config(
        {"version": 1, "strategies": {"s": {"steps": ["pymupdf", "reducto"], "escalate_if": False}}}
    )


def test_extends_unknown_base_errors():
    cfg = _config({"a": {"extends": "ghost", "steps": ["x"]}})
    with pytest.raises(NormalizeError, match="unknown"):
        normalize_strategy("a", cfg)


def test_preset_name_collision_errors():
    cfg = _config({"cost_saver": ["pymupdf"]})
    with pytest.raises(NormalizeError, match="collides"):
        build_library(cfg)


def test_normalize_config_covers_all_user_strategies():
    cfg = _config(
        {"a": ["pymupdf"], "b": {"parallel": ["pymupdf", "tesseract"], "pick": "fastest"}}
    )
    out = normalize_config(cfg)
    assert set(out) == {"a", "b"}
    assert out["a"] == {"steps": [{"backend": "pymupdf"}]}


# ---- decide: candidate labels must be distinct (BL-35) -----------------------------------------
#
# The engine names every `among:` member with `candidate_label` and dispatches on that name
# (engine._eval_decide). Two members resolving to the same name make the later one permanently
# unreachable — silently, with a normal-looking trace. Normalize fails closed instead.

_RACE = {"parallel": ["pymupdf", "tesseract"], "pick": "fastest"}
_BEST = {"parallel": ["docling", "reducto"], "pick": "best"}


def _decide(among: list, otherwise) -> dict:
    return {"decide": {"among": among, "otherwise": otherwise}}


def test_two_anonymous_same_kind_candidates_collide():
    with pytest.raises(NormalizeError, match="parallel") as exc:
        normalize_node(_decide([_RACE, _BEST], _RACE))
    assert "decide" in str(exc.value)


def test_candidate_collision_names_the_decide_node_path_and_the_label():
    cfg = _config({"s": {"steps": ["pymupdf", _decide([_RACE, _BEST], _RACE)]}})
    with pytest.raises(NormalizeError) as exc:
        normalize_strategy("s", cfg)
    assert "strategies.s.steps[1].decide" in str(exc.value)
    assert "'parallel'" in str(exc.value)


def test_two_anonymous_candidates_of_different_kinds_normalize():
    # no false positive: `parallel` and `steps` are already distinct names.
    node = _decide([_RACE, {"steps": ["docling", "reducto"]}], _RACE)
    out = normalize_node(node)
    assert [candidate_label(m) for m in out["decide"]["among"]] == ["parallel", "steps"]
    assert normalize_node(out) == out


def test_explicit_label_disambiguates_same_kind_candidates():
    out = normalize_node(_decide([{**_RACE, "label": "race"}, _BEST], _RACE))
    assert [candidate_label(m) for m in out["decide"]["among"]] == ["race", "parallel"]


def test_duplicate_leaf_candidates_collide():
    # two leaves on the same backend are one name to the decider, whatever their `with:` differs on
    among = [{"backend": "reducto", "with": {"mode": "fast"}}, {"backend": "reducto"}]
    with pytest.raises(NormalizeError, match="reducto"):
        normalize_node(_decide(among, among[0]))


def test_candidate_named_otherwise_collides_with_the_fallthrough():
    # `otherwise` is the engine's own candidate name; a member claiming it is never dispatched.
    with pytest.raises(NormalizeError, match="otherwise"):
        normalize_node(_decide([{"use": "otherwise"}, "reducto"], "reducto"))


# ---- CLI: strategy show | list | normalize ----------------------------------------------------


@pytest.fixture
def _clean_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENREADING_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_cli_strategy_show_preset(_clean_cwd, capsys):
    rc = main(["strategy", "show", "cost_saver"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "cost_saver:" in out
    assert "pymupdf" in out and "escalate_if" in out


def test_cli_strategy_show_unknown_errors(_clean_cwd, capsys):
    rc = main(["strategy", "show", "nope"])
    assert rc == 3
    assert "unknown strategy" in capsys.readouterr().err


def test_cli_strategy_list_presets(_clean_cwd, capsys):
    rc = main(["strategy", "list"])
    out = capsys.readouterr().out
    assert rc == 0
    for name in ("cost_saver", "max_accuracy", "fast", "offline_first"):
        assert name in out


def test_cli_strategy_normalize_roundtrip(_clean_cwd, capsys):
    f = _clean_cwd / "openreading.yaml"
    f.write_text("version: 1\nstrategies:\n  cheap: [pymupdf, reducto]\n")
    rc = main(["strategy", "normalize", "--config", str(f)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "cheap:" in out and "steps:" in out and "backend: pymupdf" in out


def test_cli_strategy_normalize_no_config_errors(_clean_cwd, capsys):
    rc = main(["strategy", "normalize"])
    assert rc == 3
    assert "no openreading.yaml" in capsys.readouterr().err


# ---- P5: strategy show (as-written / --longhand) + normalize docs-as-golden (§10, T6) ----------

_PLAIN_CFG = (
    "version: 1\nstrategies:\n  s:\n    try: [pymupdf, reducto]\n    escalate_when: looks_bad\n"
)


def test_cli_show_plain_body_as_written(_clean_cwd, capsys):
    f = _clean_cwd / "openreading.yaml"
    f.write_text(_PLAIN_CFG)
    rc = main(["strategy", "show", "s", "--config", str(f)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "try:" in out and "escalate_when:" in out  # Plain prints Plain
    assert "steps:" not in out and "escalate_if:" not in out


def test_cli_show_longhand_prints_canonical_tree(_clean_cwd, capsys):
    f = _clean_cwd / "openreading.yaml"
    f.write_text(_PLAIN_CFG)
    rc = main(["strategy", "show", "s", "--config", str(f), "--longhand"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "steps:" in out and "escalate_if:" in out  # canonical tree
    assert "try:" not in out


def test_cli_normalize_contracts_matches_section_6_3(_clean_cwd, capsys):
    import yaml

    f = _clean_cwd / "openreading.yaml"
    f.write_text(
        "version: 1\nstrategies:\n  contracts:\n    compare: [docling, aws-textract]\n"
        "    then: reducto\n    max_time: 45s\n"
    )
    rc = main(["strategy", "normalize", "--config", str(f)])
    assert rc == 0
    parsed = yaml.safe_load(capsys.readouterr().out)
    scan_pair = {"all_of": [{"scanned_pages_detected": True}, {"chars_per_page_below": 100}]}
    expected = {
        "steps": [
            {
                "parallel": [{"backend": "docling"}, {"backend": "aws-textract"}],
                "pick": "best",
                "require": "all",
                "escalate_if": {
                    "any_of": [
                        {"disagreement_over": 0.3},
                        scan_pair,
                        {"garbled": True},
                        {"empty_pages_over": 0.2},
                    ]
                },
            },
            {"backend": "reducto"},
        ],
        "budget": {"max_duration": "45s"},
    }
    assert parsed["strategies"]["contracts"] == expected  # docs-as-golden, compared as data


def test_cli_show_longhand_unknown_errors(_clean_cwd, capsys):
    rc = main(["strategy", "show", "nope", "--longhand"])
    assert rc == 3
    assert "unknown strategy" in capsys.readouterr().err
