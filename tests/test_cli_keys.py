"""`openreading keys set|list|unset|import`, the SOURCE column in `openreading backends`, and the
keychain opt-in each CLI verb makes (`openreading.cli.keys`, `openreading.cli.app.main`).

Every test uses the in-memory fake from `tests/conftest.py` (`fake_keychain`) or the suite's
default `OPENREADING_KEYCHAIN=off`. No test reaches a real keychain or the network.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from openreading import credentials, keychain
from openreading.adapters.registry import make_adapter
from openreading.cli import app as cli
from openreading.cli import keys as cli_keys
from openreading.credentials import EnvCredentialBroker
from openreading.types.errors import TerminalError
from openreading.types.request import OpenReadingRequest

KEY = "sk-ant-keychain-canary-0002"


def _saved(fake, name: str) -> str | None:
    return fake.store.get(("openreading", name))


def _stdin(monkeypatch, text: str, *, tty: bool = False) -> None:
    stream = io.StringIO(text)
    stream.isatty = lambda: tty  # type: ignore[method-assign]
    monkeypatch.setattr(sys, "stdin", stream)


def _run(argv: list[str], capsys) -> tuple[int, str, str]:
    code = cli.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


# --- keys set ------------------------------------------------------------------------------------


def test_set_from_stdin_saves_the_declared_secret_and_never_prints_it(
    monkeypatch, capsys, fake_keychain
):
    _stdin(monkeypatch, KEY + "\n")
    code, out, err = _run(["keys", "set", "anthropic-claude", "--stdin"], capsys)
    assert code == 0, err
    assert _saved(fake_keychain, "ANTHROPIC_API_KEY") == KEY
    assert "Saved ANTHROPIC_API_KEY" in out
    assert KEY not in out + err


def test_set_interactive_uses_hidden_input_and_offers_no_probe_by_default(
    monkeypatch, capsys, fake_keychain
):
    _stdin(monkeypatch, "", tty=True)
    prompts: list[str] = []
    answers = iter([KEY, ""])
    monkeypatch.setattr(cli_keys.getpass, "getpass", lambda p: prompts.append(p) or next(answers))
    monkeypatch.setattr("builtins.input", lambda _p: "")
    code, out, _ = _run(["keys", "set", "reducto"], capsys)
    assert code == 0
    assert prompts[0].startswith("REDUCTO_API_KEY")
    assert _saved(fake_keychain, "REDUCTO_API_KEY") == KEY
    # The optional webhook secret was left blank, so nothing is saved for it.
    assert _saved(fake_keychain, "REDUCTO_WEBHOOK_SECRET") is None
    assert "Skipped REDUCTO_WEBHOOK_SECRET" in out
    assert "Get a key: https://" in out
    assert KEY not in out


def test_set_interactive_probe_runs_only_when_you_say_yes(monkeypatch, capsys, fake_keychain):
    from openreading.types.liveness import LivenessStatus

    _stdin(monkeypatch, "", tty=True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "shell-value")
    monkeypatch.setattr(cli_keys.getpass, "getpass", lambda _p: KEY)
    monkeypatch.setattr("builtins.input", lambda _p: "y")
    seen = {}

    class _Report:
        status = LivenessStatus.LIVE
        latency_ms = 412.0
        detail = "responding"

    def probe(adapter, *, broker, timeout_s=None):
        seen["key"] = broker.resolve(
            adapter.descriptor,
            OpenReadingRequest.model_validate(
                {"document": {"path": "/x"}, "backend": {"id": adapter.descriptor.id}}
            ),
        ).values.get("api_key")
        return _Report()

    monkeypatch.setattr(cli_keys, "check_liveness", probe)
    code, out, _ = _run(["keys", "set", "anthropic-claude"], capsys)
    assert code == 0
    assert seen["key"] == KEY
    assert _anthropic_key() == "shell-value"
    assert "anthropic-claude is working (412 ms)" in out

    _Report.status = LivenessStatus.UNAUTHORIZED
    _Report.detail = "key was found but rejected by anthropic-claude: check ANTHROPIC_API_KEY"
    code, out, _ = _run(["keys", "set", "anthropic-claude"], capsys)
    assert code == 0
    assert "✗ anthropic-claude" in out and "check ANTHROPIC_API_KEY" in out


def test_probe_reads_the_selected_saved_alias(monkeypatch, fake_keychain):
    from openreading.liveness import probe_request

    slug = "llamaparse-fast"
    monkeypatch.setenv("LLAMA_CLOUD_API_KEY", "shell")
    fake_keychain.store[("openreading", "LLAMA_PARSE_API_KEY")] = KEY
    credentials.use_keychain(True)
    seen = []

    def probe(adapter, *, broker):
        seen.append(broker.resolve(adapter.descriptor, probe_request(slug)).values["api_key"])

    monkeypatch.setattr(cli_keys, "check_liveness", probe)
    cli_keys.run_probe(slug, ["LLAMA_PARSE_API_KEY"])
    assert seen == [KEY]
    assert EnvCredentialBroker().resolve(
        make_adapter(slug).descriptor, probe_request(slug)
    ).values == {"api_key": "shell"}


def test_set_stdin_never_offers_a_probe(monkeypatch, capsys, fake_keychain):
    _stdin(monkeypatch, KEY + "\n", tty=True)
    monkeypatch.setattr("builtins.input", lambda _p: pytest.fail("prompted in --stdin mode"))
    assert _run(["keys", "set", "anthropic-claude", "--stdin"], capsys)[0] == 0


def test_set_multi_field_stdin_is_one_line_per_secret_in_order(monkeypatch, capsys, fake_keychain):
    _stdin(monkeypatch, "AKIA_ID\nSECRET_VALUE\n")
    code, out, _ = _run(["keys", "set", "aws-textract", "--stdin"], capsys)
    assert code == 0
    assert _saved(fake_keychain, "AWS_ACCESS_KEY_ID") == "AKIA_ID"
    assert _saved(fake_keychain, "AWS_SECRET_ACCESS_KEY") == "SECRET_VALUE"
    assert _saved(fake_keychain, "AWS_SESSION_TOKEN") is None
    assert "Skipped AWS_SESSION_TOKEN" in out


def test_set_one_named_variable_including_an_alias(monkeypatch, capsys, fake_keychain):
    _stdin(monkeypatch, "secret\n")
    code, _, _ = _run(["keys", "set", "aws-textract", "AWS_SECRET_ACCESS_KEY", "--stdin"], capsys)
    assert code == 0 and _saved(fake_keychain, "AWS_SECRET_ACCESS_KEY") == "secret"
    _stdin(monkeypatch, "lp\n")
    code, _, _ = _run(["keys", "set", "llamaparse-fast", "LLAMA_PARSE_API_KEY", "--stdin"], capsys)
    assert code == 0 and _saved(fake_keychain, "LLAMA_PARSE_API_KEY") == "lp"


def test_set_stdin_with_too_many_lines_writes_nothing(monkeypatch, capsys, fake_keychain):
    _stdin(monkeypatch, "one\ntwo\n")
    code, _, err = _run(["keys", "set", "anthropic-claude", "--stdin"], capsys)
    assert code == 2 and "one line per secret" in err
    assert fake_keychain.store == {}


def test_set_stdin_with_no_value_is_usage(monkeypatch, capsys, fake_keychain):
    _stdin(monkeypatch, "")
    code, _, err = _run(["keys", "set", "anthropic-claude", "--stdin"], capsys)
    assert code == 2 and "no value" in err


def test_set_without_a_terminal_and_without_stdin_flag_is_usage(monkeypatch, capsys, fake_keychain):
    _stdin(monkeypatch, KEY + "\n", tty=False)
    code, _, err = _run(["keys", "set", "anthropic-claude"], capsys)
    assert code == 2 and "--stdin" in err
    assert fake_keychain.store == {}


def test_set_interrupted_reports_what_was_saved_and_exits_130(monkeypatch, capsys, fake_keychain):
    _stdin(monkeypatch, "", tty=True)
    answers = iter(["AKIA_ID"])

    def prompt(_p):
        try:
            return next(answers)
        except StopIteration:
            raise KeyboardInterrupt from None

    monkeypatch.setattr(cli_keys.getpass, "getpass", prompt)
    code, _, err = _run(["keys", "set", "aws-textract"], capsys)
    assert code == 130
    assert "AWS_ACCESS_KEY_ID" in err and "Nothing else was changed" in err


def test_set_interactive_with_everything_blank_saves_nothing(monkeypatch, capsys, fake_keychain):
    _stdin(monkeypatch, "", tty=True)
    monkeypatch.setattr(cli_keys.getpass, "getpass", lambda _p: "")
    code, out, _ = _run(["keys", "set", "anthropic-claude"], capsys)
    assert code == 0 and "Nothing was saved" in out and fake_keychain.store == {}


def test_set_warns_when_the_environment_will_shadow_the_saved_key(
    monkeypatch, capsys, fake_keychain
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "shell-value-123")
    _stdin(monkeypatch, KEY + "\n")
    code, out, _ = _run(["keys", "set", "anthropic-claude", "--stdin"], capsys)
    assert code == 0
    assert "takes priority" in out and "shell-value-123" not in out


# --- PS4 / PS5: what keys set refuses -----------------------------------------------------------


def test_ps4_set_has_no_value_flag():
    import argparse

    parser = cli.build_parser()
    set_parser = parser._subparsers._group_actions[0].choices["keys"]  # type: ignore[union-attr]
    keys_sub = next(
        a for a in set_parser._actions if a.__class__.__name__ == "_SubParsersAction"
    ).choices["set"]
    flags = {
        opt
        for action in keys_sub._actions
        if action.help != argparse.SUPPRESS
        for opt in action.option_strings
    }
    assert flags == {"-h", "--help", "--stdin"}
    assert "--value" not in keys_sub.format_help()


@pytest.mark.parametrize(
    "argv",
    [
        ["keys", "set", "anthropic-claude", "--value", KEY],
        ["keys", "set", "anthropic-claude", f"--value={KEY}"],
        ["keys", "set", "nosuch", "--value", KEY],
        ["keys", "set", "anthropic-claude", "--value"],
    ],
)
def test_ps4_a_value_on_the_command_line_is_refused_with_the_reason(
    monkeypatch, capsys, fake_keychain, argv
):
    _stdin(monkeypatch, "")
    code, out, err = _run(argv, capsys)
    assert code == 2
    assert "ps" in err and "shell history" in err and "--stdin" in err and "hidden" in err
    assert "usage:" not in err
    assert KEY not in out + err
    assert fake_keychain.store == {}


@pytest.mark.parametrize(
    ("var", "phrase"),
    [("ANTHROPIC_MODEL", "a setting, not a secret"), ("OPENREADING_LEDGER", "not a secret")],
)
def test_ps5_set_refuses_settings_and_undeclared_variables(
    monkeypatch, capsys, fake_keychain, var, phrase
):
    _stdin(monkeypatch, "value\n")
    code, _, err = _run(["keys", "set", "anthropic-claude", var, "--stdin"], capsys)
    assert code == 2 and phrase in err and "ANTHROPIC_API_KEY" in err
    assert fake_keychain.store == {}


SECRET_ARG = "sk-live-SECRET123"


@pytest.mark.parametrize(
    "argv",
    [
        ["keys", "set", "reducto", SECRET_ARG, "--stdin"],
        ["keys", "unset", "reducto", SECRET_ARG],
        ["keys", "set", SECRET_ARG, "--stdin"],
        ["keys", "unset", SECRET_ARG],
        ["keys", "import", SECRET_ARG],
        # argparse's own usage errors echo the offending token, so these never reach keys.py.
        ["keys", "set", "reducto", "--token", SECRET_ARG],
        ["keys", "set", "reducto", f"--token={SECRET_ARG}"],
        ["keys", SECRET_ARG],
        ["keys", "list", SECRET_ARG],
        # The --value refusal must not quote a slug it never validated.
        ["keys", "set", SECRET_ARG, "--value"],
        ["keys", "set", SECRET_ARG, "--value", "x"],
        # The startup scan reads `--config` for every verb, so its file report must not quote it.
        ["keys", "set", "anthropic-claude", "--config", SECRET_ARG],
        ["keys", "list", f"--config={SECRET_ARG}"],
        # A token before the verb must not become the command argparse names as invalid.
        ["--key", SECRET_ARG, "keys", "list"],
        ["-x", SECRET_ARG, "keys", "set", "anthropic-claude"],
    ],
)
def test_ps4_an_argument_that_looks_like_a_key_is_never_echoed(
    monkeypatch, capsys, fake_keychain, argv
):
    """A key pasted where a variable, slug or path belongs is refused without printing it."""
    _stdin(monkeypatch, "value\n")
    code, out, err = _run(argv, capsys)
    assert code in (2, 3)
    assert SECRET_ARG not in out + err
    assert fake_keychain.store == {}


def test_an_undeclared_variable_names_the_declared_ones(monkeypatch, capsys, fake_keychain):
    _stdin(monkeypatch, "value\n")
    code, _, err = _run(["keys", "set", "reducto", SECRET_ARG, "--stdin"], capsys)
    assert code == 2
    assert "not a secret variable reducto declares" in err
    assert "REDUCTO_API_KEY" in err and "REDUCTO_WEBHOOK_SECRET" in err


def test_set_refuses_an_unknown_slug_and_a_backend_with_no_secret(capsys, fake_keychain):
    code, _, err = _run(["keys", "set", "nope", "--stdin"], capsys)
    assert code == 2 and "unknown backend" in err
    known = err.split("known:", 1)[1]
    assert "anthropic-claude" in known
    assert "pymupdf" not in known and "tesseract" not in known  # they declare no secret
    code, _, err = _run(["keys", "set", "pymupdf", "--stdin"], capsys)
    assert code == 2 and "declares no secret" in err


# --- PS6 and the switch: no keychain means exit 2 and nothing written ---------------------------


def test_ps6_set_with_a_fail_backend_exits_2_and_writes_nothing(monkeypatch, capsys, fake_keychain):
    import keyring.backends.fail
    import keyring.core

    monkeypatch.setattr(keyring.core, "_keyring_backend", keyring.backends.fail.Keyring())
    _stdin(monkeypatch, KEY + "\n")
    code, out, err = _run(["keys", "set", "anthropic-claude", "--stdin"], capsys)
    assert code == 2
    assert (
        "No keychain is available on this machine. Use an environment variable or .env instead."
        in err
    )
    # Reading stays silent: list prints every source without a keychain row and no warning.
    code, out, err = _run(["keys", "list"], capsys)
    assert code == 0 and err == "" and "keychain" not in out


def test_ps6_parse_runs_silently_without_a_keychain(monkeypatch, capsys, fake_keychain, tmp_path):
    import keyring.backends.fail
    import keyring.core

    from openreading.testing.sample_pdf import build_sample_pdf

    monkeypatch.setattr(keyring.core, "_keyring_backend", keyring.backends.fail.Keyring())
    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(build_sample_pdf())
    code, out, err = _run(["parse", str(pdf), "--backend", "pymupdf"], capsys)
    assert code == 0 and json.loads(out)["status"]["state"] == "succeeded"
    assert "keychain" not in err.lower()


def test_keys_writes_are_refused_when_the_switch_is_off(monkeypatch, capsys):
    _stdin(monkeypatch, KEY + "\n")
    code, _, err = _run(["keys", "set", "anthropic-claude", "--stdin"], capsys)
    assert code == 2 and "OPENREADING_KEYCHAIN=off" in err
    code, _, err = _run(["keys", "unset", "anthropic-claude"], capsys)
    assert code == 2 and "OPENREADING_KEYCHAIN=off" in err


# --- keys list -----------------------------------------------------------------------------------


def test_list_names_backend_var_and_source_never_a_value(
    monkeypatch, capsys, fake_keychain, tmp_path
):
    fake_keychain.store[("openreading", "ANTHROPIC_API_KEY")] = KEY
    monkeypatch.setenv("PULSE_API_KEY", "exported-value")
    (tmp_path / ".env").write_text("REDUCTO_API_KEY=dotenv-value\n", encoding="utf-8")
    code, out, _ = _run(["keys", "list"], capsys)
    assert code == 0
    lines = out.splitlines()
    assert lines[0].split() == ["BACKEND", "VAR", "SOURCE"]
    rows = {tuple(line.split()) for line in lines[1:]}
    assert ("anthropic-claude", "ANTHROPIC_API_KEY", "keychain") in rows
    assert ("reducto", "REDUCTO_API_KEY", ".env") in rows
    assert ("pulse", "PULSE_API_KEY", "env") in rows
    assert ("mistral-ocr", "MISTRAL_API_KEY", "missing") in rows
    assert not any(r[0] == "pymupdf" for r in rows)
    for secret in (KEY, "exported-value", "dotenv-value"):
        assert secret not in out


def test_ps4_list_json_carries_no_value(capsys, fake_keychain):
    fake_keychain.store[("openreading", "ANTHROPIC_API_KEY")] = KEY
    code, out, _ = _run(["keys", "list", "--json"], capsys)
    assert code == 0
    rows = json.loads(out)
    assert {"backend": "anthropic-claude", "var": "ANTHROPIC_API_KEY", "source": "keychain"} in rows
    assert all(set(r) == {"backend", "var", "source"} for r in rows)
    assert KEY not in out


# --- keys unset ----------------------------------------------------------------------------------


def test_unset_removes_the_backends_secrets_and_names_sharers(capsys, fake_keychain):
    fake_keychain.store[("openreading", "LLAMA_CLOUD_API_KEY")] = "lc"
    code, out, _ = _run(["keys", "unset", "llamaparse-fast"], capsys)
    assert code == 0
    assert _saved(fake_keychain, "LLAMA_CLOUD_API_KEY") is None
    assert "Removed LLAMA_CLOUD_API_KEY" in out and "llamaparse-agentic" in out
    code, out, _ = _run(["keys", "unset", "llamaparse-fast"], capsys)
    assert code == 0 and "Nothing saved" in out


def test_unset_refuses_an_undeclared_variable(capsys, fake_keychain):
    code, _, err = _run(["keys", "unset", "anthropic-claude", "OPENREADING_LEDGER"], capsys)
    assert code == 2 and "not a secret" in err


# --- keys import ---------------------------------------------------------------------------------


def test_unset_reports_a_store_that_refuses_reads(monkeypatch, capsys, fake_keychain):
    def locked(*args):
        raise RuntimeError("private store message")

    monkeypatch.setattr(fake_keychain, "get_password", locked)
    code, out, err = _run(["keys", "unset", "anthropic-claude"], capsys)
    assert code == 2
    assert "ANTHROPIC_API_KEY" in err and "private store message" not in out + err


@pytest.mark.parametrize("second, expected_code", [(KEY, 0), ("another-value", 2)])
def test_import_only_recommends_removing_copied_values(
    tmp_path, capsys, fake_keychain, second, expected_code
):
    src = tmp_path / "old.env"
    src.write_text(f"REDUCTO_API_KEY=earlier\nMISTRAL_API_KEY={KEY}\nMISTRAL_API_KEY={second}\n")
    code, out, err = _run(["keys", "import", str(src)], capsys)
    assert code == expected_code
    assert KEY not in out + err and "another-value" not in out + err
    if code:
        assert "MISTRAL_API_KEY" in err and "2" in err and "3" in err
        assert not fake_keychain.store and "Delete" not in out
    else:
        assert _saved(fake_keychain, "MISTRAL_API_KEY") == KEY and "lines 1, 2, 3" in out


def test_import_moves_declared_secrets_and_names_the_lines_to_delete(
    capsys, fake_keychain, tmp_path
):
    src = tmp_path / "old.env"
    src.write_text(
        "# keys\nREDUCTO_API_KEY=r-secret\nANTHROPIC_MODEL=claude-x\nOPENREADING_LEDGER=/l\n"
        "PULSE_API_KEY=\nexport MISTRAL_API_KEY='m-secret'\n",
        encoding="utf-8",
    )
    code, out, err = _run(["keys", "import", str(src)], capsys)
    assert code == 0, err
    assert _saved(fake_keychain, "REDUCTO_API_KEY") == "r-secret"
    assert _saved(fake_keychain, "MISTRAL_API_KEY") == "m-secret"
    assert _saved(fake_keychain, "ANTHROPIC_MODEL") is None
    assert _saved(fake_keychain, "OPENREADING_LEDGER") is None
    assert _saved(fake_keychain, "PULSE_API_KEY") is None
    assert "lines 2, 6" in out
    assert "ANTHROPIC_MODEL" in out
    for secret in ("r-secret", "m-secret"):
        assert secret not in out + err
    # The file itself is never edited.
    assert "REDUCTO_API_KEY=r-secret" in src.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("filename", "approved"), [("old.env", False), (".env", False), ("old.env", True)]
)
def test_import_names_the_endpoint_to_export_before_removing_the_key(
    capsys, fake_keychain, tmp_path, monkeypatch, filename, approved
):
    src = tmp_path / filename
    setting = "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT"
    secret = "AZURE_DOCUMENT_INTELLIGENCE_KEY"
    origin = "https://custom.example.test"
    src.write_text(f"{secret}={KEY}\n{setting}={origin}\nOPENREADING_PROVIDER_ORIGINS={origin}\n")
    if approved:
        monkeypatch.setenv("OPENREADING_PROVIDER_ORIGINS", origin)
    code, out, err = _run(["keys", "import", str(src), "azure-document-intelligence"], capsys)
    assert code == 0 and _saved(fake_keychain, secret) == KEY
    if approved:
        assert "Delete line 1" in out and f"Export {setting}" not in out
    else:
        assert f"Export {setting}" in out and "line 2" in out
        assert "Delete line 1" not in out
    assert KEY not in out + err and origin not in out + err


BARE_KEY = "c2stbGl2ZS1TRUNSRVQtYmFzZTY0"


def test_import_lists_only_variable_names_it_leaves_in_place(capsys, fake_keychain, tmp_path):
    """A bare base64 key line ending in `==` parses with the key as its name, so printing every
    name left in place printed the key."""
    src = tmp_path / "old.env"
    src.write_text(
        f"REDUCTO_API_KEY=r-secret\n{BARE_KEY}==\nANTHROPIC_MODEL=x\nlower_name=1\n",
        encoding="utf-8",
    )
    code, out, err = _run(["keys", "import", str(src)], capsys)
    assert code == 0, err
    assert BARE_KEY not in out + err and "lower_name" not in out
    assert "Left in place, not a backend secret: ANTHROPIC_MODEL, and 2 other lines" in out


def test_import_never_prints_an_uppercase_bare_key(capsys, fake_keychain, tmp_path):
    """An AWS access key id is uppercase letters and digits, so it passes any name pattern. Only
    a variable some backend declares is named, and every other line is counted."""
    bare = "AKIAIOSFODNN7EXAMPLE"
    src = tmp_path / "old.env"
    src.write_text(f"REDUCTO_API_KEY=r-secret\n{bare}==\nMY_OWN_SETTING=1\n", encoding="utf-8")
    code, out, err = _run(["keys", "import", str(src)], capsys)
    assert code == 0, err
    assert bare not in out + err and "MY_OWN_SETTING" not in out
    assert "Left in place, not a backend secret: 2 other lines" in out


def test_import_with_a_slug_never_calls_another_backends_secret_not_a_secret(
    capsys, fake_keychain, tmp_path
):
    src = tmp_path / "old.env"
    src.write_text("REDUCTO_API_KEY=r-secret\nANTHROPIC_API_KEY=a-secret\n", encoding="utf-8")
    code, out, err = _run(["keys", "import", str(src), "reducto"], capsys)
    assert code == 0, err
    assert "not a backend secret" not in out
    assert "Left in place, not a secret reducto declares: ANTHROPIC_API_KEY" in out
    assert "a-secret" not in out + err


def test_import_with_nothing_to_move_and_an_unreadable_file(capsys, fake_keychain, tmp_path):
    src = tmp_path / "plain.env"
    src.write_text("ANTHROPIC_MODEL=x\n", encoding="utf-8")
    code, out, _ = _run(["keys", "import", str(src)], capsys)
    assert code == 0 and "No declared secret" in out
    code, _, err = _run(["keys", "import", str(tmp_path / "missing.env")], capsys)
    assert code == 3 and "cannot read" in err


def test_import_without_a_keychain_exits_2(monkeypatch, capsys, tmp_path):
    src = tmp_path / "k.env"
    src.write_text("REDUCTO_API_KEY=r\n", encoding="utf-8")
    code, _, err = _run(["keys", "import", str(src)], capsys)
    assert code == 2 and "OPENREADING_KEYCHAIN=off" in err


# --- backends: the SOURCE column ----------------------------------------------------------------


def test_ps1_backends_output_is_identical_with_an_empty_keychain(
    monkeypatch, capsys, fake_keychain
):
    monkeypatch.setenv("REDUCTO_API_KEY", "r")
    code, with_empty, _ = _run(["backends"], capsys)
    assert code == 0
    monkeypatch.setenv("OPENREADING_KEYCHAIN", "off")
    keychain._reset()
    code, with_off, _ = _run(["backends"], capsys)
    assert with_empty == with_off
    assert "SOURCE" not in with_off


def test_backends_shows_a_source_column_once_a_key_comes_from_the_keychain(
    monkeypatch, capsys, fake_keychain, tmp_path
):
    fake_keychain.store[("openreading", "ANTHROPIC_API_KEY")] = KEY
    monkeypatch.setenv("PULSE_API_KEY", "p")
    (tmp_path / ".env").write_text("REDUCTO_API_KEY=r\n", encoding="utf-8")
    code, out, _ = _run(["backends"], capsys)
    assert code == 0
    header, *rows = out.splitlines()
    assert header.split() == ["BACKEND", "TYPE", "CONFIGURED", "SOURCE", "MISSING"]
    by_slug = {r.split()[0]: r.split() for r in rows}
    assert by_slug["anthropic-claude"][2:4] == ["yes", "keychain"]
    assert by_slug["reducto"][3] == ".env"
    assert by_slug["pulse"][3] == "env"
    assert by_slug["pymupdf"][3] == "-"
    assert KEY not in out


# --- PS4: a failure from a keychain-sourced key is redacted -------------------------------------


def test_ps4_a_failure_echoing_a_keychain_key_is_redacted(
    monkeypatch, capsys, fake_keychain, tmp_path
):
    from openreading import api
    from tests.fakes import ScriptedBackend

    fake_keychain.store[("openreading", "REDUCTO_API_KEY")] = KEY
    exc = TerminalError(f"provider echoed key={KEY}", backend_code="bad_request")
    bad = ScriptedBackend("reducto", required_env=["REDUCTO_API_KEY"], error=exc)
    monkeypatch.setattr(api, "make_adapter", lambda _slug: bad)
    monkeypatch.setattr(cli, "make_adapter", lambda _slug: bad)
    doc = tmp_path / "d.pdf"
    doc.write_bytes(b"%PDF-1.4 tiny")
    code, out, err = _run(["parse", str(doc), "--backend", "reducto"], capsys)
    assert code == 3
    assert KEY not in out + err
    assert "***" in err


# --- the opt-in: every verb but serve and mcp ---------------------------------------------------


def _anthropic_key() -> object:
    desc = make_adapter("anthropic-claude").descriptor
    req = OpenReadingRequest.model_validate(
        {"document": {"path": "/x"}, "backend": {"id": "anthropic-claude"}}
    )
    return EnvCredentialBroker().resolve(desc, req).values.get("api_key")


@pytest.mark.parametrize("flag", [[], ["--keychain"]])
def test_serve_reads_the_keychain_only_with_the_flag(monkeypatch, capsys, fake_keychain, flag):
    import uvicorn

    fake_keychain.store[("openreading", "ANTHROPIC_API_KEY")] = KEY
    seen = {}

    def fake_run(self, sockets=None):
        seen["key"] = _anthropic_key()

    monkeypatch.setattr(uvicorn.Server, "run", fake_run)
    code, _, _ = _run(["serve", "--port", "0", *flag], capsys)
    assert code == 0
    if flag:
        assert seen["key"] == KEY
    else:
        assert seen["key"] is None
        assert fake_keychain.calls == []
    # The opt-in ends with the command, so a later caller in this process starts clean.
    assert credentials.keychain_in_use() is False


def test_mcp_never_reads_the_keychain(monkeypatch, fake_keychain):
    import openreading.mcp_server.main as mcp_main

    fake_keychain.store[("openreading", "ANTHROPIC_API_KEY")] = KEY
    seen = {}

    def launch(_args):
        seen["key"] = _anthropic_key()
        return 0

    monkeypatch.setattr(mcp_main, "launch", launch)
    root = str(__import__("pathlib").Path.cwd())
    assert (
        cli.main(
            [
                "mcp",
                "--profile",
                "local-document-proof-v1",
                "--input-root",
                root,
                "--artifact-root",
                root + "/a",
            ]
        )
        == 0
    )
    assert seen["key"] is None and fake_keychain.calls == []


def test_an_ordinary_verb_reads_the_keychain(capsys, fake_keychain):
    fake_keychain.store[("openreading", "ANTHROPIC_API_KEY")] = KEY
    code, out, _ = _run(["backends"], capsys)
    assert code == 0
    row = next(r for r in out.splitlines() if r.startswith("anthropic-claude"))
    assert "yes" in row.split()


def test_without_the_setup_extra_backends_is_unchanged(monkeypatch, capsys, fake_keychain):
    fake_keychain.store[("openreading", "ANTHROPIC_API_KEY")] = KEY
    monkeypatch.setitem(sys.modules, "keyring", None)
    code, out, err = _run(["backends"], capsys)
    assert code == 0 and "SOURCE" not in out and err == ""
    _stdin(monkeypatch, KEY + "\n")
    code, _, err = _run(["keys", "set", "anthropic-claude", "--stdin"], capsys)
    assert code == 2 and "[setup] extra" in err


# --- provider profiles from openreading.yaml -----------------------------------------------------

PROFILE_YAML = """\
version: 1
providers:
  example-llm:
    kind: openai-compatible
    base_url: https://llm.example.test/v1
    model: vision-1
    key_env: EXAMPLE_LLM_API_KEY
  local-llm:
    kind: openai-compatible
    base_url: http://localhost:11434/v1
    model: llava
"""


def test_set_and_list_cover_a_profile_that_names_a_key_env(
    monkeypatch, capsys, fake_keychain, tmp_path
):
    (tmp_path / "openreading.yaml").write_text(PROFILE_YAML, encoding="utf-8")
    _stdin(monkeypatch, KEY + "\n")
    code, out, err = _run(["keys", "set", "example-llm", "--stdin"], capsys)
    assert code == 0, err
    # The key is bound to the profile's endpoint, never saved under the bare variable.
    assert _saved(fake_keychain, "EXAMPLE_LLM_API_KEY@https://llm.example.test") == KEY
    assert _saved(fake_keychain, "EXAMPLE_LLM_API_KEY") is None
    code, out, _ = _run(["keys", "list"], capsys)
    assert code == 0
    rows = {tuple(line.split()) for line in out.splitlines()[1:]}
    assert ("example-llm", "EXAMPLE_LLM_API_KEY", "keychain") in rows
    assert not any(r[0] == "local-llm" for r in rows)
    assert KEY not in out


def test_a_profile_without_key_env_has_nothing_to_set(capsys, fake_keychain, tmp_path):
    (tmp_path / "openreading.yaml").write_text(PROFILE_YAML, encoding="utf-8")
    code, _, err = _run(["keys", "set", "local-llm", "--stdin"], capsys)
    assert code == 2
    assert "local-llm declares no secret" in err
    assert fake_keychain.calls == []


def test_a_profile_key_from_the_keychain_reaches_the_adapter_and_never_os_environ(
    capsys, fake_keychain, tmp_path
):
    import os

    import httpx
    import respx

    from openreading.testing.sample_pdf import build_sample_pdf

    pytest.importorskip("pymupdf", reason="pymupdf (rasterizer) not installed")
    (tmp_path / "openreading.yaml").write_text(PROFILE_YAML, encoding="utf-8")
    fake_keychain.store[("openreading", "EXAMPLE_LLM_API_KEY@https://llm.example.test")] = KEY
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(build_sample_pdf())
    reply = {
        "choices": [{"message": {"content": "# Hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }
    with respx.mock:
        route = respx.post("https://llm.example.test/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=reply)
        )
        code, out, err = _run(["parse", str(doc), "--backend", "example-llm"], capsys)
    assert code == 0, err
    assert route.calls.last.request.headers["authorization"] == f"Bearer {KEY}"
    assert "EXAMPLE_LLM_API_KEY" not in os.environ
    assert KEY not in out + err
