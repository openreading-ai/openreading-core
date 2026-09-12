"""A second process cannot clear staging or import while another process owns the store."""

import json
import select
import subprocess
import sys

import pytest

from openreading.artifacts.limits import ArtifactError
from tests.test_artifact_service import pdf
from tests.test_artifact_service import service as service_fixture

service = service_fixture


def test_concurrent_import_preserves_active_staging_and_reuses_committed_artifact(service):
    pdf(service.config.input_root / "test.pdf")
    script = """
import json, sys
from pathlib import Path
from openreading.artifacts.limits import ProfileConfig
from openreading.artifacts.service import ArtifactService
service = ArtifactService(ProfileConfig(Path(sys.argv[1]), Path(sys.argv[2])))
original = service._worker
def pause(*args):
    print('staged', flush=True)
    sys.stdin.readline()
    return original(*args)
service._worker = pause
print(json.dumps(service.import_document('test.pdf').wire()), flush=True)
"""
    with subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(service.config.input_root),
            str(service.config.artifact_root),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        try:
            assert select.select([process.stdout], [], [], 10)[0]
            assert process.stdout.readline().strip() == "staged"
            staged = list((service.config.artifact_root / "staging").glob("*/source.pdf"))
            assert len(staged) == 1
            before = staged[0].read_bytes()
            with pytest.raises(ArtifactError, match="busy"):
                service.import_document("test.pdf")
            assert staged[0].read_bytes() == before
            output, errors = process.communicate("continue\n", timeout=15)
            assert process.returncode == 0, errors
            receipt = json.loads(output)
            assert service.import_document("test.pdf").artifact_id == receipt["artifact_id"]
            assert not list((service.config.artifact_root / "staging").iterdir())
            assert len(list(service.store.documents.glob("*/manifest.json"))) == 1
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
