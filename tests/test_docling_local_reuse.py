"""Repeated API calls retain one converter without mixing documents or configurations."""

from types import SimpleNamespace

import pytest

from openreading import run
from openreading.adapters.docling_local.config import LocalDoclingConfig


@pytest.fixture
def conversions(tmp_path, monkeypatch):
    from openreading.adapters.docling_local import client, pipeline

    created = []
    monkeypatch.setattr(client, "_shared", client._SharedClient())
    monkeypatch.setenv("DOCLING_LOCAL_ASSETS", str(tmp_path))
    monkeypatch.setattr(LocalDoclingConfig, "validate_assets", lambda self: {"model": "fixed"})

    class Document:
        def __init__(self, text):
            self.text = text

        def export_to_dict(self):
            return {"pages": {"1": {"size": {"width": 100, "height": 200}}}}

        def iterate_items(self, included_content_layers=None):
            if included_content_layers is not None:
                return []
            return [
                (
                    SimpleNamespace(
                        model_dump=lambda **kw: {
                            "label": "text",
                            "text": self.text,
                            "prov": [{"page_no": 1, "charspan": [0, len(self.text)]}],
                        }
                    ),
                    0,
                )
            ]

    def create(config):
        created.append(config)

        def convert(source):
            text = source.stream.getvalue().decode()
            if text == "broken":
                raise ValueError("conversion failed")
            return SimpleNamespace(
                status=SimpleNamespace(value="success"), document=Document(text), pages=[]
            )

        return SimpleNamespace(convert=convert, set_ocr_mode=lambda mode: None)

    monkeypatch.setattr(pipeline, "create_converter", create)
    yield created
    client._shared._discard()


def test_repeated_api_calls_reuse_converter_and_return_only_current_document(conversions):
    for number in range(12):
        text = f"Document {number}"
        result = run(text.encode(), backend="docling_local", mime_type="application/pdf")
        assert result["status"]["state"] == "succeeded"
        assert result["document"]["text"] == text
    assert len(conversions) == 1


def test_changed_assets_force_new_converter_and_invalid_assets_refuse_reuse(
    conversions, monkeypatch
):
    from openreading.types.errors import TerminalError

    run(b"first", backend="docling_local", mime_type="application/pdf")
    monkeypatch.setattr(LocalDoclingConfig, "validate_assets", lambda self: {"model": "changed"})
    run(b"second", backend="docling_local", mime_type="application/pdf")
    assert len(conversions) == 2

    def invalid(self):
        raise ValueError("Missing model")

    monkeypatch.setattr(LocalDoclingConfig, "validate_assets", invalid)
    with pytest.raises(TerminalError):
        run(b"third", backend="docling_local", mime_type="application/pdf")
    assert len(conversions) == 2


def test_failed_conversion_is_discarded_before_next_request(conversions):
    from openreading.types.errors import TerminalError

    run(b"first", backend="docling_local", mime_type="application/pdf")
    with pytest.raises(TerminalError):
        run(b"broken", backend="docling_local", mime_type="application/pdf")
    assert (
        run(b"next", backend="docling_local", mime_type="application/pdf")["document"]["text"]
        == "next"
    )
    assert len(conversions) == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"ocr": True},
        {"threads": 2},
        {"languages": ("fra",)},
        {"artifacts_path": "other"},
        {"tessdata_path": "data"},
        {"tesseract_cmd": "tesseract"},
        {"dependency_lock": "uv.lock"},
    ],
)
def test_config_changes_evict_instead_of_growing_the_cache(
    conversions, tmp_path, monkeypatch, changes
):
    import weakref
    from dataclasses import replace

    from openreading.adapters.docling_local import client, pipeline

    original = pipeline.create_converter
    alive = weakref.WeakSet()

    class Converter:
        def __init__(self, config):
            # Simulate native wrappers whose Python reference cycles delay finalization.
            assert not alive
            self.cycle = self
            self.inner = original(config)
            alive.add(self)

        def convert(self, source):
            return self.inner.convert(source)

    monkeypatch.setattr(pipeline, "create_converter", Converter)
    changes = {key: tmp_path / val if isinstance(val, str) else val for key, val in changes.items()}
    first = LocalDoclingConfig(tmp_path)
    second = replace(first, **changes)
    for config in (first, second, first):
        assert client.convert_shared(config, b"content")["items"][0]["text"] == "content"
        assert len(alive) == 1
    assert conversions == [first, second, first]


def test_concurrent_api_calls_serialize_conversion_and_keep_documents_separate(
    conversions, monkeypatch
):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from openreading.adapters.docling_local.client import LocalDoclingClient

    convert = LocalDoclingClient.convert
    guard = threading.Lock()
    barrier = threading.Barrier(4)
    active = peak = 0

    def measured(self, data, **kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.05)
            return convert(self, data, **kwargs)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(LocalDoclingClient, "convert", measured)

    def request(number):
        barrier.wait(timeout=5)
        return run(str(number).encode(), backend="docling_local", mime_type="application/pdf")

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(request, range(4)))
    assert [result["document"]["text"] for result in results] == ["0", "1", "2", "3"]
    assert peak == 1
    assert len(conversions) == 1


def test_child_process_does_not_reuse_parent_session_or_locked_mutex(conversions, monkeypatch):
    from openreading.adapters.docling_local import client

    run(b"parent", backend="docling_local", mime_type="application/pdf")

    class InheritedMutex:
        def __enter__(self):
            pytest.fail("The child tried to acquire an inherited mutex")

        def __exit__(self, *args):
            pass

    client._shared.lock = InheritedMutex()
    monkeypatch.setattr(client.os, "getpid", lambda: -1)
    assert (
        run(b"child", backend="docling_local", mime_type="application/pdf")["document"]["text"]
        == "child"
    )
    assert len(conversions) == 2


def test_http_uploads_reuse_the_same_converter(conversions):
    from fastapi.testclient import TestClient

    from openreading.server import create_app

    with TestClient(create_app()) as client:
        for text in ("first upload", "second upload", "third upload"):
            response = client.post(
                "/v1/parse",
                files={"file": ("source.pdf", text.encode(), "application/pdf")},
                data={"request": '{"backend":{"id":"docling_local"}}'},
            )
            assert response.status_code == 200, response.text
            assert response.json()["document"]["text"] == text
    assert len(conversions) == 1


@pytest.mark.parametrize("requested", [None, "auto", "off", "force"])
@pytest.mark.parametrize("configured", [False, True])
def test_api_ocr_mode_obeys_request_and_reports_missing_setup(
    conversions, monkeypatch, tmp_path, requested, configured
):
    from openreading.adapters.docling_local import pipeline
    from openreading.types.errors import TerminalError

    monkeypatch.delenv("DOCLING_LOCAL_TESSERACT", raising=False)
    monkeypatch.delenv("DOCLING_LOCAL_TESSDATA", raising=False)
    if configured:
        monkeypatch.setenv("DOCLING_LOCAL_TESSERACT", str(tmp_path / "tesseract"))
        monkeypatch.setenv("DOCLING_LOCAL_TESSDATA", str(tmp_path / "data"))
    modes = []
    create = pipeline.create_converter

    def observed(config):
        converter = create(config)
        converter.set_ocr_mode = modes.append
        return converter

    monkeypatch.setattr(pipeline, "create_converter", observed)
    kwargs = {} if requested is None else {"features": {"ocr": requested}}
    if requested == "force" and not configured:
        with pytest.raises(TerminalError, match="DOCLING_LOCAL_TESSERACT"):
            run(b"source", backend="docling_local", mime_type="application/pdf", **kwargs)
        assert not conversions
        return
    response = run(b"source", backend="docling_local", mime_type="application/pdf", **kwargs)
    expected = "off" if requested == "off" or not configured else requested or "auto"
    assert modes == [expected]
    assert any(w["code"] == "ocr_skipped" for w in response.get("warnings", [])) == (
        not configured and requested != "off"
    )


def test_http_ocr_toggle_reuses_layout_converter_and_keeps_request_modes_isolated(
    conversions, monkeypatch, tmp_path
):
    import json

    from fastapi.testclient import TestClient

    from openreading.adapters.docling_local import pipeline
    from openreading.server import create_app

    monkeypatch.setenv("DOCLING_LOCAL_TESSERACT", str(tmp_path / "tesseract"))
    monkeypatch.setenv("DOCLING_LOCAL_TESSDATA", str(tmp_path / "data"))
    modes = []
    create = pipeline.create_converter

    def observed(config):
        converter = create(config)
        converter.set_ocr_mode = modes.append
        return converter

    monkeypatch.setattr(pipeline, "create_converter", observed)
    expected = ["off", "force", "auto", "off", "auto", "force"]
    with TestClient(create_app()) as client:
        for mode in expected:
            response = client.post(
                "/v1/parse",
                files={"file": ("source.pdf", mode.encode(), "application/pdf")},
                data={
                    "request": json.dumps(
                        {"backend": {"id": "docling_local"}, "features": {"ocr": mode}}
                    )
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["document"]["text"] == mode
    assert modes == expected
    assert len(conversions) == 1


def test_request_override_does_not_replace_the_clients_configured_default(
    conversions, tmp_path, monkeypatch
):
    from openreading.adapters.docling_local import client, pipeline

    modes = []
    create = pipeline.create_converter

    def observed(config):
        converter = create(config)
        converter.set_ocr_mode = modes.append
        return converter

    monkeypatch.setattr(pipeline, "create_converter", observed)
    selected = client.LocalDoclingClient(LocalDoclingConfig(tmp_path))
    selected.convert(b"forced", ocr_mode="force")
    selected.convert(b"default")
    assert modes == ["force", "off"]


@pytest.mark.parametrize(
    "default,requested,expected",
    [
        (False, "auto", "off"),
        (True, "auto", "auto"),
        (False, "force", "force"),
        (True, "off", "off"),
    ],
)
def test_explicit_adapter_configuration_supplies_default_but_honors_request(
    conversions, monkeypatch, tmp_path, default, requested, expected
):
    from openreading.adapters.docling_local import DoclingLocalAdapter, pipeline
    from openreading.types.runtime import RunContext
    from tests.test_docling_local_adapter import request

    modes = []
    create = pipeline.create_converter

    def observed(config):
        converter = create(config)
        converter.set_ocr_mode = modes.append
        return converter

    monkeypatch.setattr(pipeline, "create_converter", observed)
    adapter = DoclingLocalAdapter(
        config=LocalDoclingConfig(
            tmp_path,
            ocr=default,
            tesseract_cmd=tmp_path / "tesseract",
            tessdata_path=tmp_path / "data",
        )
    )
    adapter.submit(request(features={"ocr": requested}), RunContext())
    assert modes == [expected]


def test_cli_uses_automatic_ocr_when_local_paths_are_configured(
    conversions, tmp_path, monkeypatch, capsys
):
    import json

    from openreading.adapters.docling_local import pipeline
    from openreading.cli.app import main

    monkeypatch.setenv("DOCLING_LOCAL_TESSERACT", str(tmp_path / "tesseract"))
    monkeypatch.setenv("DOCLING_LOCAL_TESSDATA", str(tmp_path / "data"))
    modes = []
    create = pipeline.create_converter

    def observed(config):
        converter = create(config)
        converter.set_ocr_mode = modes.append
        return converter

    monkeypatch.setattr(pipeline, "create_converter", observed)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    assert main(["parse", str(source), "--backend", "docling_local"]) == 0
    assert json.loads(capsys.readouterr().out)["document"]["text"] == "source"
    assert modes == ["auto"]


def test_ocr_assets_are_rechecked_after_off_requests(conversions, monkeypatch, tmp_path):
    from openreading.adapters.docling_local.client import convert_shared

    revision = "first"

    def assets(self):
        return {"model": "fixed", **({"tessdata": revision} if self.ocr else {})}

    monkeypatch.setattr(LocalDoclingConfig, "validate_assets", assets)
    config = LocalDoclingConfig(tmp_path)
    convert_shared(config, b"first", ocr_mode="force")
    convert_shared(config, b"off", ocr_mode="off")
    assert len(conversions) == 1
    revision = "changed"
    convert_shared(config, b"still off", ocr_mode="off")
    assert len(conversions) == 1
    convert_shared(config, b"changed", ocr_mode="force")
    assert len(conversions) == 2


def test_concurrent_requests_keep_their_ocr_mode_through_conversion(
    conversions, monkeypatch, tmp_path
):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from openreading.adapters.docling_local import pipeline

    monkeypatch.setenv("DOCLING_LOCAL_TESSERACT", str(tmp_path / "tesseract"))
    monkeypatch.setenv("DOCLING_LOCAL_TESSDATA", str(tmp_path / "data"))
    create = pipeline.create_converter
    barrier = threading.Barrier(3)

    def observed(config):
        converter = create(config)
        convert = converter.convert
        converter.set_ocr_mode = lambda mode: setattr(converter, "mode", mode)

        def convert_in_mode(source):
            time.sleep(0.05)
            assert converter.mode == source.stream.getvalue().decode()
            return convert(source)

        converter.convert = convert_in_mode
        return converter

    monkeypatch.setattr(pipeline, "create_converter", observed)

    def request(mode):
        barrier.wait(timeout=5)
        return run(
            mode.encode(),
            backend="docling_local",
            mime_type="application/pdf",
            features={"ocr": mode},
        )

    with ThreadPoolExecutor(max_workers=3) as executor:
        responses = list(executor.map(request, ("off", "force", "auto")))
    assert [r["document"]["text"] for r in responses] == ["off", "force", "auto"]
    assert len(conversions) == 1
