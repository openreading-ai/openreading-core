"""`openreading.config` — the one reader of `openreading.yaml`, pinned on every surface it serves.

Four things are proved here and nowhere else. Discovery order, so an explicit path beats the
environment variable and the environment variable beats the working directory. Equivalence of a
dict and a file, so a shape Python passes inline is refused exactly where a file is. The union,
so a request constraint survives a file that omits it (law P4: constraints only ever add).
Isolation, so reading the file never imports `openreading.strategies` (law P6).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from openreading import config
from openreading.router.compliance import RouterConfig
from openreading.types.request import OpenReadingRequest

_MINIMAL_DOC = {"bytes_base64": "", "mime_type": "application/pdf"}


def _request(**body) -> OpenReadingRequest:
    return OpenReadingRequest.model_validate(
        {"document": _MINIMAL_DOC, "backend": {"id": "auto"}, **body}
    )


@pytest.fixture
def clean_cwd(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENREADING_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


# --- discovery ---------------------------------------------------------------------------------


def test_load_returns_none_when_no_file_exists(clean_cwd):
    """Law P5: no file means no policy, which is the behaviour of every release before this one."""
    assert config.load(None) is None


def test_load_finds_the_working_directory_file(clean_cwd):
    (clean_cwd / "openreading.yaml").write_text("version: 1\npolicy: {require_local: true}\n")
    loaded = config.load(None)
    assert loaded is not None
    assert loaded.path == clean_cwd / "openreading.yaml"
    assert loaded.policy == {"require_local": True}


def test_load_finds_the_yml_spelling_too(clean_cwd):
    (clean_cwd / "openreading.yml").write_text("version: 1\n")
    loaded = config.load(None)
    assert loaded is not None and loaded.path == clean_cwd / "openreading.yml"


def test_environment_variable_beats_the_working_directory(clean_cwd, monkeypatch):
    (clean_cwd / "openreading.yaml").write_text("version: 1\npolicy: {require_local: true}\n")
    env_file = clean_cwd / "env.yaml"
    env_file.write_text("version: 1\npolicy: {require_baa: true}\n")
    monkeypatch.setenv("OPENREADING_CONFIG", str(env_file))
    loaded = config.load(None)
    assert loaded is not None and loaded.policy == {"require_baa": True}


def test_an_explicit_path_beats_the_environment_variable(clean_cwd, monkeypatch):
    env_file = clean_cwd / "env.yaml"
    env_file.write_text("version: 1\npolicy: {require_baa: true}\n")
    monkeypatch.setenv("OPENREADING_CONFIG", str(env_file))
    explicit = clean_cwd / "explicit.yaml"
    explicit.write_text("version: 1\npolicy: {data_region: eu}\n")
    loaded = config.load(str(explicit))
    assert loaded is not None and loaded.policy == {"data_region": "eu"}


def test_allow_cwd_false_ignores_the_working_directory_file(clean_cwd):
    """The server passes `allow_cwd=False` so a stray file cannot change a long-running process."""
    (clean_cwd / "openreading.yaml").write_text("version: 1\npolicy: {require_local: true}\n")
    assert config.load(None, allow_cwd=False) is None


def test_an_explicit_path_that_is_not_a_file_is_an_error(clean_cwd):
    with pytest.raises(config.ConfigError) as exc:
        config.load(str(clean_cwd / "absent.yaml"))
    assert "absent.yaml" in str(exc.value)


def test_the_environment_variable_pointing_nowhere_is_an_error(clean_cwd, monkeypatch):
    monkeypatch.setenv("OPENREADING_CONFIG", str(clean_cwd / "absent.yaml"))
    with pytest.raises(config.ConfigError) as exc:
        config.load(None)
    assert "OPENREADING_CONFIG" in str(exc.value)


def test_an_empty_environment_variable_falls_through_to_the_directory(clean_cwd, monkeypatch):
    monkeypatch.setenv("OPENREADING_CONFIG", "")
    (clean_cwd / "openreading.yaml").write_text("version: 1\npolicy: {require_local: true}\n")
    loaded = config.load(None)
    assert loaded is not None and loaded.policy == {"require_local": True}


# --- reading and refusing ------------------------------------------------------------------------


def test_a_malformed_file_raises_config_error_naming_the_path(clean_cwd):
    bad = clean_cwd / "openreading.yaml"
    bad.write_text("version: 1\nstrategies:\n  a: [unclosed\n")
    with pytest.raises(config.ConfigError) as exc:
        config.load(None)
    assert str(bad) in str(exc.value)


@pytest.mark.skipif(sys.platform == "win32", reason="chmod 000 is a POSIX permission model")
def test_a_file_that_cannot_be_opened_raises_config_error(clean_cwd):
    blocked = clean_cwd / "openreading.yaml"
    blocked.write_text("version: 1\n")
    blocked.chmod(0o000)
    try:
        with pytest.raises(config.ConfigError) as exc:
            config.load(None)
    finally:
        blocked.chmod(0o644)  # restore so the tmp_path teardown can remove it
    assert "cannot read config file" in str(exc.value)
    assert str(blocked) in str(exc.value)


def test_a_file_that_is_not_a_mapping_raises_config_error(clean_cwd):
    (clean_cwd / "openreading.yaml").write_text("- one\n- two\n")
    with pytest.raises(config.ConfigError) as exc:
        config.load(None)
    assert "mapping" in str(exc.value)


def test_an_empty_file_raises_config_error(clean_cwd):
    (clean_cwd / "openreading.yaml").write_text("")
    with pytest.raises(config.ConfigError):
        config.load(None)


def test_a_file_that_fails_the_schema_raises_config_error(clean_cwd):
    (clean_cwd / "openreading.yaml").write_text("version: 2\n")
    with pytest.raises(config.ConfigError) as exc:
        config.load(None)
    assert "version" in str(exc.value)


def test_a_file_with_no_policy_block_loads_with_no_policy(clean_cwd):
    (clean_cwd / "openreading.yaml").write_text("version: 1\nstrategies:\n  a: [pymupdf]\n")
    loaded = config.load(None)
    assert loaded is not None and loaded.policy is None


def test_the_source_hash_is_deterministic_and_named(clean_cwd):
    (clean_cwd / "openreading.yaml").write_text("version: 1\npolicy: {require_local: true}\n")
    first, second = config.load(None), config.load(None)
    assert first is not None and second is not None
    assert first.source_hash == second.source_hash
    assert first.source_hash.startswith("sha256:")


# --- a dict is a file ----------------------------------------------------------------------------


def test_a_dict_and_a_file_of_the_same_content_load_identically(clean_cwd):
    text = "version: 1\npolicy: {require_local: true}\nstrategies:\n  a: [pymupdf]\n"
    (clean_cwd / "openreading.yaml").write_text(text)
    from_file = config.load(None)
    from_dict = config.load(
        {"version": 1, "policy": {"require_local": True}, "strategies": {"a": ["pymupdf"]}}
    )
    assert from_file is not None and from_dict is not None
    assert from_file.raw == from_dict.raw
    assert from_file.policy == from_dict.policy


def test_a_dict_that_fails_the_schema_is_refused_and_labelled(clean_cwd):
    """A shape refused from a file is refused from Python, and the message says which it was."""
    with pytest.raises(config.ConfigError) as exc:
        config.load({"version": 2})
    assert "<dict>" in str(exc.value)


@pytest.mark.parametrize("value", [[], 3, True, ("a",)])
def test_a_config_that_is_neither_a_path_nor_a_mapping_is_refused(clean_cwd, value):
    with pytest.raises(config.ConfigError) as exc:
        config.load(value)
    assert "path or a mapping" in str(exc.value)


def test_a_dict_has_no_path_and_still_carries_a_hash(clean_cwd):
    loaded = config.load({"version": 1, "policy": {"require_baa": True}})
    assert loaded is not None
    assert loaded.path is None
    assert loaded.source_hash.startswith("sha256:")


def test_two_dicts_written_in_a_different_key_order_hash_alike(clean_cwd):
    a = config.load({"version": 1, "policy": {"require_baa": True, "require_local": True}})
    b = config.load({"policy": {"require_local": True, "require_baa": True}, "version": 1})
    assert a is not None and b is not None
    assert a.source_hash == b.source_hash


# --- the policy block's own shape ----------------------------------------------------------------


def test_an_unknown_policy_key_is_refused_by_name(clean_cwd):
    (clean_cwd / "openreading.yaml").write_text("version: 1\npolicy: {require_locall: true}\n")
    with pytest.raises(config.ConfigError) as exc:
        config.load(None)
    assert "require_locall" in str(exc.value)
    assert "openreading.yaml" in str(exc.value)


def test_a_quoted_boolean_cannot_flip_the_fail_closed_switch(clean_cwd):
    (clean_cwd / "openreading.yaml").write_text(
        'version: 1\npolicy: {allow_unverified_compliance: "false"}\n'
    )
    with pytest.raises(config.ConfigError) as exc:
        config.load(None)
    assert "policy/allow_unverified_compliance" in str(exc.value)


# --- apply: the union --------------------------------------------------------------------------


def test_apply_with_no_policy_returns_the_request_untouched(clean_cwd):
    req = _request()
    out, cfg = config.apply(req, None, RouterConfig())
    assert out is req
    assert cfg == RouterConfig()


def test_apply_adds_the_file_constraint_to_a_request_that_had_none(clean_cwd):
    out, _ = config.apply(_request(), {"require_local": True}, RouterConfig())
    assert out.compliance is not None and out.compliance.require_local is True


def test_apply_never_widens_a_request_constraint(clean_cwd):
    """Law P4. A file that omits `require_local` cannot switch off a request that asks for it."""
    req = _request(compliance={"require_local": True})
    out, _ = config.apply(req, {"require_baa": True}, RouterConfig())
    assert out.compliance is not None
    assert out.compliance.require_local is True
    assert out.compliance.require_baa is True


def test_apply_keeps_the_request_region_when_the_file_names_another(clean_cwd):
    req = _request(compliance={"data_region": "us"})
    out, _ = config.apply(req, {"data_region": "eu"}, RouterConfig())
    assert out.compliance is not None and out.compliance.data_region == "us"


def test_apply_adds_the_file_region_when_the_request_names_none(clean_cwd):
    out, _ = config.apply(_request(), {"data_region": "eu"}, RouterConfig())
    assert out.compliance is not None and out.compliance.data_region == "eu"


def test_an_attestation_only_policy_leaves_the_request_compliance_alone(clean_cwd):
    """Three keys widen rather than constrain. A block holding only those adds no compliance
    object, so the request the response echoes is the one the caller sent."""
    req = _request()
    out, cfg = config.apply(req, {"train_optout_confirmed": ["aws-textract"]}, RouterConfig())
    assert out.compliance is None
    assert cfg.train_optout_confirmed == frozenset({"aws-textract"})


def test_apply_is_idempotent(clean_cwd):
    policy = {
        "require_local": True,
        "data_region": "eu",
        "optimize_for": "cost",
        "allow_unverified_compliance": True,
        "train_optout_confirmed": ["aws-textract"],
    }
    once, cfg_once = config.apply(_request(), policy, RouterConfig())
    twice, cfg_twice = config.apply(once, policy, cfg_once)
    assert once.model_dump() == twice.model_dump()
    assert cfg_once == cfg_twice


def test_apply_folds_the_routing_preference(clean_cwd):
    out, _ = config.apply(_request(), {"optimize_for": "cost"}, RouterConfig())
    assert out.routing is not None and out.routing.optimize_for == "cost"


def test_the_request_routing_preference_wins_over_the_file(clean_cwd):
    req = _request(routing={"optimize_for": "latency"})
    out, _ = config.apply(req, {"optimize_for": "cost"}, RouterConfig())
    assert out.routing is not None and out.routing.optimize_for == "latency"


def test_apply_keeps_a_request_fallback_chain_the_file_says_nothing_about(clean_cwd):
    req = _request(routing={"fallback": ["pymupdf"]})
    out, _ = config.apply(req, {"optimize_for": "cost"}, RouterConfig())
    assert out.routing is not None
    assert out.routing.fallback == ["pymupdf"]
    assert out.routing.optimize_for == "cost"


# --- apply: the router configuration ------------------------------------------------------------


def test_apply_folds_the_three_attestation_keys(clean_cwd):
    policy = {
        "allow_unverified_compliance": True,
        "train_optout_confirmed": ["aws-textract"],
        "baa_tier_confirmed": ["reducto"],
    }
    _, cfg = config.apply(_request(), policy, RouterConfig())
    assert cfg.allow_unverified_compliance is True
    assert cfg.train_optout_confirmed == frozenset({"aws-textract"})
    assert cfg.baa_tier_confirmed == frozenset({"reducto"})


def test_apply_unions_attestations_with_the_base_rather_than_replacing_them(clean_cwd):
    base = RouterConfig(train_optout_confirmed=frozenset({"chunkr"}))
    _, cfg = config.apply(_request(), {"train_optout_confirmed": ["aws-textract"]}, base)
    assert cfg.train_optout_confirmed == frozenset({"chunkr", "aws-textract"})


def test_router_config_builds_the_three_keys_from_a_policy(clean_cwd):
    cfg = config.router_config({"baa_tier_confirmed": ["reducto"]})
    assert cfg.baa_tier_confirmed == frozenset({"reducto"})
    assert cfg.allow_unverified_compliance is False


# --- law P6: reading the file never imports the engine -------------------------------------------


def test_importing_config_does_not_import_the_strategy_package():
    """Law P6, checked in a fresh subprocess: this process' sys.modules carries the whole suite."""
    script = textwrap.dedent("""
        import sys
        import openreading.config  # noqa: F401
        leaked = [m for m in sys.modules if m.startswith("openreading.strategies")]
        assert not leaked, f"openreading.config imported: {leaked}"
        print("OK")
    """)
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert out.returncode == 0 and "OK" in out.stdout, out.stderr
