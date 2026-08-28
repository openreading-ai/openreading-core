"""Live-path bug fixes (GOAL2 7.1) — regression guards, all offline via fakes. These paths were
never executed before v0.2, so the audit found latent bugs: async POLL read a None client, textract
async had no default S3 uploader, DocAI ignored the processor region, reducto treated an async
Extract (which has no async endpoint) as a job. Each fix is pinned here."""

from __future__ import annotations

import base64

import pytest

from openreading.credentials import EnvCredentialBroker, build_run_context
from openreading.router.clock import FakeClock
from openreading.router.driver import run_to_completion
from openreading.types import JobState
from openreading.types.enums import WaitMode
from openreading.types.errors import TerminalError
from openreading.types.request import OpenReadingRequest

_PDF_B64 = base64.b64encode(b"%PDF-1.7 fake").decode()


def _bcx(adapter, req, env=None):
    return build_run_context(req, adapter.descriptor, broker=EnvCredentialBroker(env or {}))


# --- G8: async POLL reaches the ctx-built client (not self._client=None) ----------------


def test_textract_async_poll_uses_ctx_bound_client_not_none():
    from openreading.adapters.aws_textract import AWSTextractAdapter
    from tests.test_aws_textract import FakeS3, FakeTextractClient

    page = {
        "JobStatus": "SUCCEEDED",
        "Blocks": [{"BlockType": "PAGE", "Id": "p1"}],
        "DocumentMetadata": {"Pages": 1},
    }
    adapter = AWSTextractAdapter()  # NO injected client → the real path would set self._client=None
    adapter._get_client = lambda ctx: FakeTextractClient(get_pages=[page])  # simulate ctx-built
    adapter._s3 = FakeS3()
    req = OpenReadingRequest.model_validate(
        {
            "document": {"bytes_base64": _PDF_B64, "mime_type": "application/pdf"},
            "backend": {"id": "aws-textract", "operation": "AnalyzeDocument"},
            "async": {"mode": "async"},
        }
    )
    job = adapter.submit(req, _bcx(adapter, req))
    assert adapter._client is None and job.wait_mode is WaitMode.POLL
    clock = FakeClock()
    job = run_to_completion(
        adapter, job, ctx=_bcx(adapter, req), deadline_ms=clock.now_ms() + 60_000, clock=clock
    )
    # poll() calls the monkeypatched self._get_client(ctx) fresh on every call (Ledger T4a: no
    # client is ever cached on self) — this is what proves the ctx-built path is actually reached,
    # not a None self._client silently no-opping.
    assert job.state is JobState.SUCCEEDED


# --- G9: textract async needs an S3 bucket; the error names the exact env var -----------


def test_textract_async_without_s3_bucket_names_the_env_var():
    from openreading.adapters.aws_textract import AWSTextractAdapter
    from tests.test_aws_textract import FakeTextractClient

    adapter = AWSTextractAdapter()  # no injected s3, no OPENREADING_TEXTRACT_S3_BUCKET
    adapter._get_client = lambda ctx: FakeTextractClient()
    req = OpenReadingRequest.model_validate(
        {
            "document": {"bytes_base64": _PDF_B64, "mime_type": "application/pdf"},
            "backend": {"id": "aws-textract", "operation": "AnalyzeDocument"},
            "async": {"mode": "async"},
        }
    )
    with pytest.raises(TerminalError) as exc:
        adapter.submit(req, _bcx(adapter, req))
    assert exc.value.backend_code == "no_s3_bucket"
    assert "OPENREADING_TEXTRACT_S3_BUCKET" in str(exc.value)


def test_textract_get_s3_returns_injected_uploader():
    from openreading.adapters.aws_textract import AWSTextractAdapter
    from tests.test_aws_textract import FakeS3

    s3 = FakeS3()
    adapter = AWSTextractAdapter(s3=s3)
    req = OpenReadingRequest.model_validate(
        {"document": {"path": "/x.pdf"}, "backend": {"id": "aws-textract"}}
    )
    assert adapter._get_s3(_bcx(adapter, req)) is s3


# --- G10: DocAI regional endpoint -------------------------------------------------------


@pytest.mark.parametrize(
    "location,expected",
    [
        ("us", None),
        (None, None),
        ("", None),
        ("eu", "eu-documentai.googleapis.com"),
        ("europe-west2", "europe-west2-documentai.googleapis.com"),
        ("asia-south1", "asia-south1-documentai.googleapis.com"),
    ],
)
def test_docai_regional_endpoint(location, expected):
    from openreading.adapters.google_document_ai.adapter import _regional_endpoint

    assert _regional_endpoint(location) == expected


# --- reducto async Extract runs sync (no async Extract endpoint) ------------------------


def test_reducto_async_extract_runs_inline_not_poll():
    from openreading.adapters.reducto import ReductoAdapter
    from tests.test_reducto import FakeReductoClient

    adapter = ReductoAdapter(client=FakeReductoClient())
    req = OpenReadingRequest.model_validate(
        {
            "document": {"url": "https://x/doc.pdf"},
            "backend": {"id": "reducto", "operation": "extract"},
            "extraction_schema": {"json_schema": {"type": "object"}},
            "async": {"mode": "async"},
        }
    )
    job = adapter.submit(req, _bcx(adapter, req, {"REDUCTO_API_KEY": "k"}))
    # extract can't be async → runs synchronously to a full result, not a job with no job_id
    assert job.wait_mode is WaitMode.INLINE and job.state is JobState.SUCCEEDED


def test_reducto_async_parse_still_polls():
    from openreading.adapters.reducto import ReductoAdapter
    from tests.test_reducto import FakeReductoClient

    adapter = ReductoAdapter(client=FakeReductoClient())
    req = OpenReadingRequest.model_validate(
        {
            "document": {"url": "https://x/doc.pdf"},
            "backend": {"id": "reducto", "operation": "parse"},
            "async": {"mode": "async"},
        }
    )
    job = adapter.submit(req, _bcx(adapter, req, {"REDUCTO_API_KEY": "k"}))
    assert job.wait_mode is WaitMode.POLL  # parse HAS /parse_async


# --- per-run credential-leak: the ctx-built client rebinds every submit -----------------


def test_ctx_built_client_never_cached_no_cross_run_leak():
    # Ledger T4a (R2/AC-7): _get_client(ctx) is called fresh on every submit() and NOTHING on
    # `self` ever caches what it returns — the old `_active_client`-rebinding story this test used
    # to tell (rebound each submit, so a later run's client never observed an earlier one) no
    # longer applies: there is nothing left to rebind, because nothing is ever bound in the first
    # place. Proven two ways: the job each submit() produces reflects THAT submit's own key (not a
    # stale one), and no `*_client` attribute (other than the constructor-injected `_client`, which
    # stays None here) ever appears on the instance.
    from openreading.adapters.reducto import ReductoAdapter

    class _Recording:
        def __init__(self, tag: str) -> None:
            self.tag = tag

        def parse(self, document, options, is_async):
            return {"result": {"served_by": self.tag}}

        def extract(self, document, schema, is_async):
            return {"result": {"served_by": self.tag}}

    adapter = ReductoAdapter()  # no injected client
    adapter._get_client = lambda ctx: _Recording(ctx.credentials.values["api_key"])

    def _no_cached_client() -> bool:
        # `callable(value)` excludes the monkey-patched `_get_client` bound above (a method
        # reference, not a cached client object) — it also happens to end in "_client".
        return not any(
            name != "_client"
            and name.endswith("_client")
            and value is not None
            and not callable(value)
            for name, value in vars(adapter).items()
        )

    def _submit_with(key: str):
        req = OpenReadingRequest.model_validate(
            {"document": {"url": "https://x/d.pdf"}, "backend": {"id": "reducto"}}
        )
        return adapter.submit(req, _bcx(adapter, req, {"REDUCTO_API_KEY": key}))

    job_a = _submit_with("KEY_A")
    assert job_a.raw.payload["result"]["served_by"] == "KEY_A"
    assert _no_cached_client()
    job_b = _submit_with("KEY_B")
    # KEY_A's client did not survive into the KEY_B run — there is nothing on self to leak from.
    assert job_b.raw.payload["result"]["served_by"] == "KEY_B"
    assert _no_cached_client()
