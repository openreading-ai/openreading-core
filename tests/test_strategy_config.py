"""Milestone 11.1 — the strategy config spine: vendored schema + models + loader.

Covers: the schema is valid JSON Schema; the complete v0.1 grammar accepts the shorthand and
longhand forms and rejects malformed ones; safe_load refuses non-safe tags; the provenance hash
is deterministic; typed top-level access. Discovery order lives in `tests/test_config.py` with the
module that owns it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openreading import schemas
from openreading.strategies import StrategyConfig, load_config, resolve_strategy
from openreading.strategies.loader import ConfigError, parse_config, strip_strategy_prefix

SCHEMA_FILE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "openreading"
    / "schemas"
    / "strategy-config.v0.1.json"
)


def test_schema_is_valid_json_schema():
    from jsonschema.validators import validator_for

    schema = json.loads(SCHEMA_FILE.read_text())
    cls = validator_for(schema)
    cls.check_schema(schema)  # raises if not a valid draft-2020-12 schema


VALID_CONFIGS = [
    # bare-string leaf shorthand + list cascade shorthand
    {"version": 1, "strategies": {"x": "reducto", "y": ["pymupdf", "reducto"]}},
    # cascade with default bundle + tuned per-step gate map (keys OR)
    {
        "version": 1,
        "strategies": {
            "cheap": {
                "steps": [
                    {
                        "backend": "pymupdf",
                        "escalate_if": {"chars_per_page_below": 100, "garbled": True},
                    },
                    "reducto",
                ],
                "escalate_if": "default",
            }
        },
    },
    # parallel race, parallel judged, decide, route
    {"version": 1, "strategies": {"r": {"parallel": ["pymupdf", "tesseract"], "pick": "fastest"}}},
    {
        "version": 1,
        "strategies": {
            "j": {
                "parallel": ["reducto", "azure-document-intelligence"],
                "pick": "best",
                "judge": {"backend": "anthropic-claude", "intent": "prefer tables"},
            }
        },
    },
    {
        "version": 1,
        "strategies": {
            "d": {"decide": {"among": ["strategy:a", "strategy:b"], "otherwise": "strategy:b"}}
        },
    },
    {
        "version": 1,
        "strategies": {
            "route1": {
                "route": {
                    "rules": [
                        {"when": {"doc_type": ["invoice"], "pages_over": 5}, "use": "strategy:x"}
                    ],
                    "default": "pymupdf",
                }
            }
        },
    },
    # hedged start, on_missing wrapper, per-field confidence, deployment blocks
    {
        "version": 1,
        "strategies": {
            "h": {
                "parallel": ["reducto", {"backend": "aws-textract", "start_after": "30s"}],
                "pick": "best",
                "budget": {"max_duration": "30s"},
            }
        },
    },
    {
        "version": 1,
        "strategies": {
            "m": {
                "steps": [
                    {
                        "backend": "pymupdf",
                        "escalate_if": {
                            "confidence_below": {"value": 0.7, "on_missing": "escalate"}
                        },
                    },
                    "reducto",
                ]
            }
        },
    },
    {
        "version": 1,
        "strategies": {
            "f": {
                "steps": [
                    {
                        "backend": "aws-textract",
                        "escalate_if": {"field_confidence_below": {"fields": {"total": 0.9}}},
                    },
                    "anthropic-claude",
                ]
            }
        },
    },
    {
        "version": 1,
        "policy": {"backends": ["pymupdf", "tesseract"]},
        "limits": {"max_duration_per_doc": "10m"},
        "decider": {
            "llm": {"backend": "anthropic-claude", "timeout": "5s", "send_document_content": False}
        },
        "defaults": {
            "strategy": "cheap",
            "advanced": {
                "circuit_breaker": {"max_fails": 5, "cooldown": "30s"},
            },
        },
        "strategies": {"cheap": ["pymupdf", "reducto"]},
    },
]

INVALID_CONFIGS = [
    ({"strategies": {}}, "missing version"),
    ({"version": 2, "strategies": {}}, "wrong version const"),
    (
        {"version": 1, "strategies": {"x": {"steps": ["a"], "escalate_if": "maybe"}}},
        "bad escalate_if literal",
    ),
    ({"version": 1, "strategies": {"x": {"route": {"rules": []}}}}, "route missing default"),
    (
        {"version": 1, "strategies": {"x": {"backend": "pymupdf", "timeout": "90"}}},
        "bare-number duration",
    ),
    (
        {"version": 1, "strategies": {"x": {"backend": "pymupdf", "with": {"nope": 1}}}},
        "with forbidden key",
    ),
    (
        {"version": 1, "strategies": {"x": {"decide": {"among": ["a"], "otherwise": "a"}}}},
        "decide among<2",
    ),
    (
        {"version": 1, "strategies": {"x": {"parallel": ["a"], "pick": "fastest"}}},
        "parallel <2 branches",
    ),
    (
        {"version": 1, "strategies": {"x": {"steps": ["a"], "on_error": {"compliance": "next"}}}},
        "on_error names compliance",
    ),
    (
        {
            "version": 1,
            "strategies": {"x": {"steps": ["a"], "on_error": {"missing_credentials": "skip"}}},
        },
        "on_error names missing_credentials",
    ),
    ({"version": 1, "extra_top_key": 1, "strategies": {}}, "unknown top-level key"),
]


@pytest.mark.parametrize("cfg", VALID_CONFIGS)
def test_valid_configs_pass_schema_and_model(cfg):
    schemas.validate_strategy_config(cfg)  # no raise
    model = StrategyConfig.model_validate(cfg)
    assert model.version == 1


@pytest.mark.parametrize("cfg,label", INVALID_CONFIGS)
def test_invalid_configs_rejected(cfg, label):
    from jsonschema.exceptions import ValidationError

    with pytest.raises(ValidationError):
        schemas.validate_strategy_config(cfg)


# --- Plain dialect (P0 — grammar only; desugar/behavior land in later phases) ---------------
#
# P0 widens the schema to ADMIT Plain map bodies (internal/design/simple-strategies.md §3) and adds
# the `disagreement_over` gate predicate (§11 phase-1 grammar). No desugar yet — these tests
# assert the schema/model accept well-formed Plain bodies and reject malformed ones. Semantic
# checks that §8 assigns to desugar-time (unknown names, disagree-outside-compare, single-item
# try + gate) are NOT schema errors and are deliberately absent here; they land in P1.


def _wrap(body):
    return {"version": 1, "strategies": {"s": body}}


PLAIN_VALID_BODIES = [
    # try: single item and list; the bare-list shorthand is already covered by VALID_CONFIGS
    "reducto",  # bare string leaf is dialect-plain but not a *map* body; kept as a sanity anchor
    {"try": "reducto"},
    {"try": ["pymupdf", "reducto"]},
    # escalate_when: scalar looks_bad, member true, member overlay map
    {"try": ["pymupdf", "reducto"], "escalate_when": "looks_bad"},
    {"try": ["pymupdf", "reducto"], "escalate_when": {"looks_bad": True}},
    {
        "try": ["pymupdf", "reducto"],
        "escalate_when": {"looks_bad": {"empty_pages": 0.3, "no_text_from_images": False}},
    },
    {"try": ["pymupdf", "reducto"], "escalate_when": {"looks_bad": {"min_text_per_page": 100}}},
    # the four criteria words, incl. missing: and low_confidence: as bool or frac
    {
        "try": ["reducto", "anthropic-claude"],
        "escalate_when": {"missing": ["invoice_number", "total_amount"], "low_confidence": 0.8},
    },
    {"try": ["pymupdf", "reducto"], "escalate_when": {"low_confidence": True}},
    # walls (time only)
    {"try": ["pymupdf", "reducto"], "max_time": "2m"},
    # race
    {"race": ["pymupdf", "docling"]},
    {"race": ["pymupdf", "docling"], "max_time": "30s"},
    # compare: alone, with then, with explicit escalate_when incl. disagree, 3-way
    {"compare": ["docling", "aws-textract"]},
    {"compare": ["docling", "aws-textract"], "then": "reducto"},
    {
        "compare": ["docling", "aws-textract"],
        "then": "reducto",
        "escalate_when": {"disagree": True, "looks_bad": True},
    },
    {"compare": ["a", "b", "c"], "then": "reducto"},
]

PLAIN_INVALID_BODIES = [
    ({"try": ["a"], "race": ["b", "c"]}, "two of try/race/compare"),
    ({"race": ["only-one"]}, "race < 2 items"),
    ({"compare": ["only-one"]}, "compare < 2 items"),
    ({"then": "reducto"}, "then without compare"),
    ({"race": ["a", "b"], "escalate_when": "looks_bad"}, "escalate_when beside race"),
    (
        {"compare": ["a", "b"], "escalate_when": {"looks_bad": True}},
        "compare escalate_when no then",
    ),
    ({"try": ["a", "b"], "escalate_when": "bad_word"}, "escalate_when scalar not looks_bad"),
    ({"try": ["a", "b"], "escalate_when": {}}, "empty escalate_when map"),
    ({"try": ["a", "b"], "escalate_when": {"nope": True}}, "unknown criterion key"),
    (
        {"try": ["a", "b"], "escalate_when": {"looks_bad": {"nope": True}}},
        "unknown looks_bad member",
    ),
    ({"try": ["a", "b"], "escalate_when": {"low_confidence": 1.5}}, "threshold outside domain"),
    ({"try": ["a", "b"], "escalate_when": {"missing": []}}, "missing: empty"),
    ({"try": ["a", "b"], "escalate_when": {"missing": [123]}}, "missing: non-string entry"),
    ({"compare": ["a", "b"], "then": ["x", "y"]}, "then with >1 target"),
    ({"try": ["a", "b"], "max_time": "90"}, "max_time bare number"),
    ({"try": ["a", "b"], "max_cost": 0.5}, "max_cost is no longer a body key"),
    ({"try": ["a", "b"], "unknown_key": 1}, "body key outside the set"),
]


@pytest.mark.parametrize("body", PLAIN_VALID_BODIES)
def test_plain_valid_bodies_pass_schema_and_model(body):
    cfg = _wrap(body)
    schemas.validate_strategy_config(cfg)  # no raise
    assert StrategyConfig.model_validate(cfg).version == 1


@pytest.mark.parametrize("body,label", PLAIN_INVALID_BODIES)
def test_plain_invalid_bodies_rejected(body, label):
    from jsonschema.exceptions import ValidationError

    with pytest.raises(ValidationError):
        schemas.validate_strategy_config(_wrap(body))


def test_escalate_if_beside_parallel_step_is_already_valid():
    # P0 note (§14): the step schema already admits `escalate_if` beside `parallel` — the P2
    # engine change makes it EVALUATE, but the shape has always validated. No step-schema edit.
    cfg = _wrap(
        {
            "steps": [
                {
                    "parallel": [{"backend": "docling"}, {"backend": "aws-textract"}],
                    "pick": "best",
                    "escalate_if": {"garbled": True},
                },
                "reducto",
            ],
        }
    )
    schemas.validate_strategy_config(cfg)  # no raise


def test_compare_then_reference_longhand_is_schema_valid():
    # The §6.3 canonical compiled tree for `contracts` — the shape P1's desugar must emit and
    # `strategy normalize` must print. Proves the schema admits it: the nested any_of>all_of
    # combinator and the new `disagreement_over` predicate both validate.
    contracts = {
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
    }
    schemas.validate_strategy_config(_wrap(contracts))  # no raise


def test_disagreement_over_is_a_gate_predicate():
    # §11 phase-1 grammar: the predicate must PARSE from P0 (so P1's compare desugar output is
    # schema-valid) even though the signal is computed only in P7.
    schemas.validate_strategy_config(
        _wrap({"steps": [{"backend": "docling", "escalate_if": {"disagreement_over": 0.4}}]})
    )
    from jsonschema.exceptions import ValidationError

    with pytest.raises(ValidationError):  # still domain-checked
        schemas.validate_strategy_config(
            _wrap({"steps": [{"backend": "docling", "escalate_if": {"disagreement_over": 2}}]})
        )


def test_parse_config_wraps_schema_error_with_location():
    with pytest.raises(ConfigError) as ei:
        parse_config(
            "version: 1\nstrategies:\n  x:\n    route:\n      rules: []\n", source="f.yaml"
        )
    assert "f.yaml" in str(ei.value)


def test_yaml_syntax_error_names_the_source_not_a_placeholder():
    # PyYAML labels a bare `str` input `<unicode string>`, which contradicted the filename the
    # ConfigError prefix already printed. The loader passes a named stream so both agree.
    with pytest.raises(ConfigError) as ei:
        parse_config("version: 1\nstrategies:\n  a: [unclosed\n", source="bad.yaml")
    message = str(ei.value)
    assert "<unicode string>" not in message
    assert 'in "bad.yaml", line 3' in message


def test_parse_config_rejects_non_mapping_and_empty():
    with pytest.raises(ConfigError):
        parse_config("- just\n- a\n- list\n")
    with pytest.raises(ConfigError):
        parse_config("")


def test_safe_load_refuses_arbitrary_tags():
    # A python/object tag is only honored by unsafe loaders; safe_load raises → ConfigError.
    text = "version: 1\nstrategies:\n  x: !!python/object/apply:os.system ['echo hi']\n"
    with pytest.raises(ConfigError):
        parse_config(text)


def test_load_config_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENREADING_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    assert load_config() is None


def test_load_config_hash_is_deterministic(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENREADING_CONFIG", raising=False)
    f = tmp_path / "openreading.yaml"
    f.write_text("version: 1\nstrategies:\n  cheap: [pymupdf, reducto]\n")
    a = load_config(str(f))
    b = load_config(str(f))
    assert a is not None and b is not None
    assert a.source_hash == b.source_hash
    assert a.source_hash.startswith("sha256:")
    assert a.config.strategy_names() == ["cheap"]


def test_strip_strategy_prefix():
    assert strip_strategy_prefix("strategy:cheap_first") == "cheap_first"
    assert strip_strategy_prefix("strategy:none") == "none"
    assert strip_strategy_prefix("reducto") is None
    assert strip_strategy_prefix("auto") is None


def test_resolve_strategy_unknown_name_errors(tmp_path):
    cfg = StrategyConfig.model_validate({"version": 1, "strategies": {"cheap": ["pymupdf"]}})
    assert resolve_strategy(cfg, "cheap") == ["pymupdf"]
    with pytest.raises(ConfigError) as ei:
        resolve_strategy(cfg, "ghost")
    assert "cheap" in str(ei.value)  # names the known strategies


# --- `auto` is gone from every dialect (Akshay, 2026-09-07) ------------------------------------
#
# The removal set took `auto` off the request and out of the Plain dialect and left it live in
# longhand, where `engine._resolve_backend` still resolved it against the policy chain. That split
# is what produced every `auto` defect in this branch: one keyword, three surfaces, each answering
# differently. It is one answer now — name the backend, or state the order once in
# `policy.backends`.


@pytest.mark.parametrize(
    "body",
    [
        "    steps:\n      - backend: auto\n",
        "    steps: [auto]\n",
        "    try: [pymupdf, auto]\n    escalate_when: looks_bad\n",
        "    parallel:\n      - backend: pymupdf\n      - backend: auto\n    pick: best\n",
    ],
    ids=["longhand-leaf", "shorthand-string", "plain-try-rung", "parallel-branch"],
)
def test_auto_is_refused_in_every_dialect(body):
    """Longhand, shorthand, Plain and a parallel branch all refuse it, and all say the same thing."""
    with pytest.raises(ConfigError) as e:
        parse_config(f"version: 1\nstrategies:\n  s:\n{body}")

    assert "auto" in str(e.value)
