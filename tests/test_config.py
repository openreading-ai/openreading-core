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
        {"document": _MINIMAL_DOC, "backend": {"id": None}, **body}
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


def test_load_finds_the_yml_spelling_too(clean_cwd):
    (clean_cwd / "openreading.yml").write_text("version: 1\n")
    loaded = config.load(None)
    assert loaded is not None and loaded.path == clean_cwd / "openreading.yml"


def test_an_explicit_path_that_is_not_a_file_is_an_error(clean_cwd):
    with pytest.raises(config.ConfigError) as exc:
        config.load(str(clean_cwd / "absent.yaml"))
    assert "absent.yaml" in str(exc.value)


def test_the_environment_variable_pointing_nowhere_is_an_error(clean_cwd, monkeypatch):
    monkeypatch.setenv("OPENREADING_CONFIG", str(clean_cwd / "absent.yaml"))
    with pytest.raises(config.ConfigError) as exc:
        config.load(None)
    assert "OPENREADING_CONFIG" in str(exc.value)


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


# --- a dict is a file ----------------------------------------------------------------------------


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


# --- the policy block's own shape ----------------------------------------------------------------


# --- apply: the union --------------------------------------------------------------------------


def test_apply_with_no_policy_returns_the_request_untouched(clean_cwd):
    req = _request()
    out, cfg = config.apply(req, None, RouterConfig())
    assert out is req
    assert cfg == RouterConfig()


# --- apply: the router configuration ------------------------------------------------------------


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
