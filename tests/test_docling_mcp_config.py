"""The Docling MCP profile requires a closed setup file with explicit resource limits."""

import argparse
import json

import pytest

from openreading.artifacts.limits import ArtifactError


def test_docling_setup_file_has_no_implicit_resource_defaults(tmp_path):
    from openreading.mcp_server.main import profile_config

    path = tmp_path / "profile.json"
    args = argparse.Namespace(
        profile="local-document-proof-v2",
        profile_config=path,
        input_root=tmp_path / "input",
        artifact_root=tmp_path / "store",
    )
    path.write_text(json.dumps({"docling": {"artifacts_path": str(tmp_path / "models")}}))
    with pytest.raises(ArtifactError, match="configuration_required"):
        profile_config(args)
    value = {
        "pages": 10,
        "deadline_seconds": 30,
        "worker_memory_bytes": 2 * 1024**3,
        "worker_idle_seconds": 60,
        "docling": {"artifacts_path": str(tmp_path / "models")},
    }
    path.write_text(json.dumps(value))
    result = profile_config(args)
    assert result.limits.pages == 10
    assert result.docling.ocr is False
    value["backend"] = "pymupdf"
    path.write_text(json.dumps(value))
    with pytest.raises(ArtifactError, match="configuration_required"):
        profile_config(args)
