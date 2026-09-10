"""Docling resource settings must be explicit instead of inheriting PyMuPDF defaults."""

import pytest

from openreading.adapters.docling_local.config import LocalDoclingConfig
from openreading.artifacts.limits import ArtifactError, ProfileConfig, ProfileLimits


def test_docling_profile_refuses_unmeasured_legacy_defaults(tmp_path):
    with pytest.raises(ArtifactError, match="configuration_required"):
        ProfileConfig(
            tmp_path / "input",
            tmp_path / "store",
            limits=ProfileLimits(worker_memory_bytes=1024**3),
            docling=LocalDoclingConfig(tmp_path / "models"),
        )


def test_docling_limits_require_all_measured_fields_and_finite_values():
    from openreading.artifacts.limits import DoclingLimits

    with pytest.raises(TypeError):
        DoclingLimits()
    for value in [float("nan"), float("inf"), -1, 0, True]:
        with pytest.raises(ValueError):
            DoclingLimits(
                pages=10,
                deadline_seconds=value,
                worker_memory_bytes=1024**3,
                worker_idle_seconds=60,
            )
