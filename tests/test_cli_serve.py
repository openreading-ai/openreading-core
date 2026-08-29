"""CLI coverage for `openreading serve` (BL-41). `cmd_serve` is exercised by neither `make verify`
(it would block on a real server) nor `make serve-smoke` (that script boots uvicorn directly against
`create_app`, bypassing `cmd_serve` entirely) — so the actual command a user runs has had zero
coverage anywhere in this repository. Every test here drives `cmd_serve` through `main([...])` and
monkeypatches `uvicorn.run` (or `uvicorn` itself) so nothing ever actually binds a socket.

Three cases, per the sprint-6 review 1:1's own correction to a naive two-test plan: (a) the
`except ImportError` branch, forced via `sys.modules["uvicorn"] = None` since a dev environment
synced with `--all-extras` (this repo's own) has uvicorn importable and can never reach that branch
any other way; (b) the vendor-key exposure warning's host-gated on/off behavior; (c) `uvicorn.run`
receiving the right `app`/`host`/`port` on the happy path."""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("uvicorn", reason="server extra not installed")

from openreading.cli import main  # noqa: E402


def test_serve_missing_server_extra_exits_3_not_1(monkeypatch, capsys):
    # Force the `except ImportError` branch even though uvicorn is really installed here: with
    # sys.modules["uvicorn"] = None, `import uvicorn` raises ModuleNotFoundError (an ImportError
    # subclass) per Python's import machinery, exactly like the extra being genuinely absent.
    monkeypatch.setitem(sys.modules, "uvicorn", None)

    rc = main(["serve"])

    assert rc == 3
    err = capsys.readouterr().err
    assert "serve needs the server extra: pip install 'openreading[server]'" in err


def test_serve_warns_only_when_host_is_not_loopback(monkeypatch, capsys):
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: None)

    rc = main(["serve"])  # default --host 127.0.0.1
    assert rc == 0
    assert capsys.readouterr().err == ""

    rc = main(["serve", "--host", "0.0.0.0"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "warning" in err
    assert "0.0.0.0" in err
    assert "vendor keys" in err


def test_serve_happy_path_passes_correct_app_host_port(monkeypatch):
    calls = []
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: calls.append((app, host, port)))
    sentinel_app = object()
    monkeypatch.setattr("openreading.server.create_app", lambda **kw: sentinel_app)

    rc = main(["serve", "--host", "0.0.0.0", "--port", "9999"])

    assert rc == 0
    assert calls == [(sentinel_app, "0.0.0.0", 9999)]


def test_serve_malformed_api_keys_reports_a_tagged_line_not_a_traceback(monkeypatch, capsys):
    """A malformed OPENREADING_API_KEYS is an operator config error, so it must reach the operator
    the way every other `cannot run` config error on this CLI does: one `[serve] …` line on stderr
    and exit 3. It used to escape create_app() as an uncaught ServerConfigError — a 12-line
    traceback whose only useful sentence was the last one, which no log rule matching this
    project's `[tag]` convention would ever catch (C9)."""
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: pytest.fail("must not bind"))
    monkeypatch.setenv("OPENREADING_API_KEYS", "tok-a,,tok-b")

    rc = main(["serve"])

    assert rc == 3
    err = capsys.readouterr().err
    assert err.splitlines() == [
        "[serve] OPENREADING_API_KEYS entry 2 is empty — check for a stray comma"
    ]
    assert "Traceback" not in err


def test_serve_malformed_scopes_names_the_position_and_never_the_value(monkeypatch, capsys):
    """The message content is already right and must stay right: a keys/scopes config error names
    the offending entry's POSITION, never its VALUE, so a startup-failure log line cannot become
    the place a bearer token leaks. Wrapping the raise in a tagged line must not start echoing the
    entry (C9, keeping BL-159 AC-5)."""
    monkeypatch.setattr("uvicorn.run", lambda app, host, port: pytest.fail("must not bind"))
    monkeypatch.setenv("OPENREADING_API_KEYS", "sekrit-token-value")
    monkeypatch.setenv("OPENREADING_API_KEY_SCOPES", "sekrit-token-value=pymupdf,malformed-entry")

    rc = main(["serve"])

    assert rc == 3
    err = capsys.readouterr().err
    assert err.startswith("[serve] ")
    assert "entry 2" in err
    assert "Traceback" not in err
    assert "sekrit-token-value" not in err
    assert "malformed-entry" not in err
