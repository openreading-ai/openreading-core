"""Milestone 11.3 — world-consistency validation (spec §9) + `strategy validate` CLI.

Golden error-message coverage (harness H4a): every catalog error/warning has a case, and the
message locates (file + node path), explains, and suggests a fix.
"""

from __future__ import annotations

import json

import pytest

from openreading.cli.app import main
from openreading.strategies import StrategyConfig, validate_config


def _issues(cfg, policy=None):
    model = StrategyConfig.model_validate(cfg)
    return validate_config(model, policy=policy, raw=cfg)


def _errors(cfg, policy=None):
    return [i for i in _issues(cfg, policy) if i.level == "error"]


def _warnings(cfg, policy=None):
    return [i for i in _issues(cfg, policy) if i.level == "warning"]


def _has(issues, path_frag, msg_frag):
    return any(path_frag in i.path and msg_frag in i.message for i in issues)


# ---- load-time errors -------------------------------------------------------------------------


def test_unknown_backend_id():
    errs = _errors({"version": 1, "strategies": {"x": ["pymupdf", "ghostbackend"]}})
    assert _has(errs, "steps[1].backend", "unknown backend 'ghostbackend'")
    assert any("known:" in i.message for i in errs)  # suggests the valid set


def test_granularity_page_warns_backend_without_page_ranges():
    # no builtin backend declares native page-range selection yet → an escalation rung under
    # granularity:page runs document granularity, and validate says so (spec §2.7).
    warns = _warnings(
        {
            "version": 1,
            "strategies": {
                "x": {
                    "granularity": "page",
                    "steps": [
                        {"backend": "pymupdf", "escalate_if": {"confidence_below": 0.8}},
                        "reducto",
                    ],
                }
            },
        }
    )
    assert _has(warns, "steps[1]", "lacks native page-range selection")


def test_unknown_strategy_reference():
    errs = _errors(
        {
            "version": 1,
            "strategies": {
                "x": {"decide": {"among": ["strategy:ghost", "reducto"], "otherwise": "reducto"}}
            },
        }
    )
    assert _has(errs, "strategies.x", "unknown strategy reference 'ghost'")


def test_use_reference_cycle():
    errs = _errors({"version": 1, "strategies": {"a": "strategy:b", "b": "strategy:a"}})
    assert _has(errs, "strategies.a", "use reference cycle")


def test_extends_cycle():
    cfg = {
        "version": 1,
        "strategies": {
            "a": {"extends": "b", "steps": ["x"]},
            "b": {"extends": "a", "steps": ["y"]},
        },
    }
    assert _has(_errors(cfg), "strategies", "cycle")


def test_decide_otherwise_not_in_among():
    cfg = {
        "version": 1,
        "strategies": {
            "x": {"decide": {"among": ["strategy:a", "reducto"], "otherwise": "pymupdf"}},
            "a": ["pymupdf"],
        },
    }
    assert _has(_errors(cfg), "decide.otherwise", "must be one of the among candidates")


def test_decide_candidates_colliding_on_one_label():
    # BL-35: two anonymous parallel sub-trees are both named "parallel", so the second could never
    # be dispatched. Normalize fails closed; validate reports it instead of crashing the CLI.
    cfg = {
        "version": 1,
        "strategies": {
            "x": {
                "decide": {
                    "among": [
                        {"parallel": ["pymupdf", "tesseract"], "pick": "fastest"},
                        {"parallel": ["docling", "reducto"], "pick": "best"},
                    ],
                    "otherwise": {"parallel": ["pymupdf", "tesseract"], "pick": "fastest"},
                }
            }
        },
    }
    assert _has(_errors(cfg), "strategies.x", "both resolve to the candidate label 'parallel'")


def test_secret_pattern_key_in_open_subtree():
    # the policy block is an open superset; a secret hiding there is caught
    errs = _errors(
        {"version": 1, "policy": {"api_key": "sk-xxx"}, "strategies": {"x": ["pymupdf"]}}
    )
    assert _has(errs, "policy.api_key", "looks like a secret")


def test_with_allowlist_secret_also_caught():
    cfg = {
        "version": 1,
        "strategies": {
            "x": {
                "steps": [
                    {"backend": "reducto", "with": {"features": {"my_token": "t"}}},
                    "pymupdf",
                ]
            }
        },
    }
    assert _has(_errors(cfg), "token", "looks like a secret")


def test_sibling_branches_same_backend():
    cfg = {
        "version": 1,
        "strategies": {
            "x": {"parallel": ["reducto", {"use": "a"}], "pick": "fastest"},
            "a": ["reducto"],
        },
    }
    assert _has(_errors(cfg), "parallel", "can both dispatch")


def test_unbindable_gate_confidence_only_on_pymupdf():
    cfg = {
        "version": 1,
        "strategies": {
            "x": {
                "steps": [
                    {"backend": "pymupdf", "escalate_if": {"confidence_below": 0.6}},
                    "reducto",
                ]
            }
        },
    }
    errs = _errors(cfg)
    assert _has(errs, "steps[0].escalate_if", "can never fire on 'pymupdf'")
    assert any(
        "chars_per_page_below" in i.message or "garbled" in i.message for i in errs
    )  # suggests a fix


def test_default_bundle_is_not_unbindable_on_pymupdf():
    # the bundle's Tier-1 members always bind, so the natural exemption holds — no error
    cfg = {
        "version": 1,
        "strategies": {"x": {"steps": ["pymupdf", "reducto"], "escalate_if": "default"}},
    }
    assert not _errors(cfg)


def test_on_missing_escalate_makes_confidence_gate_bindable():
    cfg = {
        "version": 1,
        "strategies": {
            "x": {
                "steps": [
                    {
                        "backend": "pymupdf",
                        "escalate_if": {
                            "confidence_below": {"value": 0.6, "on_missing": "escalate"}
                        },
                    },
                    "reducto",
                ]
            }
        },
    }
    assert not _errors(cfg)


def _gate_cfg(gate):
    return {
        "version": 1,
        "strategies": {"x": {"steps": [{"backend": "pymupdf", "escalate_if": gate}, "reducto"]}},
    }


def test_unbindable_predicate_ord_beside_a_live_one_is_reported():
    # C4: `confidence_below` on pymupdf never fires. Alone it is a hard error; OR'd beside a
    # binding predicate the gate still works, so the dead half is dead weight, not a dead gate —
    # a warning at the leaf's own path, and validate must not stay silent about it.
    issues = _issues(
        _gate_cfg({"any_of": [{"confidence_below": 0.0}, {"chars_per_page_below": 50}]})
    )
    assert not [i for i in issues if i.level == "error"]
    warns = [i for i in issues if i.level == "warning"]
    assert _has(warns, "escalate_if.any_of[0].confidence_below", "can never fire on 'pymupdf'")
    assert any("dead weight" in i.message for i in warns)


def test_flat_gate_map_ors_its_keys_so_a_dead_key_is_reported_too():
    # a gate map ORs its keys, so this is the same shape as any_of with no list syntax.
    warns = _warnings(_gate_cfg({"confidence_below": 0.0, "chars_per_page_below": 50}))
    assert _has(warns, "escalate_if.confidence_below", "can never fire on 'pymupdf'")


def test_all_of_with_an_unbindable_member_can_never_fire():
    # C4, the sharp edge: `all_of` fires only when EVERY member fires, so one predicate that can
    # never fire kills the whole conjunction. Flattening the tree and asking "does ANY leaf bind?"
    # gets this backwards and passes a gate that is completely dead.
    errs = _errors(_gate_cfg({"all_of": [{"confidence_below": 0.0}, {"chars_per_page_below": 50}]}))
    assert _has(errs, "escalate_if", "can never fire on 'pymupdf'")
    assert any("all_of" in i.message for i in errs)


def test_all_of_dead_conjunct_is_still_dead_when_ord_beside_a_live_branch():
    # the live `garbled` branch keeps the gate alive, so the dead all_of is a warning, not an error.
    gate = {
        "any_of": [
            {"all_of": [{"confidence_below": 0.0}, {"chars_per_page_below": 50}]},
            {"garbled": True},
        ]
    }
    issues = _issues(_gate_cfg(gate))
    assert not [i for i in issues if i.level == "error"]
    assert _has(
        [i for i in issues if i.level == "warning"],
        "escalate_if.any_of[0].all_of[0].confidence_below",
        "can never fire on 'pymupdf'",
    )


def test_default_bundle_reports_no_dead_predicate_on_pymupdf():
    # the shipped `default` bundle carries `confidence_below` deliberately — a Tier-2 bonus that is
    # "silently inapplicable on confidence-less backends" (normalize.DEFAULT_BUNDLE). Designed
    # degradation is not dead weight, so it must stay silent on both levels.
    cfg = {
        "version": 1,
        "strategies": {"x": {"steps": ["pymupdf", "reducto"], "escalate_if": "default"}},
    }
    assert not _issues(cfg)


def test_preset_name_collision():
    cfg = {"version": 1, "strategies": {"cost_saver": ["pymupdf"]}}
    assert _has(_errors(cfg), "strategies", "collides")


def test_strategy_none_as_node():
    cfg = {
        "version": 1,
        "strategies": {
            "x": {"decide": {"among": ["strategy:none", "reducto"], "otherwise": "reducto"}}
        },
    }
    assert _has(_errors(cfg), "strategies.x", "strategy:none is the reserved escape hatch")


# ---- warnings ---------------------------------------------------------------------------------


def test_shadowed_route_rule_warns():
    cfg = {
        "version": 1,
        "strategies": {
            "x": {
                "route": {
                    "rules": [
                        {"when": {"doc_type": ["invoice"]}, "use": "pymupdf"},
                        {"when": {"doc_type": ["invoice"]}, "use": "reducto"},
                    ],
                    "default": "pymupdf",
                }
            }
        },
    }
    assert _has(_warnings(cfg), "route.rules[1]", "duplicates an earlier rule")


def test_timeout_exceeds_deadline_warns():
    cfg = {
        "version": 1,
        "strategies": {
            "x": {
                "steps": [{"backend": "reducto", "timeout": "5m"}, "pymupdf"],
                "budget": {"max_duration": "30s"},
            }
        },
    }
    assert _has(_warnings(cfg), "steps[0].timeout", "exceeds the effective deadline")


def test_review_if_without_decider_warns():
    cfg = {
        "version": 1,
        "strategies": {
            "x": {
                "steps": [{"backend": "reducto", "review_if": {"confidence_below": 0.8}}, "pymupdf"]
            }
        },
    }
    assert _has(_warnings(cfg), "review_if", "no decider is configured")


def test_judged_over_three_candidates_warns():
    cfg = {
        "version": 1,
        "strategies": {
            "x": {
                "parallel": [
                    "reducto",
                    "aws-textract",
                    "azure-document-intelligence",
                    "anthropic-claude",
                ],
                "pick": "best",
                "judge": {"backend": "anthropic-claude"},
            }
        },
    }
    assert _has(_warnings(cfg), "strategies.x", ">3 candidates")


def test_policy_unreachable_step_warns():
    cfg = {"version": 1, "strategies": {"x": ["pymupdf", "reducto"]}}
    w = _warnings(cfg, policy={"require_local": True})
    assert _has(w, "steps[1].backend", "filtered out by the policy")


def test_clean_config_no_issues():
    cfg = {
        "version": 1,
        "strategies": {
            "cheap": {"steps": ["pymupdf", "reducto"], "escalate_if": "default"},
            "race": {"parallel": ["pymupdf", "tesseract"], "pick": "fastest"},
        },
    }
    assert _issues(cfg) == []


# ---- CLI --------------------------------------------------------------------------------------


@pytest.fixture
def _clean_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENREADING_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_cli_validate_clean_exit0(_clean_cwd, capsys):
    f = _clean_cwd / "openreading.yaml"
    f.write_text("version: 1\nstrategies:\n  cheap: [pymupdf, reducto]\n")
    rc = main(["strategy", "validate", "--config", str(f)])
    assert rc == 0
    assert "OK" in capsys.readouterr().out


def test_cli_validate_error_exit3(_clean_cwd, capsys):
    f = _clean_cwd / "openreading.yaml"
    f.write_text("version: 1\nstrategies:\n  x: [pymupdf, ghostbackend]\n")
    rc = main(["strategy", "validate", "--config", str(f)])
    cap = capsys.readouterr()
    assert rc == 3
    assert "unknown backend" in cap.err
    assert str(f) in cap.err  # located to the file


def test_cli_validate_schema_error_located(_clean_cwd, capsys):
    f = _clean_cwd / "openreading.yaml"
    f.write_text(
        "version: 1\nstrategies:\n  x:\n    backend: pymupdf\n    timeout: '90'\n"
    )  # bare-number duration
    rc = main(["strategy", "validate", "--config", str(f)])
    assert rc == 3
    assert str(f) in capsys.readouterr().err


def test_cli_validate_with_policy(_clean_cwd, capsys, tmp_path):
    f = _clean_cwd / "openreading.yaml"
    f.write_text("version: 1\nstrategies:\n  x: [pymupdf, reducto]\n")
    pol = _clean_cwd / "phi.json"
    pol.write_text(json.dumps({"require_local": True}))
    rc = main(["strategy", "validate", "--config", str(f), "--policy", str(pol)])
    # warnings don't fail the exit code
    assert rc == 0
    assert "filtered out by the policy" in capsys.readouterr().out


def test_cli_validate_unreadable_policy_exits_3_without_a_traceback(_clean_cwd, capsys):
    # An unreadable --policy fails the same way an unreadable --config does: one tagged stderr
    # line, exit 3. main() returning at all is what pins "no traceback".
    f = _clean_cwd / "openreading.yaml"
    f.write_text("version: 1\nstrategies:\n  x: [pymupdf]\n")
    malformed = _clean_cwd / "bad.json"
    malformed.write_text("{not json")
    for pol in (_clean_cwd / "missing.json", malformed):
        rc = main(["strategy", "validate", "--config", str(f), "--policy", str(pol)])
        assert rc == 3
        err = capsys.readouterr().err
        assert err.startswith("[strategy validate] cannot read policy")
        assert len(err.splitlines()) == 1


# ---- P3: Plain-dialect validation (§8 strategy-validate rows, harness §15 T4/T6) --------------
#
# These mirror the loader path: desugar the Plain body first, then validate the longhand with the
# PlainInfo, so messages come back in Plain vocabulary. Each §8 row gets a triggering input and a
# message-content assertion; the §4 milestone file must validate with ZERO errors and warnings.

from openreading.strategies.plain import desugar_config  # noqa: E402


def _plain_issues(cfg, policy=None):
    desugared, plain_info = desugar_config(cfg)
    model = StrategyConfig.model_validate(desugared)
    return validate_config(model, policy=policy, raw=desugared, plain_info=plain_info)


def _plain_errors(cfg):
    return [i for i in _plain_issues(cfg) if i.level == "error"]


def _plain_warnings(cfg):
    return [i for i in _plain_issues(cfg) if i.level == "warning"]


def test_plain_row2_unbindable_gate_in_plain_vocabulary():
    cfg = {
        "version": 1,
        "strategies": {
            "s": {"try": ["pymupdf", "reducto"], "escalate_when": {"low_confidence": 0.7}}
        },
    }
    errs = _plain_errors(cfg)
    assert _has(errs, "steps[0]", "low_confidence")  # Plain word, not confidence_below
    assert _has(errs, "steps[0]", "looks_bad")  # suggests the always-available Plain criterion


def test_plain_row3_missing_on_fieldless_backend_warns():
    cfg = {
        "version": 1,
        "strategies": {
            "s": {"try": ["pymupdf", "reducto"], "escalate_when": {"missing": ["total"]}}
        },
    }
    warns = _plain_warnings(cfg)
    assert _has(warns, "steps[0]", "missing")
    assert any("always escalate" in w.message for w in warns)


def test_plain_row4_escalate_when_does_not_apply_to_a_reference():
    cfg = {
        "version": 1,
        "strategies": {
            "helper": ["pymupdf", "reducto"],
            "s": {"try": ["helper", "reducto"], "escalate_when": "looks_bad"},
        },
    }
    warns = _plain_warnings(cfg)
    assert _has(warns, "steps[0]", "escalate_when")
    assert any("its own" in w.message or "does not apply" in w.message for w in warns)


def test_plain_explicit_disagree_no_longer_warns_phase2():
    # P7 ships the signal, so the phase-1 "does not fire until vNEXT" warning is gone — an explicit
    # `disagree:` is now a live, binding gate.
    cfg = {
        "version": 1,
        "strategies": {
            "s": {
                "compare": ["docling", "aws-textract"],
                "then": "reducto",
                "escalate_when": {"disagree": True, "looks_bad": True},
                "max_cost": 1.0,
            }
        },
    }
    warns = _plain_warnings(cfg)
    assert not any("vNEXT" in w.message or "does not fire" in w.message for w in warns)


def test_disagreement_over_misplaced_is_an_error():
    # §8/§11: disagreement_over compares parallel branches — valid only on a pick:best parallel step.
    leaf_gate = {
        "version": 1,
        "strategies": {
            "s": {
                "steps": [
                    {"backend": "pymupdf", "escalate_if": {"disagreement_over": 0.3}},
                    "reducto",
                ]
            }
        },
    }
    assert _has(_errors(leaf_gate), "steps[0]", "pick: best")
    fastest = {
        "version": 1,
        "strategies": {
            "s": {
                "steps": [
                    {
                        "parallel": ["pymupdf", "docling"],
                        "pick": "fastest",
                        "escalate_if": {"disagreement_over": 0.3},
                    },
                    "reducto",
                ]
            }
        },
    }
    assert _has(_errors(fastest), "escalate_if", "pick: best")
    ok = {
        "version": 1,
        "strategies": {
            "s": {
                "budget": {"max_cost_usd": 1.0},
                "steps": [
                    {
                        "parallel": ["docling", "aws-textract"],
                        "pick": "best",
                        "escalate_if": {"disagreement_over": 0.3},
                    },
                    "reducto",
                ],
            }
        },
    }
    assert not [e for e in _errors(ok) if "disagreement" in e.message.lower()]


def test_plain_compare_default_gate_does_not_warn_about_disagree():
    # the IMPLICIT compare default gate carries disagreement_over but must NOT warn (§8).
    cfg = {
        "version": 1,
        "strategies": {
            "s": {"compare": ["docling", "aws-textract"], "then": "reducto", "max_cost": 1.0}
        },
    }
    assert not any("disagree" in w.message for w in _plain_warnings(cfg))


SECTION_4_FILE = {
    "version": 1,
    "strategies": {
        "quick": {"race": ["pymupdf", "docling"]},
        "fallback": ["reducto", "azure-document-intelligence", "docling"],
        "main": {
            "try": ["pymupdf", "docling", "reducto"],
            "escalate_when": "looks_bad",
            "max_cost": 0.5,
        },
        "invoices": {
            "try": ["reducto", "anthropic-claude"],
            "escalate_when": {"missing": ["invoice_number", "total_amount"], "low_confidence": 0.8},
        },
        "contracts": {"compare": ["docling", "aws-textract"], "then": "reducto", "max_cost": 0.6},
    },
}


def test_section_4_file_validates_with_zero_errors_and_warnings():
    issues = _plain_issues(SECTION_4_FILE)
    assert issues == [], [i.render("s") for i in issues]  # milestone acceptance: zero, zero


def test_dialect_badge_plain_and_advanced(_clean_cwd, capsys):
    f = _clean_cwd / "openreading.yaml"
    f.write_text(
        "version: 1\n"
        "strategies:\n"
        "  simple:\n    try: [pymupdf, reducto]\n    escalate_when: looks_bad\n"
        "  fancy:\n    parallel: [pymupdf, docling]\n    pick: best\n"
    )
    rc = main(["strategy", "validate", "--config", str(f)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "simple" in out and "dialect: plain" in out
    assert "fancy" in out and "dialect: advanced" in out
    assert "first advanced key: parallel" in out  # names the first non-Plain key


def test_plain_compare_then_local_produces_no_issues():
    # a plain compare + then over local backends validates with zero issues.
    cfg = {
        "version": 1,
        "strategies": {"s": {"compare": ["pymupdf", "docling"], "then": "tesseract"}},
    }
    assert _plain_issues(cfg) == []


# ---- unenforced guardrails (B4) ----------------------------------------------------------------
# `budget.max_attempts`, `defaults.advanced.circuit_breaker` and `defaults.advanced.attempt_timeout`
# are schema-accepted and read by no engine code (model.py §6.1, §8). Validating them green — and,
# for max_attempts, narrating them back as enforced — is a kill switch that reports itself armed.


def test_max_attempts_is_refused_as_unenforced():
    cfg = {
        "version": 1,
        "strategies": {"x": {"steps": ["pymupdf", "reducto"], "budget": {"max_attempts": 1}}},
    }
    errs = _errors(cfg)
    assert _has(errs, "budget.max_attempts", "not enforced")
    assert _has(errs, "budget.max_attempts", "max_duration")  # names the enforced alternative


def test_circuit_breaker_is_refused_as_unenforced():
    cfg = {
        "version": 1,
        "defaults": {"advanced": {"circuit_breaker": {"max_fails": 5, "cooldown": "30s"}}},
        "strategies": {"x": ["pymupdf"]},
    }
    assert _has(_errors(cfg), "defaults.advanced.circuit_breaker", "not enforced")


def test_attempt_timeout_is_refused_as_unenforced():
    cfg = {
        "version": 1,
        "defaults": {"advanced": {"attempt_timeout": "1s"}},
        "strategies": {"x": ["pymupdf"]},
    }
    assert _has(_errors(cfg), "defaults.advanced.attempt_timeout", "not enforced")


def test_nested_max_attempts_is_refused_at_its_own_path():
    cfg = {
        "version": 1,
        "strategies": {
            "x": {
                "steps": [
                    {"steps": ["pymupdf", "reducto"], "budget": {"max_attempts": 2}},
                    "tesseract",
                ],
                "budget": {"max_attempts": 5},
            }
        },
    }
    errs = _errors(cfg)
    paths = {i.path for i in errs if "max_attempts" in i.path}
    assert "strategies.x.budget.max_attempts" in paths
    assert "strategies.x.steps[0].budget.max_attempts" in paths


def test_max_duration_still_validates_clean():
    # the enforced budget key is untouched by the refusal
    cfg = {
        "version": 1,
        "strategies": {"x": {"steps": ["pymupdf", "reducto"], "budget": {"max_duration": "30s"}}},
    }
    assert _errors(cfg) == []


def test_cli_validate_refuses_unenforced_guardrails_and_never_affirms_them(_clean_cwd, capsys):
    f = _clean_cwd / "openreading.yaml"
    f.write_text(
        "version: 1\n"
        "defaults:\n"
        "  advanced:\n"
        "    circuit_breaker: {max_fails: 5, cooldown: 30s}\n"
        "    attempt_timeout: 1s\n"
        "strategies:\n"
        "  guarded:\n"
        "    steps: [tesseract, pymupdf]\n"
        "    budget: {max_attempts: 1}\n"
    )
    rc = main(["strategy", "validate", "--config", str(f)])
    cap = capsys.readouterr()
    assert rc == 3
    assert "not enforced" in cap.err
    # the summary must never narrate an unenforced ceiling as a real one
    assert "at most 1 attempt" not in cap.out


def test_cli_validate_with_no_config_anywhere_exits_3_not_0(_clean_cwd, capsys):
    """A CI job whose whole purpose is `openreading strategy validate` must not pass when there is
    nothing to validate. Reported as exiting 0 (a false green) and re-measured at 3 in every
    invocation form — auto-discovery, an explicit `--config` naming a missing file, with and
    without `--policy`. This pins that, since the finding would have been real if it were true."""
    for argv in (
        ["strategy", "validate"],
        ["strategy", "validate", "--config", str(_clean_cwd / "nope.yaml")],
    ):
        assert main(argv) == 3, argv
        assert capsys.readouterr().out == ""  # and never an "OK" on stdout
