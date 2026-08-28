"""AWS Textract adapter — fault injection for the branches the happy-path fixtures never reach,
centred on `_map_error`: the one function deciding auth_rejected vs RetryableError vs
TerminalError, and therefore whether the router retries this backend, falls back, or gives up for
good. Every branch is pinned — each code in `_AUTH_CODES`, `_RETRYABLE_CODES` and `_TERMINAL_CODES`
(parametrized off the sets themselves, so a code added to one is exercised rather than shipping
untested), the botocore ClientError envelope the real code arrives in, an already-typed error
passed through unchanged, and an unmapped exception falling through to TerminalError — plus the
submit/poll callers that reach it, the intake guards, and the normalize paths the captured
fixtures skip. All offline via injected fakes; no AWS, no credentials."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest

from openreading.adapters.aws_textract import AWSTextractAdapter
from openreading.adapters.aws_textract.adapter import (
    _AUTH_CODES,
    _RETRYABLE_CODES,
    _TERMINAL_CODES,
)
from openreading.types import BlockType, JobState, WaitMode
from openreading.types.enums import TextType
from openreading.types.errors import MissingCredentialsError, RetryableError, TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RawResult, RunContext

FIX = Path(__file__).parent / "fixtures" / "aws-textract"
DOC_B64 = base64.b64encode(b"%PDF-1.7 fake").decode()


def _fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


def _req(op: str = "AnalyzeDocument", **over) -> OpenReadingRequest:
    body = {
        "document": {"bytes_base64": DOC_B64, "mime_type": "application/pdf"},
        "backend": {"id": "aws-textract", "type": "hosted_api", "operation": op},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


class _ScriptedClient:
    """The Textract client methods the adapter calls. `sync` and `start` may be an Exception (it is
    raised); `polls` walks a scripted list of Get* responses, holding on the last entry. Records
    every call's kwargs so the intake tests can assert what actually reached the API."""

    def __init__(self, *, sync=None, start=None, polls=None) -> None:
        self._sync = sync
        self._start = start
        self._polls = list(polls or [])
        self._i = 0
        self.last_kwargs: dict = {}

    @staticmethod
    def _emit(value):
        if isinstance(value, Exception):
            raise value
        return value

    def _synced(self, kw):
        self.last_kwargs = kw
        return self._emit(self._sync if self._sync is not None else _fixture("analyze_document"))

    def _started(self, kw):
        self.last_kwargs = kw
        return self._emit(self._start if self._start is not None else {"JobId": "job-1"})

    def _polled(self, kw):
        self.last_kwargs = kw
        item = self._polls[min(self._i, len(self._polls) - 1)]
        self._i += 1
        return self._emit(item)

    def detect_document_text(self, **kw):
        return self._synced(kw)

    def analyze_document(self, **kw):
        return self._synced(kw)

    def analyze_expense(self, **kw):
        return self._synced(kw)

    def analyze_id(self, **kw):
        return self._synced(kw)

    def start_document_analysis(self, **kw):
        return self._started(kw)

    def start_document_text_detection(self, **kw):
        return self._started(kw)

    def start_lending_analysis(self, **kw):
        return self._started(kw)

    def get_document_analysis(self, **kw):
        return self._polled(kw)

    def get_document_text_detection(self, **kw):
        return self._polled(kw)

    def get_lending_analysis(self, **kw):
        return self._polled(kw)


class _FakeS3:
    def __init__(self) -> None:
        self.uploaded: bytes | None = None

    def upload(self, data: bytes, key: str) -> dict:
        self.uploaded = data
        return {"Bucket": "test-bucket", "Name": key}


class _ClientError(Exception):
    """botocore's ClientError shape: the real code lives under response['Error']['Code'], not on
    the exception's own attributes."""

    def __init__(self, response: dict, message: str = "the service said no") -> None:
        super().__init__(message)
        self.response = response


def _coded(code: str, message: str = "the service said no") -> Exception:
    """A boto exception class named after the AWS error code — how botocore surfaces modelled
    service exceptions (`client.exceptions.ThrottlingException`)."""
    return type(code, (Exception,), {})(message)


# ---- _map_error: the retry-vs-give-up decision --------------------------------------------------


@pytest.mark.parametrize("code", sorted(_AUTH_CODES))
def test_every_auth_code_maps_to_a_terminal_auth_rejected(code):
    # Parametrized off _AUTH_CODES itself: a code added to the set is exercised here automatically.
    # auth_rejected is a distinct terminal signal — a bad IAM identity must never be retried.
    mapped = AWSTextractAdapter()._map_error(_coded(code))
    assert isinstance(mapped, TerminalError) and mapped.backend_code == "auth_rejected"


@pytest.mark.parametrize("code", sorted(_RETRYABLE_CODES))
def test_every_retryable_code_maps_to_retryable(code):
    mapped = AWSTextractAdapter()._map_error(_coded(code))
    assert isinstance(mapped, RetryableError)
    assert mapped.backend_code == code and mapped.retry_after is None


@pytest.mark.parametrize("code", sorted(_TERMINAL_CODES))
def test_every_terminal_code_maps_to_terminal_keeping_the_aws_code(code):
    mapped = AWSTextractAdapter()._map_error(_coded(code))
    assert isinstance(mapped, TerminalError) and mapped.backend_code == code


def test_the_three_code_sets_are_disjoint():
    # _map_error checks auth → retryable → terminal in order, so a code listed in two sets would
    # be silently decided by that order rather than by intent.
    assert not _AUTH_CODES & _RETRYABLE_CODES
    assert not _AUTH_CODES & _TERMINAL_CODES
    assert not _RETRYABLE_CODES & _TERMINAL_CODES


def test_clienterror_envelope_code_wins_over_the_exception_type_name():
    err = _ClientError({"Error": {"Code": "ThrottlingException"}})
    mapped = AWSTextractAdapter()._map_error(err)
    assert isinstance(mapped, RetryableError) and mapped.backend_code == "ThrottlingException"


def test_clienterror_without_an_error_member_falls_back_to_the_type_name():
    err = _ClientError({"ResponseMetadata": {"HTTPStatusCode": 500}})
    mapped = AWSTextractAdapter()._map_error(err)
    assert isinstance(mapped, TerminalError) and mapped.backend_code == "_ClientError"


def test_unmapped_exception_falls_through_to_terminal():
    mapped = AWSTextractAdapter()._map_error(ValueError("boom"))
    assert isinstance(mapped, TerminalError)
    assert mapped.backend_code == "ValueError"  # no code attribute → the exception type name
    assert str(mapped) == "boom"


@pytest.mark.parametrize(
    "err",
    [
        RetryableError("throttled", backend_code="ThrottlingException"),
        TerminalError("no S3 bucket configured", backend_code="no_s3_bucket"),
    ],
    ids=["retryable", "terminal"],
)
def test_already_typed_errors_pass_through_unchanged(err):
    # The adapter raises its own taxonomy errors from inside the try blocks (intake and S3 guards),
    # so _map_error must hand them back untouched rather than reclassifying them.
    assert AWSTextractAdapter()._map_error(err) is err


# ---- real-client construction (_get_client, no injected client) --------------------------------


def test_get_client_raises_missing_credentials_with_no_region(monkeypatch, tmp_path):
    """QA closing pass (ui-app): `boto3.client("textract")` needs a region one way or another; when
    boto3 itself can't resolve one anywhere (no AWS_REGION/AWS_DEFAULT_REGION, no ~/.aws/config
    default, no ambient IMDS role) it previously escaped `submit()` as a raw, uncaught
    `NoRegionError` ("You must specify a region.") — an unnamed "Backend error" instead of the
    named MissingCredentialsError panel every correctly-required adapter gets (Ive's/Karri's QA:
    "aws-textract renders a bare Backend error with 'You must specify a region.'"). `_get_client`
    must now convert boto3's own genuine "nothing resolved" signal into that same named error —
    credentials_spec/config_spec stay honestly optional (a real ambient boto3 chain, e.g. an IAM
    instance role, is a legitimate path this adapter never needs the broker to see); only boto3
    itself confirming failure triggers this.

    Points AWS_CONFIG_FILE/AWS_SHARED_CREDENTIALS_FILE at empty paths so this is deterministic
    regardless of the machine running the suite — a real `~/.aws/config` with a default region
    would otherwise let `boto3.client()` succeed and this test would flake."""
    for var in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "AWS_PROFILE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "no-such-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "no-such-credentials"))
    with pytest.raises(MissingCredentialsError) as exc:
        AWSTextractAdapter()._get_client(RunContext())
    assert exc.value.missing == ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"]
    assert "region" in str(exc.value).lower()


# ---- the callers that reach _map_error ----------------------------------------------------------


def test_sync_submit_maps_a_service_exception():
    adapter = AWSTextractAdapter(client=_ScriptedClient(sync=_coded("ThrottlingException")))
    with pytest.raises(RetryableError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value.backend_code == "ThrottlingException"


def test_async_start_failure_is_mapped():
    adapter = AWSTextractAdapter(
        client=_ScriptedClient(start=_coded("LimitExceededException")), s3=_FakeS3()
    )
    with pytest.raises(RetryableError) as exc:
        adapter.submit(_req(**{"async": {"mode": "async"}}), RunContext())
    assert exc.value.backend_code == "LimitExceededException"


def test_poll_failure_is_mapped():
    client = _ScriptedClient(polls=[_coded("InvalidS3ObjectException")])
    adapter = AWSTextractAdapter(client=client, s3=_FakeS3())
    job = adapter.submit(_req(**{"async": {"mode": "async"}}), RunContext())
    with pytest.raises(TerminalError) as exc:
        adapter.poll(job, RunContext())
    assert exc.value.backend_code == "InvalidS3ObjectException"


def test_poll_failed_job_status_is_terminal():
    client = _ScriptedClient(polls=[{"JobStatus": "FAILED"}])
    adapter = AWSTextractAdapter(client=client, s3=_FakeS3())
    job = adapter.submit(_req(**{"async": {"mode": "async"}}), RunContext())
    with pytest.raises(TerminalError) as exc:
        adapter.poll(job, RunContext())
    assert exc.value.backend_code == "FAILED"


# ---- operation and intake guards ----------------------------------------------------------------


def test_unknown_operation_is_rejected_before_any_call():
    client = _ScriptedClient()
    with pytest.raises(TerminalError) as exc:
        AWSTextractAdapter(client=client).submit(_req(op="AnalyzeMortgage"), RunContext())
    assert exc.value.backend_code == "InvalidOperation"
    assert client.last_kwargs == {}  # rejected before the client was touched


def test_lending_without_async_is_rejected_as_async_only():
    # AnalyzeLending is a Start*/Get* API only; a sync request for it must fail loudly rather than
    # silently running some other analysis.
    adapter = AWSTextractAdapter(client=_ScriptedClient())
    with pytest.raises(TerminalError, match="async-only") as exc:
        adapter.submit(_req(op="AnalyzeLending"), RunContext())
    assert exc.value.backend_code == "InvalidOperation"


def test_sync_path_input_is_read_from_disk(tmp_path):
    pdf = tmp_path / "loan.pdf"
    pdf.write_bytes(b"%PDF-1.7 on disk")
    client = _ScriptedClient()
    doc = {"path": str(pdf), "mime_type": "application/pdf"}
    AWSTextractAdapter(client=client).submit(_req(document=doc), RunContext())
    assert client.last_kwargs["Document"] == {"Bytes": b"%PDF-1.7 on disk"}


def test_url_input_is_unsupported():
    adapter = AWSTextractAdapter(client=_ScriptedClient())
    doc = {"url": "https://example.test/loan.pdf", "mime_type": "application/pdf"}
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(document=doc), RunContext())
    assert exc.value.backend_code == "unsupported_input"


def test_async_path_input_is_staged_to_s3_from_disk(tmp_path):
    pdf = tmp_path / "loan.pdf"
    pdf.write_bytes(b"%PDF-1.7 staged")
    s3 = _FakeS3()
    doc = {"path": str(pdf), "mime_type": "application/pdf"}
    adapter = AWSTextractAdapter(client=_ScriptedClient(), s3=s3)
    job = adapter.submit(_req(document=doc, **{"async": {"mode": "async"}}), RunContext())
    assert s3.uploaded == b"%PDF-1.7 staged"
    assert job.state is JobState.RUNNING and job.backend_job_id == "job-1"


# ---- BL-165: the S3 key must be a pure function of the document's bytes -------------------------
#
# `_submit_async` used to key the S3 upload with a fresh uuid4 on every attempt. A retried submit
# then re-uploaded to a *new* object and presented the *same* ClientRequestToken with a *different*
# DocumentLocation — AWS's real behaviour for that mismatch is IdempotentParameterMismatchException,
# not a dedupe. `_IdempotencyAwareClient` below reproduces that vendor rule directly: a repeated
# token must arrive with byte-identical kwargs, or it raises.


class _IdempotencyAwareClient:
    """A single async Start* call, honest about AWS's own idempotency contract: the same
    ClientRequestToken presented twice must carry identical request kwargs, or the vendor raises
    IdempotentParameterMismatchException — it does not deduplicate a mismatched retry."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._by_token: dict[str, dict] = {}

    def start_document_analysis(self, **kw):
        self.calls.append(kw)
        token = kw["ClientRequestToken"]
        prior = self._by_token.get(token)
        if prior is not None and prior != kw:
            raise _coded("IdempotentParameterMismatchException")
        self._by_token[token] = kw
        return {"JobId": "job-1"}


def test_retried_async_submit_with_an_identical_token_does_not_hit_the_vendor_mismatch(tmp_path):
    pdf = tmp_path / "loan.pdf"
    pdf.write_bytes(b"%PDF-1.7 retried")
    s3 = _FakeS3()
    client = _IdempotencyAwareClient()
    doc = {"path": str(pdf), "mime_type": "application/pdf"}
    req = _req(document=doc, **{"async": {"mode": "async"}})
    ctx = RunContext(idempotency_key="fixed-token")
    adapter = AWSTextractAdapter(client=client, s3=s3)

    job1 = adapter.submit(req, ctx)
    job2 = adapter.submit(req, ctx)  # a retried submit after e.g. a client-side timeout

    assert job1.backend_job_id == "job-1"
    assert job2.backend_job_id == "job-1"
    assert len(client.calls) == 2
    assert client.calls[0]["ClientRequestToken"] == "fixed-token"
    assert client.calls[0] == client.calls[1]  # byte-identical kwargs, including DocumentLocation


def test_same_document_bytes_yield_the_same_s3_key_across_two_submits():
    s3 = _FakeS3()
    client = _ScriptedClient()
    adapter = AWSTextractAdapter(client=client, s3=s3)
    doc = {"bytes_base64": DOC_B64, "mime_type": "application/pdf"}
    req = _req(document=doc, **{"async": {"mode": "async"}})

    adapter.submit(req, RunContext())
    key1 = client.last_kwargs["DocumentLocation"]["S3Object"]["Name"]
    adapter.submit(req, RunContext())
    key2 = client.last_kwargs["DocumentLocation"]["S3Object"]["Name"]

    assert key1 == key2 == f"openreading/{hashlib.sha256(b'%PDF-1.7 fake').hexdigest()}.pdf"


def test_distinct_documents_get_distinct_s3_keys():
    s3 = _FakeS3()
    client = _ScriptedClient()
    adapter = AWSTextractAdapter(client=client, s3=s3)

    doc_a = {
        "bytes_base64": base64.b64encode(b"document A").decode(),
        "mime_type": "application/pdf",
    }
    adapter.submit(_req(document=doc_a, **{"async": {"mode": "async"}}), RunContext())
    key_a = client.last_kwargs["DocumentLocation"]["S3Object"]["Name"]

    doc_b = {
        "bytes_base64": base64.b64encode(b"document B").decode(),
        "mime_type": "application/pdf",
    }
    adapter.submit(_req(document=doc_b, **{"async": {"mode": "async"}}), RunContext())
    key_b = client.last_kwargs["DocumentLocation"]["S3Object"]["Name"]

    assert key_a != key_b


def test_health_reports_the_missing_extra_when_boto3_is_absent(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _no_boto3(name, *args, **kwargs):
        if name == "boto3":
            raise ImportError("No module named 'boto3'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_boto3)
    health = AWSTextractAdapter().health()
    assert not health.ready and any("boto3" in dep for dep in health.missing_deps)


# ---- normalize paths the captured fixtures skip -------------------------------------------------


def _normalize_raw(payload: dict, op: str):
    adapter = AWSTextractAdapter()
    job = adapter.new_job(WaitMode.INLINE, state=JobState.SUCCEEDED)
    job.raw = RawResult(payload=payload, object_class=op, encoding="json")
    return adapter.normalize(job, RunContext(), _req(op=op))


def _line(id_: str, text: str, *, text_type: str | None = None, geometry: dict | None = None):
    block = {
        "BlockType": "LINE",
        "Id": id_,
        "Page": 1,
        "Text": text,
        "Confidence": 98.0,
        "Geometry": geometry
        if geometry is not None
        else {"BoundingBox": {"Left": 0.1, "Top": 0.1, "Width": 0.5, "Height": 0.03}},
    }
    if text_type is not None:
        block["TextType"] = text_type
    return block


def test_line_only_payload_is_normalized_when_no_layout_blocks_exist():
    # DetectDocumentText returns LINE/WORD only — no LAYOUT_* blocks — so the whole LINE branch is
    # dead for the AnalyzeDocument fixtures and only this shape exercises it.
    payload = {
        "DocumentMetadata": {"Pages": 1},
        "Blocks": [
            {"BlockType": "PAGE", "Id": "p1", "Page": 1},
            _line("l1", "Loan Application", text_type="PRINTED"),
            _line("l2", "Jane Doe", text_type="HANDWRITING"),
            _line("l3", "no text type given"),
        ],
    }
    resp = _normalize_raw(payload, "DetectDocumentText")
    blocks = resp.document.pages[0].blocks
    assert [b.type for b in blocks] == [BlockType.TEXT] * 3
    assert [b.text_type for b in blocks] == [TextType.PRINTED, TextType.HANDWRITING, None]
    assert resp.document.text == "Loan Application\nJane Doe\nno text type given"
    assert blocks[0].confidence == pytest.approx(0.98)  # 98.0/100


def test_geometry_without_a_bounding_box_yields_no_bbox():
    payload = {
        "DocumentMetadata": {"Pages": 1},
        "Blocks": [_line("l1", "Loan Application", geometry={"Polygon": [{"X": 0.1, "Y": 0.1}]})],
    }
    blk = _normalize_raw(payload, "DetectDocumentText").document.pages[0].blocks[0]
    assert blk.bbox is None and blk.text == "Loan Application"  # geometry-less, never invented


def test_section_header_renders_as_a_level_two_markdown_heading():
    payload = {
        "DocumentMetadata": {"Pages": 1},
        "Blocks": [
            {
                "BlockType": "LAYOUT_SECTION_HEADER",
                "Id": "sh",
                "Page": 1,
                "Confidence": 99.0,
                "Geometry": {
                    "BoundingBox": {"Left": 0.1, "Top": 0.2, "Width": 0.4, "Height": 0.03}
                },
                "Relationships": [{"Type": "CHILD", "Ids": ["w1"]}],
            },
            {"BlockType": "WORD", "Id": "w1", "Page": 1, "Text": "Income", "Confidence": 99.0},
        ],
    }
    resp = _normalize_raw(payload, "AnalyzeDocument")
    assert resp.document.pages[0].blocks[0].type is BlockType.SECTION_HEADER
    assert resp.document.markdown == "## Income"


def test_expense_field_without_a_label_is_skipped():
    payload = {
        "DocumentMetadata": {"Pages": 1},
        "ExpenseDocuments": [
            {
                "SummaryFields": [
                    {"ValueDetection": {"Text": "128.50", "Confidence": 99.0}},  # no Type/Label
                    {
                        "Type": {"Text": "TOTAL"},
                        "ValueDetection": {"Text": "128.50", "Confidence": 99.0},
                    },
                ]
            }
        ],
    }
    resp = _normalize_raw(payload, "AnalyzeExpense")
    assert set(resp.typed_fields) == {"TOTAL"}  # the unlabelled field is dropped, never keyed ""
