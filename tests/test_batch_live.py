"""Phase B (Manifest v0.6) — live directory-batch sanity (keyed lane; skipped without keys).

One real 2-file directory batch per keyed backend (§11/§12): the whole pipeline against the actual
provider must yield a schema-valid batch envelope with 2 succeeded items. Never runs in
`make verify` (marked live); `tests/conftest.py` loads .env under `-m live`."""

from __future__ import annotations

import pytest

from openreading import run_batch, schemas
from openreading.testing.sample_pdf import build_sample_pdf


def _corpus(tmp_path):
    d = tmp_path / "corpus"
    d.mkdir()
    pdf = build_sample_pdf()
    (d / "doc1.pdf").write_bytes(pdf)
    (d / "doc2.pdf").write_bytes(pdf)
    return d


@pytest.mark.live
@pytest.mark.parametrize("backend", ["reducto", "pulse"])
def test_live_directory_batch(backend, tmp_path):  # pragma: no cover
    from tests.live_helpers import skip_unless_creds

    skip_unless_creds(backend)
    env = run_batch([str(_corpus(tmp_path))], backend=backend, jobs=2)
    schemas.validate_batch_result(env)
    assert env["status"]["state"] == "succeeded"
    assert env["summary"]["succeeded"] == 2 and env["summary"]["failed"] == 0
    assert env["summary"]["backends"] == {backend: 2}
    for item in env["items"]:
        assert item["state"] == "succeeded"
        schemas.validate_response(item["response"])  # M9: each item is a full response envelope
