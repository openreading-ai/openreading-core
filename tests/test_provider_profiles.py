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


def _allow_origin(monkeypatch) -> None:
    """A remote profile reads its key from the environment only once the operator lists its
    origin (`openreading.credentials`, the provider key binding)."""
    monkeypatch.setenv("OPENREADING_PROVIDER_ORIGINS", "https://llm.example.test")


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
    # urlsplit lowercases the scheme, so only the schema's lowercase pattern catches this one.
    pytest.param({**PROFILE, "base_url": "HTTPS://llm.example.test/v1"}, "base_url", id="upper"),
    # Python's `$` also matches before a final newline, so the whole string must be the URL.
    pytest.param({**PROFILE, "base_url": "https://llm.example.test/v1\n"}, "base_url", id="nl"),
    pytest.param({**PROFILE, "model": ""}, "model", id="empty-model"),
    pytest.param({**PROFILE, "key_env": "sk-live-abc123"}, "key_env", id="key-value"),
    pytest.param({**PROFILE, "key_env": "openrouter_key"}, "key_env", id="lowercase-var"),
    pytest.param({**PROFILE, "key_env": "OPENROUTER_API_KEY\n"}, "key_env", id="var-nl"),
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
    _allow_origin(monkeypatch)
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


@pytest.mark.parametrize("argv", [["backends"], ["keys", "list"], ["help"], ["benchmark", "list"]])
def test_a_broken_file_is_named_once_and_other_commands_go_on(yaml_file, capsys, argv):
    """A first-time user who forgot `version: 1` learns why the profile is missing."""
    assert main(argv) == 0
    clean = capsys.readouterr()
    yaml_file(YAML.replace("version: 1\n", ""))
    assert main(argv) == 0
    broken = capsys.readouterr()
    assert broken.out == clean.out  # stdout byte-identical, built-ins only
    assert "example-llm" not in broken.out
    lines = [ln for ln in broken.err.splitlines() if ln.startswith("[config]")]
    assert len(lines) == 1, broken.err
    assert "openreading.yaml" in lines[0] and "version" in lines[0]
    assert lines[0].endswith("Provider profiles from this file are not loaded.")


def test_a_broken_file_with_a_profile_backend_exits_3_not_2(yaml_file, pdf, capsys):
    yaml_file("version: [\n")
    assert main(["parse", pdf, "--backend", "example-llm"]) == 3
    err = capsys.readouterr().err
    assert "invalid YAML" in err and "invalid choice" not in err and "usage:" not in err


@pytest.mark.parametrize(
    ("argv", "head"),
    [
        (["route", "{pdf}"], "[route] "),
        (["parse", "{pdf}", "--backend", "pymupdf"], "[pymupdf] "),
        (["strategy", "validate"], "ERROR "),
        (["strategy", "list"], "[strategy list] "),
    ],
)
def test_commands_that_read_the_file_keep_their_own_exit_3(yaml_file, pdf, capsys, argv, head):
    yaml_file("version: [\n")
    assert main([a.format(pdf=pdf) for a in argv]) == 3
    err = capsys.readouterr().err
    assert err.startswith(head) and "[config]" not in err


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
    _allow_origin(monkeypatch)
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
    _allow_origin(monkeypatch)
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


def test_api_with_no_file_sees_no_profiles_and_leaves_the_process_set_alone(pdf, yaml_file):
    """Law P5 per call: a library call that finds no file sees no profile, even right after a
    call that found one, and never touches the set the CLI or server installed."""
    install_profiles({"example-llm": PROFILE})
    path = yaml_file()
    api.route(pdf, config=str(path))
    path.unlink()
    with pytest.raises(KeyError):
        api.run(pdf, backend="example-llm")
    assert "example-llm" in installed_profiles()
    assert "example-llm" in known_backends()  # outside a call, the process set answers


def test_api_dict_config_profiles_last_only_for_the_call(pdf):
    api.run(pdf, backend="pymupdf", config={"version": 1, "providers": {"example-llm": PROFILE}})
    assert installed_profiles() == {}
    assert "example-llm" not in known_backends()


@respx.mock
def test_concurrent_api_calls_each_reach_their_own_profile(pdf, monkeypatch):
    """Two threads, one profile name, two endpoints. A process-wide set let the second call's
    file replace the first's between loading it and dispatching."""
    import threading

    keyless = {k: v for k, v in PROFILE.items() if k != "key_env"}
    urls = {"a": "https://a.example.test/v1", "b": "https://b.example.test/v1"}
    for tag, url in urls.items():
        respx.post(f"{url}/chat/completions").mock(
            return_value=httpx.Response(200, json=_reply(f"# from {tag}"))
        )
    loaded_both = threading.Barrier(2, timeout=10)
    real_read = api._read_config_file

    def read_then_wait(cfg):
        loaded = real_read(cfg)
        loaded_both.wait()  # both files loaded before either call dispatches
        return loaded

    monkeypatch.setattr(api, "_read_config_file", read_then_wait)
    results: dict[str, object] = {}

    def call(tag: str) -> None:
        cfg = {"version": 1, "providers": {"shared": {**keyless, "base_url": urls[tag]}}}
        try:
            results[tag] = api.run(pdf, backend="shared", config=cfg)["document"]["markdown"]
        except Exception as exc:  # reported below, on the main thread
            results[tag] = exc

    threads = [threading.Thread(target=call, args=(t,)) for t in urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert str(results["a"]).startswith("# from a"), results
    assert str(results["b"]).startswith("# from b"), results


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


# --- the trust model: a committed file cannot borrow another backend's key -------------------


@pytest.mark.parametrize(
    ("var", "owner"),
    [
        ("ANTHROPIC_API_KEY", "anthropic-claude"),
        ("LLAMA_PARSE_API_KEY", "llamaparse"),
        ("NUMIND_API_KEY", "nuextract"),
        ("QWEN_VL_ENDPOINT", "qwen-vl"),
    ],
)
def test_key_env_owned_by_a_built_in_is_refused(var, owner):
    """A file pointing a built-in's key at its own host would exfiltrate that key."""
    bad = {**PROFILE, "base_url": "https://attacker.example.test/v1", "key_env": var}
    with pytest.raises(config.ConfigError) as ei:
        config.load({"version": 1, "providers": {"example-llm": bad}})
    message = str(ei.value)
    assert var in message and owner in message
    assert "EXAMPLE_LLM_API_KEY" in message  # a name of its own, suggested by profile name


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1:8000/v1",
        "http://127.8.9.10/v1",
        "http://[::1]:8000/v1",
        "https://llm.example.test/v1",
    ],
)
def test_a_keyed_profile_may_use_https_or_loopback_http(url):
    assert config.validate_providers({"example-llm": {**PROFILE, "base_url": url}})


@pytest.mark.parametrize(
    "url", ["http://llm.example.test/v1", "http://10.0.0.5/v1", "http://localhost.evil.test/v1"]
)
def test_a_keyed_profile_over_plain_http_to_another_host_is_refused(url):
    with pytest.raises(config.ConfigError) as ei:
        config.validate_providers({"example-llm": {**PROFILE, "base_url": url}})
    assert "https" in str(ei.value) and "base_url" in str(ei.value)


def test_a_keyless_profile_may_use_plain_http_anywhere():
    keyless = {k: v for k, v in PROFILE.items() if k != "key_env"}
    assert config.validate_providers(
        {"example-llm": {**keyless, "base_url": "http://gpu-box.lan:8000/v1"}}
    )
