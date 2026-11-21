"""Provider profiles: the `providers:` block of `openreading.yaml`, on every surface that reads it.

The grammar is proved twice, once as file text against the schema and once as a mapping built in
Python against `openreading.config.validate_providers` (law PF2), because the two paths share no
code. The rest proves that a profile is an ordinary backend id once installed: the CLI accepts
it, the router falls back past it, the server lists it, and a request still cannot point it at
another host. HTTP is respx throughout, so no socket opens.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import textwrap

import httpx
import pytest
import respx

from openreading import api, config
from openreading.adapters import registry
from openreading.adapters.registry import (
    BUILTIN_ADAPTERS,
    build_registry,
    install_profiles,
    installed_profiles,
    known_backends,
    make_adapter,
)
from openreading.cli.app import main
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.errors import TerminalError
from openreading.types.request import OpenReadingRequest

pytest.importorskip("pymupdf", reason="pymupdf (rasterizer) not installed")

BASE = "https://llm.example.test/v1"
CHAT = f"{BASE}/chat/completions"
PROFILE = {
    "kind": "openai-compatible",
    "base_url": BASE,
    "model": "vision-1",
    "key_env": "EXAMPLE_LLM_API_KEY",
}
YAML = textwrap.dedent(
    f"""\
    version: 1
    providers:
      example-llm:
        kind: openai-compatible
        base_url: {BASE}
        model: vision-1
        key_env: EXAMPLE_LLM_API_KEY
      local-llm:
        kind: openai-compatible
        base_url: http://localhost:11434/v1
        model: llava
    """
)


def _reply(content: str) -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "doc.pdf"
    p.write_bytes(build_sample_pdf())
    return str(p)


@pytest.fixture
def yaml_file(tmp_path):
    """An openreading.yaml in the working directory, which the autouse fixture made tmp_path."""

    def write(text: str = YAML):
        (tmp_path / "openreading.yaml").write_text(text)
        return tmp_path / "openreading.yaml"

    return write


# --- grammar: accepted ------------------------------------------------------------------------


def test_a_file_with_profiles_loads_and_exposes_the_block(yaml_file):
    loaded = config.load(str(yaml_file()))
    assert loaded is not None
    assert set(loaded.providers) == {"example-llm", "local-llm"}
    assert "key_env" not in loaded.providers["local-llm"]


def test_a_file_without_profiles_has_an_empty_block(yaml_file):
    loaded = config.load(str(yaml_file("version: 1\n")))
    assert loaded is not None and loaded.providers == {}


# --- grammar: refused, file text and Python mapping alike -------------------------------------

_BAD_PROFILES = [
    pytest.param({**PROFILE, "api_key": "sk-1"}, "api_key", id="unknown-key"),
    pytest.param({k: v for k, v in PROFILE.items() if k != "model"}, "model", id="no-model"),
    pytest.param({k: v for k, v in PROFILE.items() if k != "base_url"}, "base_url", id="no-url"),
    pytest.param({**PROFILE, "kind": "anthropic"}, "kind", id="wrong-kind"),
    pytest.param({**PROFILE, "base_url": "ftp://llm.example.test"}, "base_url", id="ftp"),
    pytest.param({**PROFILE, "base_url": "https://"}, "base_url", id="no-host"),
    pytest.param({**PROFILE, "model": ""}, "model", id="empty-model"),
    pytest.param({**PROFILE, "key_env": "sk-live-abc123"}, "key_env", id="key-value"),
    pytest.param({**PROFILE, "key_env": "openrouter_key"}, "key_env", id="lowercase-var"),
]


@pytest.mark.parametrize(("profile", "where"), _BAD_PROFILES)
def test_a_bad_profile_is_refused_from_a_file(yaml_file, profile, where):
    import yaml

    path = yaml_file(yaml.safe_dump({"version": 1, "providers": {"example-llm": profile}}))
    with pytest.raises(config.ConfigError) as ei:
        config.load(str(path))
    assert "providers" in str(ei.value)


@pytest.mark.parametrize(("profile", "where"), _BAD_PROFILES)
def test_a_bad_profile_is_refused_from_python(profile, where):
    with pytest.raises(config.ConfigError) as ei:
        config.validate_providers({"example-llm": profile})
    assert "providers/example-llm" in str(ei.value)
    assert where in str(ei.value)


@pytest.mark.parametrize(
    ("profile", "fragment"),
    [
        ({**PROFILE, "key_env": "OPENREADING_LEDGER"}, "OPENREADING_"),
        ({**PROFILE, "base_url": "https://user:sk-1@llm.example.test/v1"}, "user or password"),
        ({**PROFILE, "base_url": "https://llm.example.test:port/v1"}, "base_url"),
        ({**PROFILE, "base_url": 7}, "base_url"),
        ({**PROFILE, "key_env": 7}, "key_env"),
        ("not a mapping", "mapping"),
    ],
)
def test_checks_the_schema_cannot_express(profile, fragment):
    """These pass or bypass the schema, so `validate_providers` is the only thing refusing them."""
    with pytest.raises(config.ConfigError) as ei:
        config.load({"version": 1, "providers": {"example-llm": profile}})
    assert fragment in str(ei.value)


@pytest.mark.parametrize("name", ["OpenRouter", "strategy:cheap", "has space", 7])
def test_a_profile_name_must_be_a_backend_slug(name):
    with pytest.raises(config.ConfigError):
        config.validate_providers({name: PROFILE})


def test_the_block_itself_must_be_a_mapping():
    with pytest.raises(config.ConfigError):
        config.validate_providers(["example-llm"])
    assert config.validate_providers(None) == {}


# --- the shadow rule --------------------------------------------------------------------------


def test_a_profile_that_shadows_a_built_in_is_a_config_error(yaml_file):
    shadow = YAML.replace("example-llm:", "pymupdf:")
    with pytest.raises(config.ConfigError) as ei:
        config.load(str(yaml_file(shadow)))
    assert "'pymupdf' is a built-in backend id" in str(ei.value)
    with pytest.raises(config.ConfigError):
        config.load({"version": 1, "providers": {"pymupdf": PROFILE}})
    with pytest.raises(config.ConfigError):
        install_profiles({"pymupdf": PROFILE})
    assert make_adapter("pymupdf").descriptor.type.value == "oss_library"


def test_a_shadowing_file_exits_3_on_the_cli(yaml_file, pdf, capsys):
    yaml_file(YAML.replace("example-llm:", "tesseract:"))
    assert main(["parse", pdf, "--backend", "pymupdf"]) == 3
    assert "built-in backend id" in capsys.readouterr().err
    assert main(["route", pdf]) == 3


# --- the registry -----------------------------------------------------------------------------


def test_installed_profiles_join_every_catalog_read():
    assert known_backends().keys() == BUILTIN_ADAPTERS.keys()  # law P5: nothing installed
    install_profiles({"example-llm": PROFILE})
    assert list(known_backends())[-1] == "example-llm"
    assert make_adapter("example-llm").descriptor.id == "example-llm"
    assert build_registry().get("example-llm") is not None
    assert installed_profiles()["example-llm"].key_env == "EXAMPLE_LLM_API_KEY"
    install_profiles(None)
    with pytest.raises(KeyError):
        make_adapter("example-llm")


def test_a_bad_install_keeps_the_previous_set():
    install_profiles({"example-llm": PROFILE})
    with pytest.raises(config.ConfigError):
        install_profiles({"other": {**PROFILE, "kind": "nope"}})
    assert set(installed_profiles()) == {"example-llm"}


def test_profiles_move_the_ledger_registry_fingerprint():
    from openreading.ledger.header import registry_fingerprint

    before = registry_fingerprint()
    install_profiles({"example-llm": PROFILE})
    assert registry_fingerprint() != before


def test_loading_a_file_with_profiles_never_imports_the_strategy_engine(yaml_file):
    """Law P6 with a `providers:` block present, in a fresh process."""
    path = yaml_file()
    script = textwrap.dedent(
        f"""
        import sys
        from openreading import config
        loaded = config.load({str(path)!r})
        assert loaded.providers
        leaked = [m for m in sys.modules if m.startswith("openreading.strategies")]
        assert not leaked, leaked
        print("OK")
        """
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert out.returncode == 0 and "OK" in out.stdout, out.stderr


# --- surfaces ---------------------------------------------------------------------------------


@respx.mock
def test_cli_parse_accepts_a_profile_id(yaml_file, pdf, capsys, monkeypatch):
    monkeypatch.setenv("EXAMPLE_LLM_API_KEY", "sk-cli")
    yaml_file()
    respx.post(CHAT).mock(return_value=httpx.Response(200, json=_reply("# Hello")))
    assert main(["parse", pdf, "--backend", "example-llm"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["backend"]["id"] == "example-llm"
    assert out["document"]["markdown"].startswith("# Hello")


def test_cli_profile_is_unknown_without_the_file(pdf):
    with pytest.raises(SystemExit):
        main(["parse", pdf, "--backend", "example-llm"])


def test_cli_explicit_config_flag_installs_its_profiles(tmp_path, pdf, capsys):
    other = tmp_path / "elsewhere.yaml"
    other.write_text(YAML)
    assert main(["route", pdf, "--config", str(other)]) in (0, 4)
    assert "example-llm" in installed_profiles()
    installed = dict(installed_profiles())
    assert main(["route", pdf, f"--config={other}"]) in (0, 4)
    assert installed_profiles() == installed


def test_cli_backends_lists_profiles_with_their_key_var(yaml_file, capsys):
    yaml_file()
    assert main(["backends"]) == 0
    rows = {line.split()[0]: line for line in capsys.readouterr().out.splitlines()[1:]}
    assert "EXAMPLE_LLM_API_KEY" in rows["example-llm"]
    assert rows["example-llm"].split()[2] == "no"
    assert rows["local-llm"].split()[2] == "yes"


def test_cli_backends_without_a_readable_file_lists_built_ins_only(yaml_file, capsys):
    yaml_file("version: [")
    assert main(["backends"]) == 0
    assert "example-llm" not in capsys.readouterr().out


def test_cli_pre_scan_skips_mcp_and_serve(yaml_file):
    """`mcp` never loads ambient routing files and `serve` reads its own in `create_app`, so the
    CLI's pre-scan installs nothing for either."""
    from openreading.cli.app import _install_file_profiles

    yaml_file()
    _install_file_profiles(["mcp", "--profile", "x"])
    _install_file_profiles(["serve", "--port", "0"])
    _install_file_profiles([])
    assert installed_profiles() == {}
    _install_file_profiles(["backends"])
    assert "example-llm" in installed_profiles()


def test_cli_strategy_validate_knows_profile_ids(yaml_file, capsys):
    yaml_file(YAML + "strategies:\n  via-llm: [example-llm, pymupdf]\n")
    assert main(["strategy", "validate"]) == 0, capsys.readouterr().err


@respx.mock
def test_a_model_that_rejects_images_falls_back_to_the_next_backend(yaml_file, pdf, monkeypatch):
    monkeypatch.setenv("EXAMPLE_LLM_API_KEY", "sk-chain")
    yaml_file(YAML + "policy:\n  backends: [example-llm, pymupdf]\n")
    route = respx.post(CHAT).mock(
        return_value=httpx.Response(
            400, json={"error": {"message": "this model does not support image input"}}
        )
    )
    out = api.run(pdf)
    assert route.call_count == 1  # one attempt, then the chain moved on
    assert out["backend"]["id"] == "pymupdf"


@respx.mock
def test_a_rejected_key_names_key_env_and_never_the_key(yaml_file, pdf, monkeypatch):
    monkeypatch.setenv("EXAMPLE_LLM_API_KEY", "sk-secret-value")
    yaml_file()
    respx.post(CHAT).mock(
        return_value=httpx.Response(401, json={"error": {"message": "bad sk-secret-value"}})
    )
    with pytest.raises(TerminalError) as ei:
        api.run(pdf, backend="example-llm")
    assert ei.value.backend_code == "auth_rejected"
    assert "check EXAMPLE_LLM_API_KEY" in str(ei.value)
    assert "sk-secret-value" not in str(ei.value)


@respx.mock
def test_request_endpoint_is_refused_for_a_profile_backend(monkeypatch):
    monkeypatch.setenv("EXAMPLE_LLM_API_KEY", "sk-x")
    install_profiles({"example-llm": PROFILE})
    route = respx.post(url__regex=r".*").mock(return_value=httpx.Response(200, json=_reply("x")))
    req = OpenReadingRequest.model_validate(
        {
            "document": {
                "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
                "mime_type": "application/pdf",
            },
            "backend": {"id": "example-llm", "runtime": {"endpoint": "https://evil.test/v1"}},
        }
    )
    with pytest.raises(TerminalError) as ei:
        api.run_request(req)
    assert ei.value.backend_code == "endpoint_not_request_configurable"
    assert route.call_count == 0


def test_api_with_no_file_leaves_installed_profiles_alone(pdf):
    install_profiles({"example-llm": PROFILE})
    api.run(pdf, backend="pymupdf")
    assert "example-llm" in installed_profiles()


def test_api_dict_config_installs_its_profiles(pdf):
    api.run(pdf, backend="pymupdf", config={"version": 1, "providers": {"example-llm": PROFILE}})
    assert "example-llm" in installed_profiles()
    api.route(pdf, config={"version": 1})
    assert installed_profiles() == {}


def test_server_installs_profiles_from_its_config_and_lists_them(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from openreading.server import create_app

    path = tmp_path / "server.yaml"
    path.write_text(YAML)
    monkeypatch.setenv("OPENREADING_CONFIG", str(path))
    client = TestClient(create_app())
    rows = {r["slug"]: r for r in client.get("/v1/backends").json()}
    assert rows["example-llm"]["creds_missing"] == ["EXAMPLE_LLM_API_KEY"]
    assert rows["example-llm"]["ready"] is False
    assert rows["local-llm"]["ready"] is True
    body = {
        "document": {
            "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
            "mime_type": "application/pdf",
        },
        "backend": {"id": "local-llm", "runtime": {"endpoint": "https://evil.test/v1"}},
    }
    r = client.post("/v1/parse", json=body)
    assert r.status_code >= 400
    assert "endpoint" in r.text


def test_plain_strategy_resolves_a_profile_named_in_the_same_file():
    from openreading.strategies.loader import build_config

    loaded = config.load(
        {
            "version": 1,
            "providers": {"example-llm": PROFILE},
            "strategies": {"via-llm": {"try": ["example-llm", "pymupdf"]}},
        }
    )
    assert registry.installed_profiles() == {}  # desugar reads the file, not the process set
    built = build_config(loaded)
    assert built is not None and "via-llm" in built.config.strategies
