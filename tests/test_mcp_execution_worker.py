"""The child repeats authorization and preserves normalized provider results without repair."""

import copy
import hashlib
import io
import json

import pytest

from openreading.mcp_server.execution import ExecutionRefused


@pytest.fixture
def packet(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    raw = b"synthetic source"
    (tmp_path / "source").write_bytes(raw)
    return {
        "configuration": {"version": 1},
        "allowed_backends": ["pymupdf"],
        "allowed_strategies": [],
        "request": {
            "document": {"path": "nested/sample.pdf"},
            "backend": {"id": "pymupdf"},
        },
        "source_sha256": hashlib.sha256(raw).hexdigest(),
    }


def test_child_reauthorizes_before_source_or_provider_access(packet, monkeypatch):
    from openreading.mcp_server import execution_worker

    packet["allowed_backends"] = []

    def forbidden(*args, **kwargs):
        raise AssertionError("Denied child request accessed source or provider")

    monkeypatch.setattr(execution_worker, "safe_read", forbidden)
    monkeypatch.setattr(execution_worker, "run_request", forbidden)
    with pytest.raises(ExecutionRefused, match="^scope_denied$"):
        execution_worker.execute(packet)


def test_child_refuses_corrupt_copy_before_dispatch(packet, tmp_path, monkeypatch):
    from openreading.mcp_server import execution_worker

    (tmp_path / "source").write_bytes(b"changed")

    def forbidden(*args, **kwargs):
        raise AssertionError("Changed copy reached provider")

    monkeypatch.setattr(execution_worker, "run_request", forbidden)
    with pytest.raises(ValueError, match="Copied source digest mismatch"):
        execution_worker.execute(packet)


@pytest.mark.parametrize("state", ["succeeded", "partial", "failed"])
def test_complete_response_and_request_semantics_survive_child(packet, monkeypatch, state):
    from openreading.mcp_server import execution_worker

    payload = {
        "schema_version": "0.3",
        "status": {"state": state},
        "backend": {"id": "pymupdf", "type": "oss_library"},
        "document": {"text": ""},
        "warnings": [{"code": "test_warning", "message": "Literal café warning"}],
        "typed_fields": {"backend_raw": {"value": {"nullable": None}}},
        "backend_raw": {"payload": "excluded", "encoding": "json"},
    }
    before = copy.deepcopy(payload)
    packet_before = copy.deepcopy(packet)

    def provider(req, **kwargs):
        import base64

        assert req.document.path is None
        assert base64.b64decode(req.document.bytes_base64) == b"synthetic source"
        assert req.document.filename == "sample.pdf"
        assert req.document.mime_type == "application/pdf"
        assert kwargs["backend_allowlist"] == frozenset({"pymupdf"})
        assert kwargs["keep_candidates"] is True
        return payload

    monkeypatch.setattr(execution_worker, "run_request", provider)
    result = execution_worker.execute(packet)
    assert result == {k: v for k, v in before.items() if k != "backend_raw"}
    assert payload == before
    assert packet == packet_before


@pytest.mark.parametrize("fault", ["provider", "packet", "output"])
def test_child_main_errors_are_silent_and_never_replace_output(
    packet, monkeypatch, tmp_path, capfd, fault
):
    from openreading.mcp_server import execution_worker

    def failing(*args, **kwargs):
        raise RuntimeError("secret diagnostic")

    if fault == "provider":
        monkeypatch.setattr(execution_worker, "run_request", failing)
    elif fault == "output":
        monkeypatch.setattr(execution_worker, "execute", lambda _: {"complete": True})
        (tmp_path / "response.json").write_bytes(b"original")
    monkeypatch.setattr(
        execution_worker.sys,
        "stdin",
        io.StringIO("broken secret input" if fault == "packet" else json.dumps(packet)),
    )
    assert execution_worker.main() == 1
    captured = capfd.readouterr()
    assert captured.out == captured.err == ""
    if fault == "output":
        assert (tmp_path / "response.json").read_bytes() == b"original"
    else:
        assert not (tmp_path / "response.json").exists()
