"""LlamaParse adapters: LlamaIndex's hosted Parse v2 API, with one descriptor per tier.

What it is. LlamaParse is LlamaIndex's hosted document parsing service. It is not the local
`liteparse` wheel in `openreading.adapters.liteparse`, and it is not the LlamaIndex framework.
Every request names a tier and a dated version. This module registers four backends, because
the tiers run different pipelines and return different channels. For example, `llamaparse-fast`
returns plain text only, while `llamaparse-agentic` also returns Markdown and structured items.

Flow. POLL. `submit` uploads local bytes to `POST /api/v2/parse/upload` as a multipart `file`
beside a JSON `configuration` form field. A document URL goes to `POST /api/v2/parse` as
`source_url` in a JSON body. `poll` reads `GET /api/v2/parse/{job_id}` with repeated `expand`
query parameters every two seconds. PENDING and RUNNING keep polling, and COMPLETED finishes the
job. FAILED and CANCELLED raise TerminalError with a fixed message, because vendor error text
can quote the document. `cancel` calls `POST /api/v2/parse/{job_id}/cancel` as a best effort.

Pinned contract. Every request sends `tier` and a dated `version`, never `latest`, so output
cannot change when LlamaIndex ships a new pipeline. The pins in `TIERS` were the current
releases in the `llama-cloud` 2.16.0 SDK on 2026-09-13. `request.backend.version` may name a
different dated release, and the service rejects a date it never published. The agentic tiers
send `processing_options.cost_optimizer.enable=false` explicitly. That optimizer routes simple
pages to a cheaper pipeline, so one response would silently mix tiers. Nothing else is sent. No
auto mode rules, custom prompts, high-effort confidence scoring, form enrichment, webhooks, or
page ranges reach the service. Each of those adds credits or changes what the tier means.

Tiers, as LlamaIndex describes them. `fast` suits plain single-column text and cannot return
Markdown or items. `cost_effective` suits text-heavy pages with simple tables. `agentic` suits
scanned pages, multi-column layouts, real tables, and charts. `agentic_plus` suits dense
financial reports and complex tables, and it turns on specialized chart parsing by default.
Every capability below is "claimed" from that guide, and none was verified by a live run here.

Channels. Page `text` comes from `expand=text` for every tier (N). Page Markdown comes from
`expand=markdown` on the structured tiers (N) and does not exist on `fast` (X). Blocks come from
`expand=items` in the service's order. A nested list, header, or footer item becomes its own
block, and its container lists the child ids in `children`. A table item becomes cells from its
native `rows` grid (N). Header rows are never inferred, and a null cell stays null. Block boxes
are X, because the item `bbox` documentation names no origin or unit, so a canonical box would be
a guess. Block confidence is X too, since it rides on that box. Page confidence comes from
`expand=metadata`. Typed fields are X, because form enrichment is a separate paid pass. A page
the service marks failed makes the response PARTIAL.

Usage. Polls request `expand=usage` because billed credits are absent unless that section is
requested. `usage.credits` is the job's own credit count, and the service leaves it null until
billing records it.
`report_cost` returns credits when present and the page count otherwise.
LlamaIndex caches a parse of the same file for 48 hours at no charge, so a repeat can show zero.

Credentials. `LLAMA_CLOUD_API_KEY`, or the SDK alias `LLAMA_PARSE_API_KEY`, is sent as a bearer
token. `LLAMA_CLOUD_BASE_URL` replaces `https://api.cloud.llamaindex.ai`, matching the SDK.

Sources: https://developers.llamaindex.ai/llamaparse/parse/guides/tiers/,
https://developers.llamaindex.ai/llamaparse/parse/guides/configuring-parse/,
https://developers.llamaindex.ai/llamaparse/parse/guides/retrieving-results/,
https://pypi.org/project/llama-cloud/2.16.0/ (accessed 2026-09-13).
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

from openreading.adapters._http import error_for_status
from openreading.adapters.base import BackendAdapter
from openreading.derive import table_to_text
from openreading.derive.mime import resolve_mime_type
from openreading.types.blocks import Block, Table, TableCell
from openreading.types.cost import CostReport
from openreading.types.descriptor import (
    AdapterDescriptor,
    Capabilities,
    ConfigField,
    CredentialField,
    Output,
    OutputChannels,
    Provisioning,
    RuntimeProfile,
    Source,
)
from openreading.types.enums import (
    BackendType,
    BlockType,
    ChannelGrade,
    JobState,
    PageUnit,
    ResponseState,
    WaitMode,
)
from openreading.types.errors import RetryableError, TerminalError, UnsupportedFeatureError
from openreading.types.job import Job
from openreading.types.request import OpenReadingRequest, Outputs
from openreading.types.response import (
    BackendInfo,
    BackendRaw,
    Document,
    NormalizedResponse,
    Page,
    Status,
    Usage,
)
from openreading.types.runtime import Health, RawResult, RunContext

N = ChannelGrade.NATIVE
X = ChannelGrade.IMPOSSIBLE

DEFAULT_BASE_URL = "https://api.cloud.llamaindex.ai"
_POLL_INTERVAL_MS = 2000.0
_DATED_VERSION = re.compile(r"\d{4}-\d{2}-\d{2}")
_RESULT_SECTIONS = ("text", "markdown", "items", "metadata")
_CONTAINERS = ("list", "header", "footer")
_ITEM_TYPES = {
    "text": BlockType.TEXT,
    "heading": BlockType.SECTION_HEADER,
    "list": BlockType.LIST,
    "table": BlockType.TABLE,
    "image": BlockType.FIGURE,
    "code": BlockType.CODE,
    "header": BlockType.HEADER,
    "footer": BlockType.FOOTER,
}


@dataclass(frozen=True)
class TierProfile:
    """One LlamaParse tier: its slug, pinned release, and what LlamaIndex says it handles."""

    tier: str
    slug: str
    version: str
    structured: bool
    accepts_cost_optimizer: bool
    capabilities: dict[str, Any] = field(default_factory=dict)


TIERS: dict[str, TierProfile] = {
    "fast": TierProfile("fast", "llamaparse-fast", "2026-06-15", False, False),
    "cost_effective": TierProfile(
        "cost_effective",
        "llamaparse-cost-effective",
        "2026-08-19",
        True,
        False,
        {"printed_tables": "claimed", "vlm_based": "claimed"},
    ),
    "agentic": TierProfile(
        "agentic",
        "llamaparse-agentic",
        "2026-09-07",
        True,
        True,
        {
            "ocr": "claimed",
            "layout": "claimed",
            "multi_column": "claimed",
            "printed_tables": "claimed",
            "figures_charts": "claimed",
            "vlm_based": "claimed",
        },
    ),
    "agentic_plus": TierProfile(
        "agentic_plus",
        "llamaparse-agentic-plus",
        "2026-08-19",
        True,
        True,
        {
            "ocr": "claimed",
            "layout": "claimed",
            "multi_column": "claimed",
            "printed_tables": "claimed",
            "complex_tables": "claimed",
            "figures_charts": "claimed",
            "vlm_based": "claimed",
        },
    ),
}


class LlamaParseClient(Protocol):
    def upload(
        self, filename: str, data: bytes, mime_type: str | None, configuration: dict
    ) -> dict: ...
    def create(self, body: dict) -> dict: ...
    def get(self, job_id: str, expand: list[str]) -> dict: ...
    def cancel(self, job_id: str) -> dict: ...


class HttpxLlamaParseClient:
    """The wire client. It sends the same requests as the `llama-cloud` 2.16.0 SDK."""

    def __init__(self, api_key: str, base_url: str) -> None:
        from openreading.adapters._http import build_httpx_client

        self._http = build_httpx_client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=120.0,
        )

    @staticmethod
    def _json(response, action: str) -> dict:
        if response.status_code >= 400:
            # A fixed message, because a vendor error body can quote the document.
            raise error_for_status(
                response.status_code,
                response.headers,
                message=f"LlamaParse {action} request returned HTTP {response.status_code}.",
            )
        return response.json()

    def upload(
        self, filename: str, data: bytes, mime_type: str | None, configuration: dict
    ) -> dict:
        response = self._http.post(
            "/api/v2/parse/upload",
            data={"configuration": json.dumps(configuration)},
            files={"file": (filename, data, mime_type or "application/octet-stream")},
        )
        return self._json(response, "upload")

    def create(self, body: dict) -> dict:
        return self._json(self._http.post("/api/v2/parse", json=body), "create")

    def get(self, job_id: str, expand: list[str]) -> dict:
        response = self._http.get(
            f"/api/v2/parse/{quote(job_id, safe='')}",
            params=[("expand", value) for value in expand],
        )
        return self._json(response, "status")

    def cancel(self, job_id: str) -> dict:
        response = self._http.post(f"/api/v2/parse/{quote(job_id, safe='')}/cancel")
        return self._json(response, "cancel")


def _descriptor(profile: TierProfile) -> AdapterDescriptor:
    structured = N if profile.structured else X
    output = Output(
        channels=OutputChannels(
            text=N,
            markdown=structured,
            blocks=structured,
            block_bbox=X,
            block_confidence=X,
            table_cells=structured,
            typed_fields=X,
        ),
        block_granularity="element" if profile.structured else None,
    )
    return AdapterDescriptor(
        id=profile.slug,
        type=BackendType.HOSTED_API,
        provisioning=Provisioning(byo_mode=["api_key"], auth="api_key"),
        protocol_version=2,
        adapter_impl="http",
        wait_modes=[WaitMode.POLL],
        operations=["parse"],
        capabilities=Capabilities(
            **profile.capabilities,
            input_formats=[
                "pdf",
                "doc",
                "docx",
                "ppt",
                "pptx",
                "xls",
                "xlsx",
                "csv",
                "html",
                "rtf",
                "epub",
                "png",
                "jpg",
                "jpeg",
                "tiff",
                "webp",
                "gif",
                "bmp",
            ],
            page_range_selection=False,
        ),
        runtime=RuntimeProfile(
            offline_capable=False,
            license="proprietary",
            version_pin=f"api v2 tier={profile.tier} version={profile.version}",
        ),
        output=output,
        credentials_spec=[
            CredentialField(
                key="api_key",
                required=True,
                env=["LLAMA_CLOUD_API_KEY", "LLAMA_PARSE_API_KEY"],
                example="llx-...",
            )
        ],
        config_spec=[
            ConfigField(
                key="base_url",
                env=["LLAMA_CLOUD_BASE_URL"],
                description="LlamaParse API origin. Defaults to https://api.cloud.llamaindex.ai.",
            )
        ],
        signup_url="https://cloud.llamaindex.ai",
        accepts_url=True,
        live_gate_env=["LLAMA_CLOUD_API_KEY"],
        # The v2 create and upload references document no idempotency key or client token, so a
        # retried submit starts a second job. Only the 48-hour result cache softens the charge.
        idempotency_supported=False,
        cancel_supported=True,
        sources=[
            Source(
                url="https://developers.llamaindex.ai/llamaparse/parse/guides/tiers/",
                accessed="2026-09-13",
                supports="Four tiers, dated version pinning, fast has no Markdown, chart defaults.",
            ),
            Source(
                url="https://developers.llamaindex.ai/llamaparse/parse/guides/configuring-parse/",
                accessed="2026-09-13",
                supports="Configuration object, cost optimizer, processing options.",
            ),
            Source(
                url="https://developers.llamaindex.ai/llamaparse/parse/guides/retrieving-results/",
                accessed="2026-09-13",
                supports="Expand values, per-page result shapes, failed page entries.",
            ),
            Source(
                url="https://pypi.org/project/llama-cloud/2.16.0/",
                accessed="2026-09-13",
                supports="Endpoints, multipart configuration, repeated expand, item models, file extensions.",
            ),
        ],
    )


def _unsupported(feature: str, message: str) -> UnsupportedFeatureError:
    return UnsupportedFeatureError(message, backend_code=f"{feature}_unsupported", feature=feature)


def _request_failed(action: str) -> TerminalError:
    return TerminalError(
        f"LlamaParse did not complete the {action} request.", backend_code="request_failed"
    )


def _pages_by_number(raw: dict, section: str) -> dict[int, dict]:
    pages = (raw.get(section) or {}).get("pages") or []
    return {
        int(page["page_number"]): page
        for page in pages
        if isinstance(page, dict) and page.get("page_number") is not None
    }


def _table(rows: Any) -> Table | None:
    if not rows:
        return None
    grid = [[None if value is None else str(value) for value in row] for row in rows]
    cells = [
        TableCell(row=r, col=c, text=value)
        for r, row in enumerate(grid)
        for c, value in enumerate(row)
    ]
    return Table(n_rows=len(grid), n_cols=max(len(row) for row in grid), cells=cells, rows=grid)


class LlamaParseAdapter(BackendAdapter):
    """One LlamaParse tier. The registry uses the zero-argument subclasses below."""

    def __init__(self, tier: str, client: LlamaParseClient | None = None) -> None:
        self._profile = TIERS[tier]
        self.descriptor = _descriptor(self._profile)
        self._client = client

    def health(self) -> Health:
        if self._client is not None:
            return Health(ready=True, detail="injected client")
        try:
            import httpx  # noqa: F401
        except ImportError:
            return Health(
                ready=False, missing_deps=["httpx (pip install 'openreading[llamaparse]')"]
            )
        return Health(ready=True)

    def _get_client(self, ctx: RunContext) -> LlamaParseClient:
        if self._client is not None:
            return self._client
        values = (ctx.credentials.values if ctx.credentials else {}) or {}
        key = values.get("api_key")
        if not key:
            raise TerminalError(
                "LlamaParse needs credentials_ref → {api_key}", backend_code="no_credentials"
            )
        base_url = (ctx.runtime or {}).get("base_url") or DEFAULT_BASE_URL
        return HttpxLlamaParseClient(key, base_url)

    def _expand(self) -> list[str]:
        sections = list(_RESULT_SECTIONS) if self._profile.structured else ["text", "metadata"]
        return [*sections, "usage"]

    def _version(self, req: OpenReadingRequest) -> str:
        requested = req.backend.version
        if requested is None:
            return self._profile.version
        if not _DATED_VERSION.fullmatch(requested):
            raise TerminalError(
                "LlamaParse needs a dated tier version such as 2026-09-07, never latest.",
                backend_code="version_unpinned",
            )
        return requested

    def _check_request(self, req: OpenReadingRequest) -> None:
        if req.pages is not None:
            raise _unsupported("pages", "This adapter does not forward LlamaParse page ranges.")
        features = req.features
        if features is not None and features.ocr != "auto":
            raise _unsupported("ocr", "LlamaParse tiers expose no switch that turns OCR on or off.")
        if features is not None and features.ocr_languages:
            raise _unsupported("ocr_languages", "This adapter does not forward OCR language hints.")
        if req.async_ is not None and req.async_.webhook_url is not None:
            raise _unsupported("webhook", "This adapter polls LlamaParse and registers no webhook.")

    def _local_document(self, req: OpenReadingRequest) -> tuple[bytes, str, str | None] | None:
        document = req.document
        refused = TerminalError(
            "LlamaParse accepts a URL, a local path, or bytes without a password.",
            backend_code="unsupported_input",
        )
        if document.password or document.file_id:
            raise refused
        if document.url:
            return None
        try:
            if document.path:
                data = Path(document.path).read_bytes()
            else:
                data = base64.b64decode(document.bytes_base64 or "", validate=True)
        except (OSError, ValueError):
            raise refused from None
        if not data:
            raise refused
        filename = document.filename or (Path(document.path).name if document.path else "document")
        mime = resolve_mime_type(mime_type=document.mime_type, filename=filename, data=data)
        return data, filename, mime

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        self.assert_supports(req)
        self._check_request(req)
        version = self._version(req)
        local = self._local_document(req)
        client = self._get_client(ctx)
        configuration: dict[str, Any] = {"tier": self._profile.tier, "version": version}
        if self._profile.accepts_cost_optimizer:
            configuration["processing_options"] = {"cost_optimizer": {"enable": False}}
        try:
            if local is None:
                created = client.create({"source_url": req.document.url, **configuration})
            else:
                data, filename, mime = local
                created = client.upload(filename, data, mime, configuration)
        except (RetryableError, TerminalError):
            raise
        except Exception:
            raise _request_failed("parse") from None
        job_id = created.get("id") if isinstance(created, dict) else None
        if not job_id:
            raise _request_failed("parse")
        job = self.new_job(
            WaitMode.POLL, state=JobState.RUNNING, idempotency_key=ctx.idempotency_key
        )
        job.backend_job_id = job_id
        job.poll_handle = {"job_id": job_id, "version": version}
        job.next_poll_at = 0.0
        return job

    def poll(self, job: Job, ctx: RunContext) -> Job:
        client = self._get_client(ctx)
        job_id = (job.poll_handle or {}).get("job_id") or job.backend_job_id
        try:
            raw = client.get(str(job_id), self._expand())
        except (RetryableError, TerminalError):
            raise
        except Exception:
            raise _request_failed("status") from None
        status = (raw.get("job") or {}).get("status") if isinstance(raw, dict) else None
        if status in ("PENDING", "RUNNING"):
            job.next_poll_at = (job.next_poll_at or 0.0) + _POLL_INTERVAL_MS
            return job
        if status == "FAILED":
            raise TerminalError("LlamaParse reported the job as failed.", backend_code="job_failed")
        if status == "CANCELLED":
            raise TerminalError(
                "LlamaParse reported the job as cancelled.", backend_code="job_cancelled"
            )
        if status != "COMPLETED":
            raise TerminalError(
                "LlamaParse returned an unknown job status.", backend_code="unknown_status"
            )
        job.state = JobState.SUCCEEDED
        job.raw = RawResult(
            payload=raw, media_type="application/json", encoding="json", object_class="parse"
        )
        return job

    def cancel(self, job: Job, ctx: RunContext) -> Job:
        if job.is_terminal():
            return job
        client = self._get_client(ctx)
        job_id = (job.poll_handle or {}).get("job_id") or job.backend_job_id
        if job_id:
            try:
                client.cancel(str(job_id))
            except (RetryableError, TerminalError):
                raise
            except Exception:
                raise _request_failed("cancel") from None
        job.state = JobState.CANCELLED
        return job

    def normalize(
        self, job: Job, ctx: RunContext, slim_req: OpenReadingRequest
    ) -> NormalizedResponse:
        raw: dict[str, Any] = job.raw.payload if job.raw else {}
        outputs = slim_req.outputs or Outputs()
        profile = self._profile
        version = (job.poll_handle or {}).get("version") or profile.version
        sections = {name: _pages_by_number(raw, name) for name in _RESULT_SECTIONS}
        numbers = sorted(set().union(*sections.values()))
        pages: list[Page] = []
        failed = 0
        has_blocks = has_tables = False
        for number in numbers:
            markdown_page = sections["markdown"].get(number, {})
            items_page = sections["items"].get(number, {})
            if markdown_page.get("success") is False or items_page.get("success") is False:
                failed += 1
            blocks: list[Block] = []
            if profile.structured and items_page.get("success"):
                for item in items_page.get("items") or []:
                    self._flatten(item, number, blocks, in_list=False)
            if outputs.tables == "none":
                for block in blocks:
                    block.table = None
            has_blocks = has_blocks or bool(blocks)
            has_tables = has_tables or any(block.table is not None for block in blocks)
            width = items_page.get("page_width") if items_page.get("success") else None
            height = items_page.get("page_height") if items_page.get("success") else None
            confidence = sections["metadata"].get(number, {}).get("confidence")
            markdown = markdown_page.get("markdown") if markdown_page.get("success") else None
            pages.append(
                Page(
                    page_number=number,
                    width=float(width) if width else None,
                    height=float(height) if height else None,
                    unit=PageUnit.PDF_POINT if width and height else None,
                    text=sections["text"].get(number, {}).get("text") if outputs.text else None,
                    markdown=markdown if outputs.markdown else None,
                    blocks=blocks if outputs.blocks and blocks else None,
                    confidence=float(confidence) if confidence is not None else None,
                )
            )
        texts = [page.text for page in pages if page.text]
        markdowns = [page.markdown for page in pages if page.markdown]
        credits = ((raw.get("job") or {}).get("usage") or {}).get("credits")
        response = NormalizedResponse(
            status=Status(state=ResponseState.PARTIAL if failed else ResponseState.SUCCEEDED),
            backend=BackendInfo(
                id=profile.slug, type=BackendType.HOSTED_API, version=version, operation="parse"
            ),
            document=Document(
                page_count=len(pages) or None,
                pages=pages,
                text="\n\n".join(texts) or None,
                markdown="\n\n".join(markdowns) or None,
            ),
            usage=Usage(
                pages_processed=len(pages) or None,
                credits=float(credits) if credits is not None else None,
            ),
        )
        self._warn(
            response,
            outputs,
            failed=failed,
            texts=bool(texts),
            markdowns=bool(markdowns),
            blocks=has_blocks,
            tables=has_tables,
        )
        provenance = {}
        for channel, emitted in (
            ("text", bool(texts)),
            ("markdown", bool(markdowns)),
            ("blocks", outputs.blocks and has_blocks),
            ("table_cells", outputs.blocks and has_tables),
        ):
            if emitted:
                provenance[channel] = "native"
        response.channel_provenance = provenance or None
        if outputs.include_backend_raw:
            response.backend_raw = BackendRaw(
                encoding="json", media_type="application/json", object_class="parse", payload=raw
            )
        return response

    def _warn(
        self,
        response: NormalizedResponse,
        outputs: Outputs,
        *,
        failed: int,
        texts: bool,
        markdowns: bool,
        blocks: bool,
        tables: bool,
    ) -> None:
        tier = self._profile.tier
        if failed:
            response.add_warning(
                "partial_conversion",
                f"LlamaParse marked {failed} page(s) as failed, so their structure is omitted.",
                "document.pages",
            )
        if outputs.text and not texts:
            response.add_warning("text_unavailable", "LlamaParse returned no page text.", "text")
        if outputs.markdown and not markdowns:
            response.add_warning(
                "markdown_unavailable", f"The {tier} tier returned no page Markdown.", "markdown"
            )
        if outputs.blocks and not blocks:
            response.add_warning(
                "blocks_unavailable", f"The {tier} tier returned no structured items.", "blocks"
            )
        if outputs.tables == "cells" and not (outputs.blocks and tables):
            response.add_warning(
                "table_cells_unavailable",
                f"The {tier} tier returned no table items.",
                "table_cells",
            )
        if outputs.blocks and blocks:
            response.add_warning(
                "block_bbox_unavailable",
                "LlamaParse item boxes name no origin or unit, so blocks carry no canonical box.",
                "block_bbox",
            )

    def _flatten(self, item: dict, page: int, blocks: list[Block], *, in_list: bool) -> str:
        kind = str(item.get("type") or "text")
        index = len(blocks)
        block_id = f"p{page}-i{index}"
        if in_list and kind == "text":
            block_type = BlockType.LIST_ITEM
        else:
            block_type = _ITEM_TYPES.get(kind, BlockType.OTHER)
        table = _table(item.get("rows")) if kind == "table" else None
        if kind in _CONTAINERS:
            text = None
        elif table is not None:
            text = table_to_text(table)
        else:
            text = item.get("value") or item.get("caption") or item.get("text") or None
        blocks.append(
            Block(
                type=block_type,
                id=block_id,
                native_type=kind,
                text=text,
                markdown=item.get("md"),
                html=item.get("html") if kind == "table" else None,
                reading_order=index,
                table=table,
            )
        )
        if kind in _CONTAINERS:
            children = [
                self._flatten(child, page, blocks, in_list=kind == "list")
                for child in item.get("items") or []
                if isinstance(child, dict)
            ]
            blocks[index] = blocks[index].model_copy(update={"children": children or None})
        return block_id

    def report_cost(self, job: Job) -> CostReport:
        raw: dict[str, Any] = job.raw.payload if job.raw else {}
        credits = ((raw.get("job") or {}).get("usage") or {}).get("credits")
        if credits is not None:
            return CostReport(native_unit="credit", native_quantity=float(credits))
        numbers = set().union(*(_pages_by_number(raw, name) for name in _RESULT_SECTIONS))
        return CostReport(native_unit="page", native_quantity=float(len(numbers)))


class LlamaParseFastAdapter(LlamaParseAdapter):
    """`llamaparse-fast`: plain text only, the lowest credit cost per page."""

    def __init__(self, client: LlamaParseClient | None = None) -> None:
        super().__init__("fast", client)


class LlamaParseCostEffectiveAdapter(LlamaParseAdapter):
    """`llamaparse-cost-effective`: Markdown and items for text-heavy pages with simple tables."""

    def __init__(self, client: LlamaParseClient | None = None) -> None:
        super().__init__("cost_effective", client)


class LlamaParseAgenticAdapter(LlamaParseAdapter):
    """`llamaparse-agentic`: scanned pages, multi-column layouts, tables, and charts."""

    def __init__(self, client: LlamaParseClient | None = None) -> None:
        super().__init__("agentic", client)


class LlamaParseAgenticPlusAdapter(LlamaParseAdapter):
    """`llamaparse-agentic-plus`: complex tables and charts, with chart parsing on by default."""

    def __init__(self, client: LlamaParseClient | None = None) -> None:
        super().__init__("agentic_plus", client)
