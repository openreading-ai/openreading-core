"""AWS Textract adapter — the first HTTP async-POLL adapter. Reassembles Textract's flat,
ID-linked Block graph (P5) into the normalized reading-order spine, and maps the typed APIs
(Expense/ID/Lending) into typed_fields. BYO-IAM (SigV4) → the charge lands on the caller's own
AWS account (pure pass-through).

boto3 is isolated in the `textract` extra and imported lazily. Two ports keep the adapter
testable without AWS: `TextractClient` (the ten Textract calls this adapter makes, four sync
analyses plus a Start/Get pair for each of the three async ones) and `S3Uploader` (async
needs the doc in S3). Tests inject fakes that replay real captured Block-graph fixtures.

Geometry: Textract is normalized 0-1 top-left, so its BoundingBox is already canonical. It still
goes through to_canonical (unit=normalized, page dims 1.0) so bbox_native is recorded and the
convention stays uniform. Confidence is 0-100 → /100. AnalyzeID carries no Geometry.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from typing import Any, Protocol, cast

from openreading.adapters.base import BackendAdapter
from openreading.derive import GridCell, cells_to_grid, table_to_pipe_md, table_to_text
from openreading.types.blocks import Block, Citation, TypedField
from openreading.types.cost import CostReport
from openreading.types.descriptor import (
    AdapterDescriptor,
    Capabilities,
    ConfigField,
    CredentialField,
    Output,
    OutputChannels,
    Provisioning,
    RouterHints,
    RuntimeProfile,
    Source,
)
from openreading.types.enums import (
    BackendType,
    BlockType,
    ChannelGrade,
    JobState,
    NativeOrigin,
    NativeUnit,
    OutputParadigm,
    ResponseState,
    TextType,
    WaitMode,
)
from openreading.types.errors import MissingCredentialsError, RetryableError, TerminalError
from openreading.types.geometry import to_canonical
from openreading.types.job import Job
from openreading.types.request import OpenReadingRequest, Outputs
from openreading.types.response import (
    BackendInfo,
    BackendRaw,
    DocType,
    Document,
    NormalizedResponse,
    Page,
    Status,
)
from openreading.types.runtime import Health, RawResult, RunContext


def _conf(v: float | None) -> float | None:
    """Textract confidence 0-100 -> normalized [0,1]; None stays None (never fabricated)."""
    return v / 100.0 if v is not None else None


X = ChannelGrade.IMPOSSIBLE
N = ChannelGrade.NATIVE
D = ChannelGrade.DERIVABLE

# Per-operation feature -> Textract FeatureTypes and the estimated $/page (aws-textract profile).
_OPERATIONS = {
    "DetectDocumentText": {"features": [], "price": 0.0015},
    "AnalyzeDocument": {"features": ["FORMS", "TABLES", "LAYOUT"], "price": 0.065},
    "AnalyzeExpense": {"features": [], "price": 0.01},
    "AnalyzeID": {"features": [], "price": 0.025},
    "AnalyzeLending": {"features": [], "price": 0.07},
}
_ASYNC_OPS = {"AnalyzeDocument", "DetectDocumentText", "AnalyzeLending"}

# Each async op has its OWN Start*/Get* client-method pair — routing them all through
# start/get_document_analysis (the old bug) runs the wrong AWS API for text-detection and lending.
_ASYNC_START = {
    "AnalyzeDocument": "start_document_analysis",
    "DetectDocumentText": "start_document_text_detection",
    "AnalyzeLending": "start_lending_analysis",
}
_ASYNC_GET = {
    "AnalyzeDocument": "get_document_analysis",
    "DetectDocumentText": "get_document_text_detection",
    "AnalyzeLending": "get_lending_analysis",
}

_RETRYABLE_CODES = {
    "ThrottlingException",
    "ProvisionedThroughputExceededException",
    "InternalServerError",
    "LimitExceededException",
}
_TERMINAL_CODES = {
    "BadDocumentException",
    "DocumentTooLargeException",
    "UnsupportedDocumentException",
    "InvalidS3ObjectException",
    "InvalidParameterException",
    "HumanLoopQuotaExceededException",
    # A real content mismatch under a reused ClientRequestToken (BL-165's own S3 key is now a
    # pure function of content, so this means the caller reused a key across genuinely different
    # documents) — not transient, retrying with the same token+content would fail identically.
    "IdempotentParameterMismatchException",
}
# IAM/SigV4 rejection → the key was found but is invalid/unauthorized (backend_code auth_rejected).
_AUTH_CODES = {
    "AccessDeniedException",
    "AccessDenied",
    "UnrecognizedClientException",
    "InvalidSignatureException",
    "InvalidClientTokenId",
    "ExpiredTokenException",
    "AuthFailure",
}

# LAYOUT_* BlockType -> normalized Block.type
_LAYOUT_MAP = {
    "LAYOUT_TITLE": BlockType.TITLE,
    "LAYOUT_SECTION_HEADER": BlockType.SECTION_HEADER,
    "LAYOUT_HEADER": BlockType.HEADER,
    "LAYOUT_FOOTER": BlockType.FOOTER,
    "LAYOUT_PAGE_NUMBER": BlockType.PAGE_NUMBER,
    "LAYOUT_LIST": BlockType.LIST,
    "LAYOUT_FIGURE": BlockType.FIGURE,
    "LAYOUT_KEY_VALUE": BlockType.KEY_VALUE,
    "LAYOUT_TEXT": BlockType.TEXT,
}


class TextractClient(Protocol):
    def detect_document_text(self, **kw) -> dict: ...
    def analyze_document(self, **kw) -> dict: ...
    def analyze_expense(self, **kw) -> dict: ...
    def analyze_id(self, **kw) -> dict: ...
    def start_document_analysis(self, **kw) -> dict: ...
    def get_document_analysis(self, **kw) -> dict: ...
    def start_document_text_detection(self, **kw) -> dict: ...
    def get_document_text_detection(self, **kw) -> dict: ...
    def start_lending_analysis(self, **kw) -> dict: ...
    def get_lending_analysis(self, **kw) -> dict: ...


class S3Uploader(Protocol):
    def upload(self, data: bytes, key: str) -> dict: ...  # -> {"Bucket":..., "Name":...}


class _DefaultS3Uploader:  # pragma: no cover - real AWS path
    """Built lazily for the async multi-page flow when no S3Uploader is injected. Bucket and region
    from config_spec (→ ctx.runtime); secrets from ctx.credentials."""

    def __init__(self, bucket: str, region: str | None, creds: dict) -> None:
        import boto3

        self._bucket = bucket
        self._s3 = boto3.client(
            "s3",
            region_name=region,
            aws_access_key_id=creds.get("aws_access_key_id"),
            aws_secret_access_key=creds.get("aws_secret_access_key"),
            aws_session_token=creds.get("aws_session_token"),
        )

    def upload(self, data: bytes, key: str) -> dict:
        self._s3.put_object(Bucket=self._bucket, Key=key, Body=data)
        return {"Bucket": self._bucket, "Name": key}


def _descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        id="aws-textract",
        type=BackendType.HOSTED_API,
        # Ledger T4a (AC-8): poll() builds its client fresh from ctx on every call (no
        # _active_client cached on self) — verified by R1/R2 conformance.
        protocol_version=2,
        adapter_impl="http",
        operations=list(_OPERATIONS),
        provisioning=Provisioning(byo_mode=["cloud_credential"], auth="sigv4"),
        wait_modes=[WaitMode.INLINE, WaitMode.POLL],
        capabilities=Capabilities(
            ocr="verified",
            handwriting="verified",
            printed_tables="verified",
            complex_tables="verified",
            forms_key_value="verified",
            layout="verified",
            reading_order="claimed",
            signatures="verified",
            classification="claimed",
            splitting="claimed",
            custom_schema_extraction="claimed",
            languages=["en", "fr", "de", "it", "pt", "es"],
            input_formats=["pdf", "png", "jpg", "tiff"],
            max_pages_per_request="1 sync / 3000 async",
        ),
        runtime=RuntimeProfile(
            offline_capable=False, license="proprietary", version_pin="boto3>=1.34"
        ),
        output=Output(
            paradigms=[OutputParadigm.BLOCK_GRAPH, OutputParadigm.TYPED_FIELDS],
            block_granularity="element",  # v0.3 hint (§4.3): LAYOUT/LINE/TABLE mixed elements
            channels=OutputChannels(
                markdown=D,
                text=D,
                blocks=N,
                block_bbox=N,
                block_confidence=N,
                typed_fields=N,
                table_cells=N,
            ),
        ),
        router=RouterHints(
            normalization_difficulty="high",
        ),
        credentials_spec=[
            CredentialField(
                key="aws_access_key_id",
                required=False,
                env=["AWS_ACCESS_KEY_ID"],
                description="IAM access key id; falls back to the ambient boto3 credential chain.",
            ),
            CredentialField(
                key="aws_secret_access_key", required=False, env=["AWS_SECRET_ACCESS_KEY"]
            ),
            CredentialField(key="aws_session_token", required=False, env=["AWS_SESSION_TOKEN"]),
        ],
        config_spec=[
            ConfigField(
                key="region",
                env=["AWS_REGION", "AWS_DEFAULT_REGION"],
                description="AWS region; falls back to the ambient boto3 chain when unset.",
                example="us-east-1",
            ),
            ConfigField(
                key="s3_bucket",
                env=["OPENREADING_TEXTRACT_S3_BUCKET"],
                description="bucket for the multi-page async POLL flow (required only for async).",
            ),
        ],
        signup_url="https://aws.amazon.com/textract/",
        accepts_url=False,
        live_gate_env=["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"],
        # ClientRequestToken IS sent by `_submit_async` below and, since BL-165, actually works: the S3
        # upload key is now sha256(content) rather than a fresh uuid4 each attempt, so a retry
        # presents the same token with the same DocumentLocation and AWS deduplicates instead of
        # answering IdempotentParameterMismatchException.
        idempotency_supported=True,
        # BL-164: checked the full Textract API operations list and the boto3 client reference —
        # 23 actions total (Start*/Get*/Analyze*/DetectDocumentText plus adapter/tag management),
        # none named Stop/Cancel/Terminate. Async jobs are fire-and-forget by design: Start* returns
        # a JobId, Get* polls it to a terminal state; there is no JobId-scoped stop primitive
        # anywhere in the API surface (unlike e.g. Amazon Transcribe's StopTranscriptionJob).
        # Honest false — a losing race branch's Textract job keeps running and billing at the
        # vendor regardless of what openreading does locally.
        cancel_supported=False,
        sources=[
            Source(
                url="https://docs.aws.amazon.com/textract/",
                accessed="2026-07-21",
                supports="Block schema, Relationships, 5 API families, error taxonomy, HIPAA eligibility",
            ),
            # BL-165's finding is deliberately uncited here. Its only write-up is in the company
            # repo, and `sources[]` ships to every caller through `GET /v1/backends`, where a
            # private path is a pointer nobody outside can follow. The reasoning behind
            # `idempotency_supported=True` is in the comment on that field instead.
            Source(
                url="https://docs.aws.amazon.com/textract/latest/dg/API_Operations.html",
                accessed="2026-08-22",
                supports="BL-164: full Textract API operations list — no Stop/Cancel action exists",
            ),
        ],
    )


class AWSTextractAdapter(BackendAdapter):
    def __init__(self, client: TextractClient | None = None, s3: S3Uploader | None = None) -> None:
        self.descriptor = _descriptor()
        self._client = client
        self._s3 = s3

    # ---- lifecycle -----------------------------------------------------------
    def health(self) -> Health:
        if self._client is not None:
            return Health(ready=True, detail="injected client")
        try:
            import boto3  # noqa: F401
        except ImportError:
            return Health(ready=False, missing_deps=["boto3 (pip install 'openreading[textract]')"])
        return Health(ready=True)

    def _get_client(self, ctx: RunContext) -> TextractClient:
        if self._client is not None:
            return self._client
        import boto3
        from botocore.exceptions import NoCredentialsError, NoRegionError, PartialCredentialsError

        creds = (ctx.credentials.values if ctx.credentials else {}) or {}
        try:
            # boto3's client is dynamically typed; we only ever call the TextractClient-protocol
            # methods on it, so cast to the protocol we depend on.
            return cast(  # pragma: no cover - real AWS path
                TextractClient,
                boto3.client(
                    "textract",
                    region_name=(ctx.runtime or {}).get("region"),
                    aws_access_key_id=creds.get("aws_access_key_id"),
                    aws_secret_access_key=creds.get("aws_secret_access_key"),
                    aws_session_token=creds.get("aws_session_token"),
                ),
            )
        except (NoRegionError, NoCredentialsError, PartialCredentialsError) as e:
            # credentials_spec/config_spec leave these fields optional on purpose — a real ambient
            # boto3 credential/region chain (IAM instance role, ~/.aws/config, SSO profile) is a
            # legitimate path this adapter never needs the broker to see. But when boto3 ITSELF
            # confirms nothing resolved (this is boto3's own authority speaking, not a guess), that
            # is a genuine missing-credentials state, not a raw SDK error to leak: `NoRegionError`
            # ("You must specify a region.") previously escaped straight out of submit() uncaught,
            # rendering an unnamed, code-less "Backend error" instead of the named
            # MissingCredentialsError panel every correctly-required adapter gets. Name it the same
            # honest way here, before any client call is attempted.
            signup = (
                f" Sign up / configure: {self.descriptor.signup_url}"
                if self.descriptor.signup_url
                else ""
            )
            raise MissingCredentialsError(
                "missing required credentials/config: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, "
                f"AWS_REGION ({e}). Set them directly or configure the ambient AWS credential "
                f"chain.{signup}",
                missing=["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION"],
            ) from e

    # ---- execution -----------------------------------------------------------
    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        op = req.backend.operation or "AnalyzeDocument"
        if op not in _OPERATIONS:
            raise TerminalError(
                f"unknown Textract operation {op!r}", backend_code="InvalidOperation"
            )
        client = self._get_client(ctx)
        want_async = (req.async_ is not None and req.async_.mode == "async") and op in _ASYNC_OPS

        if want_async:
            return self._submit_async(client, req, ctx, op)
        return self._submit_sync(client, req, ctx, op)

    def _get_s3(self, ctx: RunContext) -> S3Uploader:
        if self._s3 is not None:
            return self._s3
        runtime = ctx.runtime or {}
        bucket = runtime.get("s3_bucket")
        if not bucket:
            raise TerminalError(
                "async Textract needs an S3 bucket: set OPENREADING_TEXTRACT_S3_BUCKET "
                "(the multi-page async flow stages the document in your S3)",
                backend_code="no_s3_bucket",
            )
        creds = (ctx.credentials.values if ctx.credentials else {}) or {}
        return _DefaultS3Uploader(bucket, runtime.get("region"), creds)  # pragma: no cover

    def _document_arg(self, req: OpenReadingRequest) -> dict:
        d = req.document
        if d.bytes_base64:
            return {"Bytes": base64.b64decode(d.bytes_base64)}
        if d.path:
            with open(d.path, "rb") as fh:
                return {"Bytes": fh.read()}
        raise TerminalError(
            "Textract sync needs document bytes/path", backend_code="unsupported_input"
        )

    def _submit_sync(self, client, req, ctx, op) -> Job:
        doc = self._document_arg(req)
        feats = _OPERATIONS[op]["features"]
        try:
            if op == "DetectDocumentText":
                raw = client.detect_document_text(Document=doc)
            elif op == "AnalyzeDocument":
                raw = client.analyze_document(Document=doc, FeatureTypes=feats)
            elif op == "AnalyzeExpense":
                raw = client.analyze_expense(Document=doc)
            elif op == "AnalyzeID":
                raw = client.analyze_id(DocumentPages=[doc])
            else:
                raise TerminalError(f"{op} is async-only", backend_code="InvalidOperation")
        except Exception as e:  # noqa: BLE001
            raise self._map_error(e) from e
        job = self.new_job(
            WaitMode.INLINE, state=JobState.SUCCEEDED, idempotency_key=ctx.idempotency_key
        )
        job.raw = RawResult(
            payload=raw, media_type="application/json", encoding="json", object_class=op
        )
        return job

    def _submit_async(self, client, req, ctx, op) -> Job:
        s3 = self._get_s3(ctx)
        d = req.document
        if d.bytes_base64:
            data = base64.b64decode(d.bytes_base64)
        else:
            with open(d.path, "rb") as fh:
                data = fh.read()
        # The key must be a pure function of the document's bytes, not a fresh uuid4 per
        # attempt: a retried submit re-uploads to the same S3 object, so the same
        # ClientRequestToken always presents the same DocumentLocation (BL-165) — AWS answers
        # IdempotentParameterMismatchException, not a dedupe, when the two disagree.
        s3obj = s3.upload(data, key=f"openreading/{hashlib.sha256(data).hexdigest()}.pdf")
        token = ctx.idempotency_key or uuid.uuid4().hex
        kwargs: dict[str, Any] = {
            "DocumentLocation": {"S3Object": s3obj},
            "ClientRequestToken": token,
        }
        # Only StartDocumentAnalysis takes FeatureTypes; StartDocumentTextDetection and
        # StartLendingAnalysis are fixed analyses that reject a FeatureTypes argument.
        if op == "AnalyzeDocument":
            kwargs["FeatureTypes"] = _OPERATIONS[op]["features"]
        start = getattr(client, _ASYNC_START[op])
        try:
            resp = start(**kwargs)
        except Exception as e:  # noqa: BLE001
            raise self._map_error(e) from e
        job = self.new_job(
            WaitMode.POLL, state=JobState.RUNNING, idempotency_key=ctx.idempotency_key
        )
        job.backend_job_id = resp["JobId"]
        # AnalyzeLending paginates GetLendingAnalysis.Results; the others paginate Blocks.
        items_key = "Results" if op == "AnalyzeLending" else "Blocks"
        job.poll_handle = {
            "op": op,
            "items_key": items_key,
            "next_token": None,
            "items": [],
            "meta": None,
        }
        job.next_poll_at = 0.0
        return job

    def poll(self, job: Job, ctx: RunContext) -> Job:
        client = self._get_client(ctx)
        h = job.poll_handle or {}
        op = h.get("op", "AnalyzeDocument")
        kw = {"JobId": job.backend_job_id}
        if h.get("next_token"):
            kw["NextToken"] = h["next_token"]
        getter = getattr(client, _ASYNC_GET.get(op, "get_document_analysis"))
        try:
            resp = getter(**kw)
        except Exception as e:  # noqa: BLE001
            raise self._map_error(e) from e
        status = resp.get("JobStatus")
        if status == "IN_PROGRESS":
            job.next_poll_at = (job.next_poll_at or 0.0) + 2000.0
            return job
        if status == "FAILED":
            raise TerminalError("Textract job FAILED", backend_code="FAILED")
        # SUCCEEDED (or PARTIAL_SUCCESS): accumulate this page's items, page through NextToken
        items_key = h["items_key"]
        h["items"].extend(resp.get(items_key, []))
        h["meta"] = resp.get("DocumentMetadata", h.get("meta"))
        h["model_version"] = resp.get("AnalyzeDocumentModelVersion")
        next_token = resp.get("NextToken")
        if next_token:
            h["next_token"] = next_token
            job.next_poll_at = (job.next_poll_at or 0.0) + 100.0  # fetch next page promptly
            return job
        # all pages fetched
        job.state = JobState.SUCCEEDED
        job.raw = RawResult(
            payload={
                items_key: h["items"],
                "DocumentMetadata": h.get("meta"),
                "AnalyzeDocumentModelVersion": h.get("model_version"),
            },
            media_type="application/json",
            encoding="json",
            object_class=h["op"],
        )
        return job

    def _map_error(self, e: Exception):
        code = getattr(e, "code", None) or type(e).__name__
        # botocore ClientError carries the code under response["Error"]["Code"]
        resp = getattr(e, "response", None)
        if isinstance(resp, dict):
            code = resp.get("Error", {}).get("Code", code)
        if code in _AUTH_CODES:
            return TerminalError(str(e), backend_code="auth_rejected")
        if code in _RETRYABLE_CODES:
            return RetryableError(str(e), backend_code=code, retry_after=None)
        if code in _TERMINAL_CODES:
            return TerminalError(str(e), backend_code=code)
        if isinstance(e, (TerminalError, RetryableError)):
            return e
        return TerminalError(str(e), backend_code=code)

    # ---- transform (Block-graph reassembly) ----------------------------------
    def normalize(
        self, job: Job, ctx: RunContext, slim_req: OpenReadingRequest
    ) -> NormalizedResponse:
        raw: dict[str, Any] = job.raw.payload if job.raw else {}
        op = job.raw.object_class if job.raw else "AnalyzeDocument"
        outputs = slim_req.outputs or Outputs()

        if op in ("AnalyzeExpense",):
            resp = self._normalize_expense(raw)
        elif op == "AnalyzeID":
            resp = self._normalize_id(raw)
        elif op == "AnalyzeLending":
            resp = self._normalize_lending(raw)
        else:
            resp = self._normalize_blocks(raw, outputs, op or "AnalyzeDocument")

        if outputs.include_backend_raw:
            resp.backend_raw = BackendRaw(
                encoding="json", media_type="application/json", object_class=op, payload=raw
            )
        return resp

    def _bbox(self, geom: dict | None, page: int):
        if not geom:
            return None
        bb = geom.get("BoundingBox")
        if not bb:
            return None
        poly = [[p["X"], p["Y"]] for p in geom.get("Polygon", [])] or None
        return to_canonical(
            [bb["Left"], bb["Top"], bb["Left"] + bb["Width"], bb["Top"] + bb["Height"]],
            origin=NativeOrigin.TOP_LEFT,
            unit=NativeUnit.NORMALIZED,
            page_width=1.0,
            page_height=1.0,
            page=page,
            polygon=poly,
        )

    def _bbox_dict(self, geom: dict | None, page: int) -> dict | None:
        """Canonical bbox as a plain dict — the shape `GridCell.bbox` expects (cells_to_grid
        re-validates it into a BBox)."""
        bb = self._bbox(geom, page)
        return bb.model_dump(mode="json", exclude_none=True) if bb is not None else None

    def _citation(self, geom: dict | None, page: int, text: str | None) -> Citation | None:
        """Field geometry (Geometry.BoundingBox) → a Citation (P1). None when there is no geometry
        to cite — a citation is a provenance pointer, never fabricated without a location."""
        bbox = self._bbox(geom, page)
        if bbox is None:
            return None
        return Citation(page=page, bbox=bbox, text=text)

    @staticmethod
    def _page_count(raw: dict) -> int:
        """The real page count from DocumentMetadata.Pages (typed ops used to hard-code 1)."""
        return (raw.get("DocumentMetadata") or {}).get("Pages", 1) or 1

    def _child_text(self, block: dict, by_id: dict) -> str:
        """Concatenated text of a block's CHILD words. SELECTION_ELEMENT children render as the
        WORDS checked/unchecked (C1) — never the invented [X]/[ ] notation."""
        words: list[str] = []
        for rel in block.get("Relationships", []) or []:
            if rel.get("Type") != "CHILD":
                continue
            for cid in rel["Ids"]:
                c = by_id.get(cid, {})
                bt = c.get("BlockType")
                if bt == "WORD":
                    words.append(c.get("Text", ""))
                elif bt == "SELECTION_ELEMENT":
                    words.append(
                        "checked" if c.get("SelectionStatus") == "SELECTED" else "unchecked"
                    )
                elif c.get("Text"):
                    words.append(c["Text"])
        return " ".join(words).strip()

    def _normalize_blocks(self, raw: dict, outputs: Outputs, op: str) -> NormalizedResponse:
        blocks_raw = raw.get("Blocks", [])
        by_id = {b["Id"]: b for b in blocks_raw}

        n_pages = self._page_count(raw)
        pages: dict[int, list[Block]] = {p: [] for p in range(1, n_pages + 1)}
        has_layout = any(b["BlockType"].startswith("LAYOUT_") for b in blocks_raw)
        typed_fields: dict[str, TypedField] = {}

        for b in blocks_raw:
            bt = b["BlockType"]
            page = b.get("Page", 1)
            conf = _conf(b.get("Confidence"))

            if bt == "KEY_VALUE_SET" and "KEY" in (b.get("EntityTypes") or []):
                key = self._child_text(b, by_id)
                value = ""
                vconf = conf
                vgeom = None
                vpage = page
                for rel in b.get("Relationships", []) or []:
                    if rel.get("Type") == "VALUE":
                        for vid in rel["Ids"]:
                            vb = by_id.get(vid, {})
                            value = self._child_text(vb, by_id)
                            if vb.get("Confidence") is not None:
                                vconf = _conf(vb["Confidence"])
                            vgeom = vb.get("Geometry")
                            vpage = vb.get("Page", page)
                if key:
                    # P1: value geometry (or the key's, defensively) → a citation pointer.
                    cit = self._citation(vgeom or b.get("Geometry"), vpage, value or None)
                    typed_fields[key.rstrip(":")] = TypedField(
                        value=value, confidence=vconf, citations=[cit] if cit else None
                    )
                continue

            if bt == "TABLE":
                pages[page].append(self._table_block(b, by_id, page))
                continue
            if bt == "SIGNATURE":
                pages[page].append(
                    Block(
                        type=BlockType.SIGNATURE,
                        native_type=bt,
                        bbox=self._bbox(b.get("Geometry"), page),
                        confidence=conf,
                    )
                )
                continue

            if has_layout and bt in _LAYOUT_MAP:
                pages[page].append(
                    Block(
                        type=_LAYOUT_MAP[bt],
                        native_type=bt,
                        text=self._child_text(b, by_id) or None,
                        bbox=self._bbox(b.get("Geometry"), page),
                        confidence=conf,
                    )
                )
            elif not has_layout and bt == "LINE":
                tt = {"HANDWRITING": TextType.HANDWRITING, "PRINTED": TextType.PRINTED}.get(
                    b.get("TextType")
                )
                pages[page].append(
                    Block(
                        type=BlockType.TEXT,
                        native_type=bt,
                        text=b.get("Text"),
                        bbox=self._bbox(b.get("Geometry"), page),
                        confidence=conf,
                        text_type=tt,
                    )
                )

        page_objs = []
        text_parts, md_parts = [], []
        for pno in sorted(pages):
            bl = pages[pno]
            for i, blk in enumerate(bl):
                blk.reading_order = i
            ptext = "\n".join(b.text for b in bl if b.text)
            page_objs.append(Page(page_number=pno, blocks=bl, text=ptext or None))
            text_parts.append(ptext)
            md_parts.append("\n\n".join(self._block_md(b) for b in bl if self._block_md(b)))

        resp = NormalizedResponse(
            status=Status(state=ResponseState.SUCCEEDED),
            backend=BackendInfo(
                id="aws-textract",
                type=BackendType.HOSTED_API,
                operation=op,  # P2: reflect the real op (DetectDocumentText vs AnalyzeDocument)
                output_paradigm=[OutputParadigm.BLOCK_GRAPH],
            ),
            document=Document(
                text="\n\n".join(text_parts) if outputs.text else None,
                markdown="\n\n".join(m for m in md_parts if m) if outputs.markdown else None,
                page_count=n_pages,
                pages=page_objs,
            ),
            typed_fields=typed_fields or None,
        )
        # v0.3 provenance (§3.3/§6.4): blocks/cells/geometry/confidence come straight from Textract
        # (native); text and markdown are the platform's plain/GFM projections of that block graph.
        provenance = {
            "text": "derived",
            "markdown": "derived",
            "blocks": "native",
            "block_bbox": "native",
            "block_confidence": "native",
            "table_cells": "native",
        }
        if typed_fields:
            provenance["typed_fields"] = "native"
        resp.channel_provenance = provenance
        return resp

    def _table_block(self, table: dict, by_id: dict, page: int) -> Block:
        # A TABLE references its CELLs via a CHILD relationship and its merged cells via a SEPARATE
        # MERGED_CELL relationship (previously never walked → spans lost, P9). Each MERGED_CELL in
        # turn CHILD-references the underlying CELLs it covers.
        cell_ids: list[str] = []
        merged_ids: list[str] = []
        for rel in table.get("Relationships", []) or []:
            t = rel.get("Type")
            if t == "CHILD":
                cell_ids.extend(rel.get("Ids", []))
            elif t == "MERGED_CELL":
                merged_ids.extend(rel.get("Ids", []))
        cells_by_id = {
            cid: by_id[cid]
            for cid in cell_ids
            if cid in by_id and by_id[cid].get("BlockType") == "CELL"
        }
        merged = [
            by_id[mid]
            for mid in merged_ids
            if mid in by_id and by_id[mid].get("BlockType") == "MERGED_CELL"
        ]

        def rc(c: dict) -> tuple[int, int]:
            return (c.get("RowIndex", 1) or 1) - 1, (c.get("ColumnIndex", 1) or 1) - 1

        def is_header(c: dict) -> bool:
            ets = c.get("EntityTypes") or []
            return "COLUMN_HEADER" in ets or "ROW_HEADER" in ets

        covered: set[str] = set()
        grid_cells: list[GridCell] = []
        conf_map: dict[tuple[int, int], float | None] = {}

        for m in merged:
            member_ids = [
                cid
                for rel in m.get("Relationships", []) or []
                if rel.get("Type") == "CHILD"
                for cid in rel.get("Ids", [])
            ]
            covered.update(member_ids)
            r, col = rc(m)
            text = " ".join(
                t for cid in member_ids if (t := self._child_text(cells_by_id.get(cid, {}), by_id))
            ).strip()
            grid_cells.append(
                GridCell(
                    r,
                    col,
                    m.get("RowSpan", 1) or 1,
                    m.get("ColumnSpan", 1) or 1,
                    text or None,
                    is_header(m),
                    self._bbox_dict(m.get("Geometry"), page),
                )
            )
            conf_map[(r, col)] = _conf(m.get("Confidence"))

        for cid, c in cells_by_id.items():
            if cid in covered:  # folded into a merged cell above
                continue
            r, col = rc(c)
            grid_cells.append(
                GridCell(
                    r,
                    col,
                    c.get("RowSpan", 1) or 1,
                    c.get("ColumnSpan", 1) or 1,
                    self._child_text(c, by_id) or None,
                    is_header(c),
                    self._bbox_dict(c.get("Geometry"), page),
                )
            )
            conf_map[(r, col)] = _conf(c.get("Confidence"))

        grid_cells.sort(key=lambda g: (g.row, g.col if g.col is not None else 0))
        table_model = cells_to_grid(grid_cells)  # THE occupancy cursor (true coords under spans)
        for cell in table_model.cells or []:  # GridCell carries no confidence — reattach per cell
            if cell.row is None or cell.col is None:
                continue
            conf = conf_map.get((cell.row, cell.col))
            if conf is not None:
                cell.confidence = conf

        return Block(
            type=BlockType.TABLE,
            native_type="TABLE",
            text=table_to_text(table_model) or None,  # P0/C2: grid content into the text channel
            markdown=table_to_pipe_md(table_model) or None,
            bbox=self._bbox(table.get("Geometry"), page),
            confidence=_conf(table.get("Confidence")),
            table=table_model,
        )

    def _block_md(self, b: Block) -> str:
        if b.type is BlockType.TITLE:
            return f"# {b.text or ''}"
        if b.type is BlockType.SECTION_HEADER:
            return f"## {b.text or ''}"
        if b.type is BlockType.TABLE and b.table:
            return table_to_pipe_md(b.table)
        return b.text or ""

    def _collect_field(
        self,
        tf: dict[str, TypedField],
        key: str,
        value: Any,
        confidence: float | None,
        citations: list[Citation] | None,
    ) -> None:
        """Add a typed field, collecting repeated names into a LIST value (§4.6) instead of
        last-writer-wins — line items repeat the same field type once per row."""
        if key not in tf:
            tf[key] = TypedField(value=value, confidence=confidence, citations=citations)
            return
        existing = tf[key]
        values = existing.value if isinstance(existing.value, list) else [existing.value]
        cits = (existing.citations or []) + (citations or [])
        tf[key] = TypedField(value=[*values, value], confidence=None, citations=cits or None)

    def _add_expense_field(self, tf: dict[str, TypedField], f: dict) -> None:
        key = (f.get("Type") or {}).get("Text") or (f.get("LabelDetection") or {}).get("Text")
        if not key:
            return
        val = f.get("ValueDetection") or {}
        value = val.get("Text")
        page = f.get("PageNumber", 1)
        cit = self._citation(val.get("Geometry"), page, value)
        self._collect_field(tf, key, value, _conf(val.get("Confidence")), [cit] if cit else None)

    def _normalize_expense(self, raw: dict) -> NormalizedResponse:
        tf: dict[str, TypedField] = {}
        for ed in raw.get("ExpenseDocuments", []):
            for f in ed.get("SummaryFields", []):
                self._add_expense_field(tf, f)
            # P1: LineItemGroups[].LineItems[].LineItemExpenseFields[] — the real AnalyzeExpense
            # returns them; map each line-item field into typed_fields (repeats → list).
            for grp in ed.get("LineItemGroups", []) or []:
                for li in grp.get("LineItems", []) or []:
                    for f in li.get("LineItemExpenseFields", []) or []:
                        self._add_expense_field(tf, f)
        return self._typed_only("AnalyzeExpense", tf, self._page_count(raw))

    def _normalize_id(self, raw: dict) -> NormalizedResponse:
        tf: dict[str, TypedField] = {}
        for idd in raw.get("IdentityDocuments", []):
            for f in idd.get("IdentityDocumentFields", []):
                key = (f.get("Type") or {}).get("Text")
                val = f.get("ValueDetection") or {}
                if key:
                    cit = self._citation(val.get("Geometry"), 1, val.get("Text"))
                    tf[key] = TypedField(
                        value=val.get("Text"),
                        normalized_value=(val.get("NormalizedValue") or {}).get("Value"),
                        confidence=_conf(val.get("Confidence")),
                        citations=[cit] if cit else None,
                    )
        return self._typed_only("AnalyzeID", tf, self._page_count(raw))

    def _normalize_lending(self, raw: dict) -> NormalizedResponse:
        tf: dict[str, TypedField] = {}
        doc_label = None
        for res in raw.get("Results", []):
            res_page = res.get("Page", 1)
            pc = res.get("PageClassification") or {}
            for pt in pc.get("PageType", []) or []:
                doc_label = doc_label or pt.get("Value")
            for ext in res.get("Extractions", []) or []:
                for lf in (ext.get("LendingDocument") or {}).get("LendingFields", []) or []:
                    key = lf.get("Type")
                    for vd in lf.get("ValueDetections", []) or []:
                        if key and vd.get("Text"):
                            cit = self._citation(vd.get("Geometry"), res_page, vd.get("Text"))
                            self._collect_field(
                                tf,
                                key,
                                vd.get("Text"),
                                _conf(vd.get("Confidence")),
                                [cit] if cit else None,
                            )
        resp = self._typed_only("AnalyzeLending", tf, self._page_count(raw))
        if doc_label:
            resp.document.doc_type = DocType(
                label=doc_label, description="Textract Lending page classification"
            )
        return resp

    def _typed_only(
        self, op: str, tf: dict[str, TypedField], page_count: int = 1
    ) -> NormalizedResponse:
        resp = NormalizedResponse(
            status=Status(state=ResponseState.SUCCEEDED),
            backend=BackendInfo(
                id="aws-textract",
                type=BackendType.HOSTED_API,
                operation=op,
                output_paradigm=[OutputParadigm.TYPED_FIELDS],
            ),
            document=Document(page_count=page_count),
            typed_fields=tf or None,
        )
        if tf:
            resp.channel_provenance = {"typed_fields": "native"}
        return resp

    def report_cost(self, job: Job) -> CostReport:
        """The page count Textract returned in `DocumentMetadata`.

        This used to multiply it by a per-operation `_OP_PRICE` table. Textract prices differ by
        operation, region and volume tier, none of which this call knows.
        """
        pages = 1
        if job.raw and isinstance(job.raw.payload, dict):
            pages = (job.raw.payload.get("DocumentMetadata") or {}).get("Pages", 1) or 1
        return CostReport(native_unit="page", native_quantity=float(pages))
