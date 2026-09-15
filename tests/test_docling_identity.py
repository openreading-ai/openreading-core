"""Docling provenance refuses missing assets and changes across warm conversions."""

import pytest

from openreading.adapters.docling_local.config import LocalDoclingConfig
from openreading.artifacts.limits import ArtifactError, DoclingLimits, ProfileConfig
from openreading.artifacts.service import engine_identity


def test_missing_assets_do_not_leak_paths_into_identity_failure(tmp_path):
    config = ProfileConfig(
        tmp_path,
        tmp_path / "store",
        DoclingLimits(
            pages=10, deadline_seconds=60, worker_memory_bytes=2**31, worker_idle_seconds=60
        ),
        LocalDoclingConfig(tmp_path / "secret-assets"),
    )
    with pytest.raises(ArtifactError, match="engine_identity_unavailable"):
        engine_identity(config)


def test_worker_refuses_assets_that_changed_since_parent_identity(tmp_path, monkeypatch):
    from openreading.adapters.docling_local import client
    from openreading.artifacts import worker

    monkeypatch.setattr(client, "preflight_pdf", lambda path: (1, False))
    monkeypatch.setattr(LocalDoclingConfig, "validate_assets", lambda self: {"tesseract": "new"})
    with pytest.raises(ArtifactError, match="engine_identity_unavailable"):
        worker.extract(
            {
                "directory": str(tmp_path),
                "pages": 10,
                "docling": LocalDoclingConfig(tmp_path).wire(),
                "expected_assets": {"tesseract": "old"},
            }
        )


def test_measured_identity_includes_dependency_lock_and_ocr_version(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from openreading.artifacts import service

    monkeypatch.setattr(
        LocalDoclingConfig, "validate_assets", lambda self: {"dependency_lock": "a" * 64}
    )
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stdout="tesseract 5.5.1\n build details"),
    )
    config = ProfileConfig(
        tmp_path,
        tmp_path / "store",
        DoclingLimits(
            pages=10, deadline_seconds=60, worker_memory_bytes=2**31, worker_idle_seconds=60
        ),
        LocalDoclingConfig(
            tmp_path,
            ocr=True,
            tesseract_cmd=tmp_path / "tesseract",
            dependency_lock=tmp_path / "uv.lock",
        ),
    )
    identity = engine_identity(config)
    assert identity.backend_id == "docling_local"
    assert identity.core_commit is None
    assert identity.extraction_settings["tesseract_version"] == "tesseract 5.5.1"
    assert identity.extraction_settings["assets"]["dependency_lock"] == "a" * 64
    assert identity.extraction_settings["dependencies"]["docling-slim"] == "2.126.0"
    # Transitive distributions reach extraction too, so a drift in any of them is a new engine.
    assert {"docling-core", "pydantic", "onnxruntime"} <= set(
        identity.extraction_settings["dependencies"]
    )
    assert identity.extraction_settings["tesseract_build"] == ["build details"]


def test_incomplete_ocr_setup_is_sanitized(tmp_path):
    config = ProfileConfig(
        tmp_path,
        tmp_path / "store",
        DoclingLimits(
            pages=10, deadline_seconds=60, worker_memory_bytes=2**31, worker_idle_seconds=60
        ),
        LocalDoclingConfig(tmp_path, ocr=True, dependency_lock=tmp_path / "uv.lock"),
    )
    with pytest.raises(ArtifactError, match="engine_identity_unavailable"):
        engine_identity(config)


def test_dependency_closure_follows_markers_and_requested_extras(monkeypatch):
    from types import SimpleNamespace

    from openreading.artifacts import service

    catalog = {
        "root": ("1.0", ["child>=1", "optional; extra == 'pdf'", "legacy; python_version < '3'"]),
        "child": ("2.0", ["leaf"]),
        "optional": ("3.0", []),
        "leaf": ("4.0", []),
        "legacy": ("5.0", []),
    }

    def distribution(name):
        if name not in catalog:
            raise service.importlib.metadata.PackageNotFoundError(name)
        version, requires = catalog[name]
        return SimpleNamespace(version=version, requires=requires)

    monkeypatch.setattr(service.importlib.metadata, "distribution", distribution)
    assert service._dependency_versions({"root": ("pdf",)}) == {
        "child": "2.0",
        "leaf": "4.0",
        "optional": "3.0",
        "root": "1.0",
    }
    assert "optional" not in service._dependency_versions({"root": ()})


def test_extraction_roots_come_from_the_installed_extra_not_a_second_list():
    from openreading.artifacts import service

    roots = service._extraction_roots()
    assert set(roots["docling-slim"]) == {"convert-core", "format-pdf"}
    assert {"onnxruntime", "transformers"} <= set(roots)
    assert {"pydantic", "jsonschema", "puremagic"} <= set(roots)
