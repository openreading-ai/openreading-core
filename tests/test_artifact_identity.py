"""Installed engine provenance cannot borrow an enclosing repository or load native code."""

import builtins
import json
import subprocess

import pytest

from openreading.artifacts import service


@pytest.mark.parametrize("inside_repository", [False, True])
def test_installed_identity_never_uses_an_enclosing_git_repository(
    tmp_path, monkeypatch, inside_repository
):
    package = tmp_path / "package/openreading"
    (package / "artifacts").mkdir(parents=True)
    (package / "artifacts/service.py").write_text("# installed module")
    if inside_repository:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(tmp_path),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "--allow-empty",
                "-qm",
                "unrelated",
            ],
            check=True,
        )
    monkeypatch.setattr(service, "__file__", str(package / "artifacts/service.py"))
    identity = service.engine_identity()
    assert identity.core_commit is None
    assert identity.backend_version
    assert identity.core_version


def test_identity_does_not_import_native_parser(monkeypatch):
    original = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name == "pymupdf" or name.startswith("pymupdf."):
            raise AssertionError("Native parser imported by parent")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    assert service.engine_identity().backend_version


def test_invalid_packaged_identity_is_a_sanitized_engine_error(tmp_path, monkeypatch):
    package = tmp_path / "openreading"
    (package / "artifacts").mkdir(parents=True)
    monkeypatch.setattr(service, "__file__", str(package / "artifacts/service.py"))
    (package / "engine-identity.json").write_text(json.dumps({"private": "secret"}))
    from openreading.artifacts.limits import ArtifactError

    with pytest.raises(ArtifactError, match="engine_identity_unavailable") as caught:
        service.engine_identity()
    assert "secret" not in str(caught.value)


def test_fingerprint_tracks_extraction_dependencies_not_cli_or_other_adapters(tmp_path):
    package = tmp_path / "openreading"
    names = [
        "cli/app.py",
        "adapters/docling/adapter.py",
        "adapters/pymupdf/adapter.py",
        "artifacts/passages.py",
        "types/response.py",
        "derive/geometry.py",
        "api.py",
    ]
    for name in names:
        path = package / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('"""Documentation."""\nVALUE = 1\n')
    before = service._source_tree_hash(package)
    for name in names[:2]:
        (package / name).write_text("VALUE = 2\n")
    assert service._source_tree_hash(package) == before
    path = package / names[2]
    path.write_text('"""Revised documentation."""\nVALUE = 1\n')
    assert service._source_tree_hash(package) == before
    for name in names[2:]:
        path = package / name
        old = path.read_text()
        path.write_text("VALUE = 3\n")
        assert service._source_tree_hash(package) != before
        path.write_text(old)


def test_packaged_identity_preserves_explicit_build_provenance(tmp_path, monkeypatch):
    from openreading.artifacts.models import EngineIdentity

    package = tmp_path / "openreading"
    (package / "artifacts").mkdir(parents=True)
    monkeypatch.setattr(service, "__file__", str(package / "artifacts/service.py"))
    expected = EngineIdentity(
        core_version="0.3.0",
        core_commit="a" * 40,
        backend_version="1",
        extraction_settings={"build_sha256": "b" * 64},
    )
    (package / "engine-identity.json").write_text(expected.model_dump_json())
    assert service.engine_identity() == expected


def test_missing_distribution_reports_dependencies_without_native_import(monkeypatch):
    def missing(name):
        raise service.importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(service.importlib.metadata, "version", missing)
    with pytest.raises(ImportError, match="Install the local profile dependencies"):
        service.engine_identity()
