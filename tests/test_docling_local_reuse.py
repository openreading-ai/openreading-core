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

        return SimpleNamespace(convert=convert)

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

    def measured(self, data):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.05)
            return convert(self, data)
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
