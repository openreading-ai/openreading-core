"""LiteParse through core's supervised worker: real subprocesses, the real engine, no network.

These tests run the pinned `liteparse` wheel inside `openreading.artifacts.supervisor.WarmWorker`.
They never enable OCR with real language data, so nothing can reach LiteParse's download path.
OCR recognition itself is the asset-gated live test in `tests/test_liteparse.py`.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys

import pymupdf
import pytest

from openreading.adapters.liteparse import LiteParseAdapter, worker
from openreading.adapters.liteparse.adapter import LiteParseWorkerError, WorkerRunner
from openreading.types.errors import TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext


def _pdf(text="Native invoice total 1,250.00", **save):
    with pymupdf.open() as document:
        document.new_page().insert_text((72, 100), text, fontsize=12)
        return document.tobytes(**save)


def _req(data, **over):
    body = {
        "document": {"bytes_base64": base64.b64encode(data).decode()},
        "backend": {"id": "liteparse"},
        "features": {"ocr": "off"},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def test_native_pdf_parses_in_an_isolated_worker_that_exits():
    runner = WorkerRunner()
    adapter = LiteParseAdapter(runner=runner)
    ctx = RunContext(deadline_ms=120_000)
    job = adapter.submit(_req(_pdf()), ctx)
    response = adapter.normalize(job, ctx, _req(_pdf()))
    assert "1,250.00" in response.document.pages[0].text
    assert response.document.pages[0].blocks[0].bbox is not None
    with pytest.raises(ProcessLookupError):
        os.kill(runner.last_pid, 0)


def test_encrypted_and_invalid_pdfs_report_fixed_codes():
    encrypted = _pdf(
        encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="private-password", owner_pw="owner"
    )
    for data, code in ((encrypted, "password_required"), (b"%PDF-1.7 broken", "unsupported_input")):
        with pytest.raises(TerminalError) as error:
            LiteParseAdapter(runner=WorkerRunner()).submit(_req(data), RunContext())
        assert error.value.backend_code == code
        assert "private-password" not in str(error.value)


def test_deadline_kills_a_running_worker_and_reports_timeout():
    slow = [sys.executable, "-c", "import sys, time; sys.stdin.readline(); time.sleep(30)"]
    runner = WorkerRunner(command=slow)
    with pytest.raises(LiteParseWorkerError) as error:
        runner.parse(
            _pdf(), suffix=".pdf", options={"ocr_enabled": False}, ctx=RunContext(deadline_ms=300)
        )
    assert error.value.code == "timeout"
    with pytest.raises(ProcessLookupError):
        os.kill(runner.last_pid, 0)


def test_memory_ceiling_kills_the_worker():
    runner = WorkerRunner(memory_bytes=1)
    with pytest.raises(LiteParseWorkerError) as error:
        runner.parse(
            _pdf(),
            suffix=".pdf",
            options={"ocr_enabled": False},
            ctx=RunContext(deadline_ms=60_000),
        )
    assert error.value.code == "memory_limit"


def test_work_directory_is_private_and_removed(monkeypatch):
    created = []
    original = worker.__name__
    import tempfile

    real_mkdtemp = tempfile.mkdtemp

    def recording(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(path)
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", recording)
    WorkerRunner().parse(_pdf(), suffix=".pdf", options={"ocr_enabled": False}, ctx=RunContext())
    assert created and not any(os.path.exists(path) for path in created)
    assert original == "openreading.adapters.liteparse.worker"


def test_missing_assets_never_start_a_worker(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a worker process was started")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    empty = tmp_path / "tessdata"
    empty.mkdir()
    request = _req(_pdf(), features={"ocr": "auto"})
    with pytest.raises(TerminalError) as error:
        LiteParseAdapter().submit(request, RunContext(runtime={"tessdata_path": str(empty)}))
    assert error.value.backend_code == "ocr_assets_missing"


def test_worker_options_never_name_a_remote_ocr_server(tmp_path):
    options = worker.liteparse_options(
        {"ocr_enabled": True, "tessdata_path": str(tmp_path), "ocr_language": "eng"}
    )
    assert "ocr_server_url" not in options
    assert "ocr_server_headers" not in options
    assert options["quiet"] is True
    assert options["tessdata_path"] == str(tmp_path)
    assert worker.liteparse_options({"ocr_enabled": False})["ocr_enabled"] is False


def test_worker_refuses_changed_assets_before_importing_the_engine(tmp_path, monkeypatch):
    (tmp_path / "eng.traineddata").write_bytes(b"changed")
    monkeypatch.setitem(sys.modules, "liteparse", None)  # importing it would now raise
    with pytest.raises(worker.WorkerFailure) as error:
        worker.parse_document(
            {
                "input": str(tmp_path / "missing.pdf"),
                "ocr_enabled": True,
                "tessdata_path": str(tmp_path),
                "ocr_language": "eng",
                "expected_sha256": "0" * 64,
            }
        )
    assert error.value.code == "engine_identity_unavailable"


# --- in-process coverage of the child module ---------------------------------------------------
# Coverage cannot see inside the WarmWorker subprocess, so these drive the same functions directly.


def test_parse_document_runs_the_engine_in_process_without_bulky_fields(tmp_path):
    source = tmp_path / "native.pdf"
    source.write_bytes(_pdf())
    data = worker.parse_document({"input": str(source), "ocr_enabled": False})
    assert "1,250.00" in data["text"]
    assert "images" not in data and "screenshots" not in data
    items = [item for page in data["pages"] for item in page["text_items"]]
    assert items and all("char_codes" not in item and "words" not in item for item in items)


def _fake_engine(monkeypatch, *, message=None, seen=None):
    import types

    class ParseError(Exception):
        pass

    class LiteParse:
        def __init__(self, **options):
            if seen is not None:
                seen.append(options)

        def parse(self, path):
            if message is not None:
                raise ParseError(message)
            return types.SimpleNamespace()

    module = types.SimpleNamespace(LiteParse=LiteParse, ParseError=ParseError)
    monkeypatch.setitem(sys.modules, "liteparse", module)
    monkeypatch.setattr(worker, "_serialize", lambda result: {"pages": []})


@pytest.mark.parametrize(
    "message,code",
    [
        ("PDF error: password required", "password_required"),
        ("PDF error: invalid PDF format", "unsupported_format"),
        ("engine exploded at /private/secret.pdf", "parse_failed"),
    ],
)
def test_engine_errors_map_to_fixed_codes(monkeypatch, message, code):
    _fake_engine(monkeypatch, message=message)
    with pytest.raises(worker.WorkerFailure) as error:
        worker.parse_document({"input": "input.pdf", "ocr_enabled": False})
    assert error.value.code == code
    assert "secret" not in str(error.value)


def test_missing_engine_is_a_parse_failure(monkeypatch):
    monkeypatch.setitem(sys.modules, "liteparse", None)
    with pytest.raises(worker.WorkerFailure) as error:
        worker.parse_document({"input": "input.pdf", "ocr_enabled": False})
    assert error.value.code == "parse_failed"


def test_verified_assets_reach_the_engine_and_vanished_assets_do_not(tmp_path, monkeypatch):
    import hashlib

    content = b"synthetic traineddata"
    (tmp_path / "eng.traineddata").write_bytes(content)
    seen: list[dict] = []
    _fake_engine(monkeypatch, seen=seen)
    job = {
        "input": "input.pdf",
        "ocr_enabled": True,
        "tessdata_path": str(tmp_path),
        "ocr_language": "eng",
        "expected_sha256": hashlib.sha256(content).hexdigest(),
    }
    assert worker.parse_document(job) == {"pages": []}
    assert seen[0]["tessdata_path"] == str(tmp_path) and seen[0]["ocr_language"] == "eng"
    (tmp_path / "eng.traineddata").unlink()
    with pytest.raises(worker.WorkerFailure) as error:
        worker.parse_document(job)
    assert error.value.code == "engine_identity_unavailable"


def test_serve_reports_each_job_outcome_over_the_control_pipe(tmp_path, monkeypatch):
    import io
    import json
    import types

    outcomes = iter([{"pages": []}, worker.WorkerFailure("password_required"), RuntimeError("x")])

    def fake_parse(job):
        outcome = next(outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(worker, "parse_document", fake_parse)
    jobs = [{"id": f"j{i}", "output": str(tmp_path / f"out{i}.json")} for i in range(3)]
    lines = b"".join(json.dumps(job).encode() + b"\n" for job in jobs)
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(lines)))
    read_fd, write_fd = os.pipe()
    try:
        assert worker.serve(write_fd) == 0
    finally:
        os.close(write_fd)
    with os.fdopen(read_fd, "rb") as stream:
        records = [json.loads(line) for line in stream.read().splitlines()]
    assert records == [
        {"id": "j0", "stage": "conversion"},
        {"id": "j0", "stage": "writing"},
        {"id": "j0", "ok": True},
        {"id": "j1", "stage": "conversion"},
        {"id": "j1", "error": "password_required"},
        {"id": "j2", "stage": "conversion"},
        {"id": "j2", "error": "parse_failed"},
    ]
    assert json.loads((tmp_path / "out0.json").read_text()) == {"pages": []}


@pytest.mark.parametrize("line", [b"x" * 9000, b"{}"])
def test_serve_stops_on_an_oversized_or_unterminated_job(monkeypatch, line):
    import io
    import types

    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(line)))
    read_fd, write_fd = os.pipe()
    try:
        assert worker.serve(write_fd) == 2
    finally:
        os.close(write_fd)
        os.close(read_fd)


def test_main_serves_only_when_asked(monkeypatch):
    monkeypatch.setattr(worker, "serve", lambda control_fd: control_fd + 100)
    assert worker.main(["--serve", "--control-fd", "7"]) == 107
    assert worker.main(["--control-fd", "7"]) == 2


_ACK = (
    "import json, os, sys\n"
    "fd = int(sys.argv[sys.argv.index('--control-fd') + 1])\n"
    "job = json.loads(sys.stdin.readline())\n"
    "{write}\n"
    "os.write(fd, (json.dumps({{'id': job['id'], 'ok': True}}) + '\\n').encode())\n"
    "sys.stdin.readline()\n"
)


def test_a_worker_that_acknowledges_without_output_is_a_parse_failure():
    command = [sys.executable, "-c", _ACK.format(write="pass")]
    with pytest.raises(LiteParseWorkerError) as error:
        WorkerRunner(command=command).parse(
            _pdf(), suffix=".pdf", options={"ocr_enabled": False}, ctx=RunContext()
        )
    assert error.value.code == "parse_failed"


def test_an_oversized_result_is_refused_before_it_is_read(monkeypatch):
    from openreading.adapters.liteparse import adapter as module

    monkeypatch.setattr(module, "_MAX_RESULT_BYTES", 1)
    write = "open(job['output'], 'w').write('{\"pages\": []}')"
    command = [sys.executable, "-c", _ACK.format(write=write)]
    with pytest.raises(LiteParseWorkerError) as error:
        WorkerRunner(command=command).parse(
            _pdf(), suffix=".pdf", options={"ocr_enabled": False}, ctx=RunContext()
        )
    assert error.value.code == "result_too_large"


def test_a_frozen_executable_refuses_to_guess_a_worker(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    with pytest.raises(LiteParseWorkerError) as error:
        WorkerRunner().parse(
            _pdf(), suffix=".pdf", options={"ocr_enabled": False}, ctx=RunContext()
        )
    assert error.value.code == "worker_unavailable"
