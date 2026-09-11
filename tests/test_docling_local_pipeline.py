"""The local pipeline stays optional and refuses settings outside the fixed profile."""

import importlib
import subprocess
import sys


def test_local_adapter_package_import_does_not_load_native_engines():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import openreading.adapters.docling_local; "
            "assert not any(n in sys.modules for n in ('docling', 'onnxruntime', 'pypdfium2', 'torch'))",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_pipeline_configuration_requires_explicit_local_assets(tmp_path):
    module = importlib.import_module("openreading.adapters.docling_local.config")
    try:
        module.LocalDoclingConfig(artifacts_path=tmp_path / "missing").validate_assets()
    except ValueError as error:
        assert "asset" in str(error).lower()
    else:
        raise AssertionError("Missing assets must refuse conversion before model initialization")


def test_config_wire_roundtrip_keeps_setup_ocr_and_explicit_paths(tmp_path):
    from openreading.adapters.docling_local.config import LocalDoclingConfig

    value = LocalDoclingConfig(
        tmp_path, ocr=True, tesseract_cmd=tmp_path / "tesseract", tessdata_path=tmp_path / "data"
    )
    assert LocalDoclingConfig.from_wire(value.wire()) == value


def test_pipeline_initializes_selected_stages_without_loading_weights(tmp_path, monkeypatch):
    from docling.datamodel.base_models import InputFormat
    from docling.models.inference_engines.object_detection.onnxruntime_engine import (
        OnnxRuntimeObjectDetectionEngine,
    )
    from docling.models.stages.layout.layout_object_detection_model import (
        LayoutObjectDetectionModel,
    )

    from openreading.adapters.docling_local.config import LocalDoclingConfig
    from openreading.adapters.docling_local.pipeline import create_converter

    seen = []

    def initialize(engine):
        seen.append(engine._resolve_providers())

    monkeypatch.setattr(LocalDoclingConfig, "validate_assets", lambda self: {})
    monkeypatch.setattr(OnnxRuntimeObjectDetectionEngine, "initialize", initialize)
    monkeypatch.setattr(LayoutObjectDetectionModel, "_build_label_map", lambda self: {})
    converter = create_converter(LocalDoclingConfig(tmp_path))
    converter.initialize_pipeline(InputFormat.PDF)
    pipeline = next(iter(converter.initialized_pipelines.values()))
    assert seen == [["CPUExecutionProvider"]]
    assert pipeline.pipeline_options.enable_remote_services is False
    assert pipeline.pipeline_options.allow_external_plugins is False
    assert pipeline.pipeline_options.do_table_structure is False
    assert pipeline.enrichment_pipe == []
    assert list(pipeline.table_model(None, iter([1, 2]))) == [1, 2]
    assert list(pipeline.ocr_model(None, iter([3]))) == [3]


def test_asset_validation_hashes_ocr_data_and_lock_and_refuses_mutation(tmp_path, monkeypatch):
    import hashlib
    from dataclasses import replace

    import pytest

    from openreading.adapters.docling_local import config

    model = tmp_path / config.MODEL_REPOSITORY.replace("/", "--")
    model.mkdir()
    (model / "config.json").write_bytes(b"model")
    monkeypatch.setattr(
        config, "MODEL_FILES", {"config.json": hashlib.sha256(b"model").hexdigest()}
    )
    (tmp_path / "eng.traineddata").write_bytes(b"language")
    (tmp_path / "osd.traineddata").write_bytes(b"orientation")
    (tmp_path / "tesseract").write_bytes(b"executable")
    (tmp_path / "uv.lock").write_bytes(b"lock")
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "tsv").write_bytes(b"tessedit_create_tsv 1")
    selected = config.LocalDoclingConfig(
        tmp_path,
        ocr=True,
        tesseract_cmd=tmp_path / "tesseract",
        tessdata_path=tmp_path,
        dependency_lock=tmp_path / "uv.lock",
    )
    hashes = selected.validate_assets()
    assert set(hashes) == {
        "config.json",
        "tesseract",
        "eng.traineddata",
        "osd.traineddata",
        "configs/tsv",
        "dependency_lock",
    }
    for changes in [
        {"languages": ("../secret",)},
        {"languages": ()},
        {"threads": 0},
        {"tessdata_path": None},
    ]:
        with pytest.raises(ValueError, match="assets"):
            replace(selected, **changes).validate_assets()
    (model / "config.json").write_bytes(b"changed")
    with pytest.raises(ValueError, match="assets"):
        selected.validate_assets()


def test_pipeline_rejects_untested_upstream_version(tmp_path, monkeypatch):
    import pytest

    from openreading.adapters.docling_local import pipeline

    monkeypatch.setattr(pipeline.LocalDoclingConfig, "validate_assets", lambda self: {})
    monkeypatch.setattr(pipeline.importlib.metadata, "version", lambda name: "unknown")
    with pytest.raises(ValueError, match="tested Docling version"):
        pipeline.create_converter(pipeline.LocalDoclingConfig(tmp_path))


def test_setup_rejects_invalid_types_before_native_import(tmp_path):
    import pytest

    from openreading.adapters.docling_local.config import LocalDoclingConfig

    for change in [
        {"artifacts_path": None},
        {"ocr": 1},
        {"threads": True},
        {"languages": "eng"},
        {"languages": [1]},
        {"unexpected": 1},
    ]:
        with pytest.raises((ValueError, TypeError)):
            LocalDoclingConfig.from_wire({"artifacts_path": str(tmp_path), **change})


def test_selected_preprocessor_uses_numpy_without_torch_or_auto_dispatch(tmp_path, monkeypatch):
    import json

    from docling.datamodel.base_models import InputFormat
    from docling.models.inference_engines.object_detection.onnxruntime_engine import (
        OnnxRuntimeObjectDetectionEngine,
    )
    from docling.models.stages.layout.layout_object_detection_model import (
        LayoutObjectDetectionModel,
    )
    from PIL import Image

    from openreading.adapters.docling_local import pipeline

    engines = []
    monkeypatch.setattr(pipeline.LocalDoclingConfig, "validate_assets", lambda self: {})
    monkeypatch.setattr(
        OnnxRuntimeObjectDetectionEngine, "initialize", lambda self: engines.append(self)
    )
    monkeypatch.setattr(LayoutObjectDetectionModel, "_build_label_map", lambda self: {})
    converter = pipeline.create_converter(pipeline.LocalDoclingConfig(tmp_path))
    converter.initialize_pipeline(InputFormat.PDF)
    (tmp_path / "preprocessor_config.json").write_text(
        json.dumps(
            {
                "image_processor_type": "RTDetrImageProcessor",
                "size": {"height": 640, "width": 640},
                "do_resize": True,
                "do_rescale": False,
                "do_normalize": False,
                "do_pad": False,
            }
        )
    )
    (tmp_path / "processor_config.json").write_text(
        json.dumps({"image_processor": {"size": {"height": 128, "width": 128}}})
    )
    processor = engines[0]._load_preprocessor(tmp_path)
    output = processor(images=[Image.new("RGB", (40, 30), "white")], return_tensors="np")
    assert output["pixel_values"].shape == (1, 3, 640, 640)
    assert processor.__class__.__name__ == "RTDetrImageProcessorPil"


def test_ocr_orientation_uses_only_selected_tessdata(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace

    from docling.datamodel.base_models import InputFormat
    from docling.models.inference_engines.object_detection.onnxruntime_engine import (
        OnnxRuntimeObjectDetectionEngine,
    )
    from docling.models.stages.layout.layout_object_detection_model import (
        LayoutObjectDetectionModel,
    )
    from docling.models.stages.ocr.tesseract_ocr_cli_model import TesseractOcrCliModel

    from openreading.adapters.docling_local import pipeline

    monkeypatch.setattr(pipeline.LocalDoclingConfig, "validate_assets", lambda self: {})
    monkeypatch.setattr(OnnxRuntimeObjectDetectionEngine, "initialize", lambda self: None)
    monkeypatch.setattr(LayoutObjectDetectionModel, "_build_label_map", lambda self: {})
    monkeypatch.setattr(
        TesseractOcrCliModel, "_get_name_and_version", lambda self: ("tesseract", "5.5.1")
    )
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert command[command.index("--tessdata-dir") + 1] == str(tmp_path / "selected")
        return SimpleNamespace(stdout=b"Orientation in degrees: 0\nScript: Latin\n")

    monkeypatch.setattr(subprocess, "run", run)
    selected = pipeline.LocalDoclingConfig(
        tmp_path,
        ocr=True,
        tesseract_cmd=tmp_path / "tesseract",
        tessdata_path=tmp_path / "selected",
    )
    converter = pipeline.create_converter(selected)
    converter.initialize_pipeline(InputFormat.PDF)
    active = next(iter(converter.initialized_pipelines.values()))
    frame = active.ocr_model._perform_osd(str(tmp_path / "image.png"))
    assert str(frame.loc[frame.key == "Orientation in degrees", "value"].iloc[0]).strip() == "0"
    assert len(commands) == 1
    assert commands[0][commands[0].index("-l") + 1] == "osd"


def test_ocr_output_configuration_is_hashed_with_language_data(tmp_path, monkeypatch):
    import hashlib

    import pytest

    from openreading.adapters.docling_local import config

    model = tmp_path / config.MODEL_REPOSITORY.replace("/", "--")
    model.mkdir()
    (model / "config.json").write_bytes(b"model")
    monkeypatch.setattr(
        config, "MODEL_FILES", {"config.json": hashlib.sha256(b"model").hexdigest()}
    )
    for name in ("eng.traineddata", "osd.traineddata", "tesseract"):
        (tmp_path / name).write_bytes(name.encode())
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "tsv").write_bytes(b"tessedit_create_tsv 1\n")
    selected = config.LocalDoclingConfig(
        tmp_path, ocr=True, tesseract_cmd=tmp_path / "tesseract", tessdata_path=tmp_path
    )
    before = selected.validate_assets()
    (tmp_path / "configs" / "tsv").write_bytes(b"tessedit_create_txt 1\n")
    assert selected.validate_assets()["configs/tsv"] != before["configs/tsv"]
    (tmp_path / "configs" / "tsv").unlink()
    with pytest.raises(ValueError, match="assets"):
        selected.validate_assets()
