"""CLI coverage for `openreading serve` (BL-41). `cmd_serve` is exercised by neither `make verify`
(it would block on a real server) nor `make serve-smoke` (that script boots uvicorn directly against
`create_app`, bypassing `cmd_serve` entirely) — so the actual command a user runs has had zero
coverage anywhere in this repository. Every test here drives `cmd_serve` through `main([...])` and
monkeypatches `uvicorn.run` (or `uvicorn` itself) so nothing ever actually binds a socket.

Three cases, per the sprint-6 Sophia×Trent 1:1's own correction to a naive two-test plan: (a) the
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
