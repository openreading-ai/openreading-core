"""Observed page assembly stays separate from import completion and normalized content."""

import copy
import json
import sys
from types import SimpleNamespace

import pytest

from openreading.artifacts.limits import ArtifactError


def test_pinned_pipeline_observes_only_successfully_assembled_pages(tmp_path, monkeypatch):
    from docling.datamodel.base_models import InputFormat, Page
    from docling.models.inference_engines.object_detection.onnxruntime_engine import (
        OnnxRuntimeObjectDetectionEngine,
    )
    from docling.models.stages.layout.layout_object_detection_model import (
        LayoutObjectDetectionModel,
    )
    from docling.pipeline.standard_pdf_pipeline import ThreadedItem

    from openreading.adapters.docling_local.config import LocalDoclingConfig
    from openreading.adapters.docling_local.pipeline import create_converter

    monkeypatch.setattr(LocalDoclingConfig, "validate_assets", lambda self: {})
    monkeypatch.setattr(OnnxRuntimeObjectDetectionEngine, "initialize", lambda self: None)
    monkeypatch.setattr(LayoutObjectDetectionModel, "_build_label_map", lambda self: {})
    converter = create_converter(LocalDoclingConfig(tmp_path))
    observed = []
    converter.set_page_completed(observed.append)
    pipeline = converter._get_pipeline(InputFormat.PDF)
    page = Page(page_no=2)
    page._image_cache[1.0] = "temporary"
    item = ThreadedItem(payload=page, page_no=2, run_id=1, conv_res=None)
    pipeline._release_page_resources(item)
    assert observed == [2]
    assert page._image_cache == {}
    item.is_failed = True
    pipeline._release_page_resources(item)
    item.payload = None
    pipeline._release_page_resources(item)
    assert observed == [2]
    converter.set_page_completed(None)
    assert converter._get_pipeline(InputFormat.PDF) is pipeline
    item.payload, item.is_failed = page, False
    pipeline._release_page_resources(item)
    assert observed == [2]


@pytest.mark.parametrize("fails", [False, True])
def test_client_clears_observer_after_conversion(tmp_path, monkeypatch, fails):
    from openreading.adapters.docling_local.client import LocalDoclingClient
    from openreading.adapters.docling_local.config import LocalDoclingConfig

    monkeypatch.setattr(LocalDoclingConfig, "validate_assets", lambda self: {})
    calls = []

    class Converter:
        observer = None

        def set_page_completed(self, observer):
            self.observer = observer
            calls.append(observer)

        def convert(self, path):
            self.observer(1)
            if fails:
                raise ValueError("Synthetic conversion failure")
            document = SimpleNamespace(
                export_to_dict=lambda: {"pages": {}}, iterate_items=lambda **kwargs: []
            )
            return SimpleNamespace(
                status=SimpleNamespace(value="success"), pages=[], document=document
            )

    client = LocalDoclingClient(LocalDoclingConfig(tmp_path))
    client._converter = Converter()
    observed = []
    if fails:
        with pytest.raises(ValueError, match="Synthetic"):
            client.convert_path(tmp_path / "source.pdf", page_completed=observed.append)
    else:
        client.convert_path(tmp_path / "source.pdf", page_completed=observed.append)
    assert observed == [1]
    assert calls[-1] is None and client._converter.observer is None


def test_observer_failure_does_not_escape_inside_provider_thread(tmp_path, monkeypatch):
    from openreading.adapters.docling_local.client import LocalDoclingClient
    from openreading.adapters.docling_local.config import LocalDoclingConfig

    monkeypatch.setattr(LocalDoclingConfig, "validate_assets", lambda self: {})
    converter = SimpleNamespace()
    converter.set_page_completed = lambda callback: setattr(converter, "callback", callback)
    returned = []

    def convert(path):
        converter.callback(1)
        returned.append(True)
        return None

    converter.convert = convert
    client = LocalDoclingClient(LocalDoclingConfig(tmp_path))
    client._converter = converter

    def broken(page):
        raise RuntimeError("private observer failure")

    with pytest.raises(ValueError, match="page-progress observation failed"):
        client.convert_path(tmp_path / "source.pdf", page_completed=broken)
    assert returned == [True]
    assert converter.callback is None


@pytest.mark.parametrize("outcome", ["succeeded", "cancelled", "parse_failed"])
def test_status_retains_progress_and_reads_legacy_jobs(tmp_path, monkeypatch, outcome):
    from dataclasses import asdict

    import jsonschema

    from openreading.artifacts import jobs
    from openreading.artifacts.limits import ProfileConfig
    from openreading.artifacts.service import ArtifactService
    from openreading.schemas import import_job_schema
    from openreading.testing.sample_pdf import build_sample_pdf
    from openreading.types.import_job import ImportJob, PageProgress

    source = tmp_path / "input"
    source.mkdir()
    (source / "test.pdf").write_bytes(build_sample_pdf())
    service = ArtifactService(ProfileConfig(source, tmp_path / "store"))
    try:
        receipt = service.import_document("test.pdf")
        store = jobs.ImportJobs(service)
        identifier = "j1_" + "a" * 32
        root = store.root / identifier
        root.mkdir()
        legacy = ImportJob(
            job_id=identifier, state="queued", stage="queued", elapsed_seconds=0.0
        ).wire()
        legacy["schema_version"] = "0.1"
        legacy.pop("page_progress", None)
        jobs._write(root / "status.json", legacy)
        jobs._write(
            root / "request.json",
            {
                "input_root": str(source),
                "artifact_root": str(service.config.artifact_root),
                "limits": asdict(service.config.limits),
                "docling": None,
                "grant": service.store.grant,
                "identity": service.identity.wire(),
                "path": "test.pdf",
                "started": 0.0,
            },
        )
        seen = []

        def parse(path, *, cancelled, progress, page_progress):
            progress("conversion")
            page_progress(PageProgress(pages_assembled=1, total_pages=2))
            seen.append(json.loads((root / "status.json").read_bytes()))
            if outcome != "succeeded":
                raise ArtifactError(outcome)
            page_progress(PageProgress(pages_assembled=2, total_pages=2))
            progress("writing")
            seen.append(json.loads((root / "status.json").read_bytes()))
            return receipt

        monkeypatch.setattr(service, "import_document", parse)
        monkeypatch.setattr(
            "openreading.artifacts.local_jobs.local_service", lambda request: service
        )
        assert jobs.run(root) == 0
        assert seen[0]["state"] == "running" and seen[0]["page_progress"]["pages_assembled"] == 1
        if outcome == "succeeded":
            assert (
                seen[1]["stage"] == "writing" and seen[1]["page_progress"]["pages_assembled"] == 2
            )
        value = store.get(identifier).wire()
        assert value["schema_version"] == "0.2"
        assert value["state"] == ("failed" if outcome == "parse_failed" else outcome)
        assert value["page_progress"] == {
            "pages_assembled": 2 if outcome == "succeeded" else 1,
            "total_pages": 2,
        }
        jsonschema.validate(value, import_job_schema())
        value["schema_version"] = "0.1"
        value.pop("page_progress")
        jobs._write(root / "status.json", value)
        assert store.get(identifier).wire()["page_progress"] is None
        assert json.loads((root / "status.json").read_bytes()) == value
    finally:
        service.close()


def test_worker_counts_distinct_pages_throttles_and_flushes_final_observation(
    tmp_path, monkeypatch
):
    from openreading.adapters.docling_local import client
    from openreading.artifacts import worker
    from tests.test_docling_worker_service import job

    value = job(tmp_path)
    monkeypatch.setattr(
        client.LocalDoclingConfig, "validate_assets", lambda self: {"model": "fixed"}
    )
    monkeypatch.setattr(client, "preflight_pdf", lambda path: (4, False))
    monkeypatch.setattr(worker.time, "monotonic", lambda: 10.0)
    raw = {
        "pages": {str(n): {"size": {"width": 100, "height": 100}} for n in range(1, 5)},
        "items": [{"text": "text", "label": "text", "prov": [{"page_no": 1, "charspan": [0, 4]}]}],
        "page_origins": {str(n): "ocr" for n in range(1, 5)},
    }

    def convert(path, page_completed=None):
        if page_completed:
            for page in [3, 1, 3, 2]:
                page_completed(page)
        return copy.deepcopy(raw)

    observed, stages = [], []
    worker.extract(
        value,
        client=SimpleNamespace(convert_path=convert),
        progress=stages.append,
        page_progress=observed.append,
    )
    assert stages == ["preflight", "conversion", "writing"]
    assert [p.model_dump() for p in observed] == [
        {"pages_assembled": 0, "total_pages": 4},
        {"pages_assembled": 3, "total_pages": 4},
    ]
    with_observer = (tmp_path / "response.json").read_bytes()
    for name in ("response.json", "passages.jsonl"):
        (tmp_path / name).unlink()
    worker.extract(value, client=SimpleNamespace(convert_path=convert))
    assert (tmp_path / "response.json").read_bytes() == with_observer


@pytest.mark.parametrize(
    "messages,valid",
    [
        (
            [
                {"stage": "conversion"},
                {"pages_assembled": 0, "total_pages": 2},
                {"pages_assembled": 2, "total_pages": 2},
                {"stage": "writing"},
            ],
            True,
        ),
        ([{"pages_assembled": 1, "total_pages": 2}], False),
        ([{"stage": "conversion"}, {"pages_assembled": True, "total_pages": 2}], False),
        ([{"stage": "conversion"}, {"pages_assembled": 3, "total_pages": 2}], False),
        (
            [
                {"stage": "conversion"},
                {"pages_assembled": 1, "total_pages": 2},
                {"pages_assembled": 1, "total_pages": 2},
            ],
            False,
        ),
        (
            [
                {"stage": "conversion"},
                {"pages_assembled": 1, "total_pages": 2},
                {"pages_assembled": 0, "total_pages": 2},
            ],
            False,
        ),
        (
            [
                {"stage": "conversion"},
                {"pages_assembled": 1, "total_pages": 2},
                {"pages_assembled": 2, "total_pages": 3},
            ],
            False,
        ),
        ([{"stage": "writing"}, {"pages_assembled": 2, "total_pages": 2}], False),
    ],
)
def test_supervisor_checks_page_progress_without_changing_stage_messages(tmp_path, messages, valid):
    from openreading.artifacts.supervisor import WarmWorker

    child = tmp_path / "progress.py"
    child.write_text(
        "import os,sys,json\nfd=int(sys.argv[-1])\njob=json.loads(sys.stdin.readline())\n"
        "for item in job['messages']+[{'ok':True}]:\n"
        " os.write(fd,(json.dumps({'id':job['id'],**item})+'\\n').encode())\n"
    )
    worker = WarmWorker([sys.executable, str(child)], memory_bytes=None, idle_seconds=60)
    observed, stages = [], []
    try:
        if valid:
            worker.run(
                {"messages": messages},
                check=lambda: None,
                progress=stages.append,
                page_progress=observed.append,
            )
            assert [p.pages_assembled for p in observed] == [0, 2]
            assert stages == ["conversion", "writing"]
        else:
            with pytest.raises(ArtifactError, match="parse_failed"):
                worker.run(
                    {"messages": messages}, check=lambda: None, page_progress=observed.append
                )
            assert worker.pid is None
    finally:
        worker.close()
