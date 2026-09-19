"""Scoped resume replays recorded work without ambient configuration or widened dispatch."""

import base64

import pytest

from openreading import api
from openreading.credentials import EnvCredentialBroker
from openreading.ledger.header import HeaderMismatch
from openreading.strategies.model import StrategyConfig
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.errors import ScopeRefused
from openreading.types.request import OpenReadingRequest


def recorded(tmp_path, monkeypatch, config=None, scope=None):
    config = config or {"version": 1, "strategies": {"local": {"backend": "pymupdf"}}}
    ledger = tmp_path / "ledger"
    monkeypatch.setenv("OPENREADING_LEDGER", str(ledger))
    armed = []
    first = api.run_request(
        OpenReadingRequest.model_validate(
            {
                "document": {
                    "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
                    "filename": "sample.pdf",
                },
                "backend": {"id": "strategy:local"},
            }
        ),
        strategy_config=StrategyConfig.model_validate(config),
        backend_allowlist=scope,
        on_run_armed=armed.append,
        keep_candidates=True,
    )
    return ledger, armed[0], first, config


def test_explicit_resume_ignores_ambient_config_and_ledger(tmp_path, monkeypatch):
    ledger, run_id, first, config = recorded(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "wrong-ledger"))
    monkeypatch.setenv("OPENREADING_CONFIG", str(tmp_path / "missing.yaml"))
    load = api.load_config_file
    monkeypatch.setattr(
        api,
        "load_config_file",
        lambda value: (
            load(value) if value is not None else pytest.fail("ambient configuration read")
        ),
    )
    from openreading.adapters.pymupdf import PyMuPDFAdapter

    monkeypatch.setattr(
        PyMuPDFAdapter, "submit", lambda *_: pytest.fail("terminal step redispatched")
    )
    resumed = api.resume_run(
        run_id,
        ledger_root=ledger,
        config=config,
        broker=EnvCredentialBroker(environ={}),
        backend_allowlist=frozenset({"pymupdf"}),
        keep_candidates=True,
    )
    assert resumed["document"] == first["document"]
    assert resumed["backend"] == first["backend"]
    assert not (tmp_path / "wrong-ledger").exists()


def test_scoped_resume_refuses_revoked_backend_before_blob_read(tmp_path, monkeypatch):
    ledger, run_id, _, config = recorded(tmp_path, monkeypatch)
    monkeypatch.setattr(
        api.LocalFsBlobStore, "get", lambda *_: pytest.fail("read before authorization")
    )
    with pytest.raises(ScopeRefused):
        api.resume_run(run_id, ledger_root=ledger, config=config, backend_allowlist=frozenset())


def test_scoped_resume_rebuilds_original_pruned_plan(tmp_path, monkeypatch):
    config = {
        "version": 1,
        "strategies": {"local": {"steps": [{"backend": "tesseract"}, {"backend": "pymupdf"}]}},
    }
    ledger, run_id, first, config = recorded(tmp_path, monkeypatch, config, frozenset({"pymupdf"}))
    result = api.resume_run(
        run_id,
        ledger_root=ledger,
        config=config,
        backend_allowlist=frozenset({"pymupdf", "tesseract"}),
    )
    assert result["document"] == first["document"]
    assert result["backend"]["id"] == "pymupdf"


def test_explicit_resume_refuses_changed_configuration(tmp_path, monkeypatch):
    ledger, run_id, _, config = recorded(tmp_path, monkeypatch)
    config["strategies"]["local"] = {"steps": [{"backend": "pymupdf"}, {"backend": "pymupdf"}]}
    with pytest.raises(HeaderMismatch):
        api.resume_run(
            run_id, ledger_root=ledger, config=config, backend_allowlist=frozenset({"pymupdf"})
        )
