"""LiteParse adapter: local PDF and image parsing in core's supervised worker, with verified OCR.

What it is. LiteParse is LlamaIndex's open-source parser: a Rust core with bundled PDFium and
Tesseract, published on PyPI as `liteparse` under Apache-2.0 and pinned here at 2.14.4. It is
not LlamaParse, the hosted service. This adapter runs `parse` over PDF, PNG and JPEG input.

Flow. INLINE. `submit` validates the input and any OCR data, then runs one parse in a child
process started by `openreading.artifacts.supervisor.WarmWorker`. The parent enforces the
request deadline (`ctx.deadline_ms`, 120 seconds when unset) and a sampled process-tree memory
ceiling (`worker_memory_bytes`, 2 GiB when unset). It kills the whole process group on timeout,
memory excess, interruption, or a malformed control record. The child writes its result into a
private work directory that the parent removes afterwards. A frozen executable has no worker
entry point, so it refuses rather than guessing one.

OCR. LiteParse downloads missing Tesseract data from GitHub whenever OCR runs, even on a page
with a text layer. That was observed on 2026-09-13 against 2.14.4 with an empty tessdata
directory. So OCR runs only against an absolute `tessdata_path` whose file matches a hash pinned
in `openreading.adapters.liteparse.assets`, checked before a worker starts and again inside it.
`features.ocr="off"` disables OCR. The default `"auto"` enables it only when verified data is
configured, and otherwise parses the text layer and adds an `ocr_skipped` warning. `"force"`
raises UnsupportedFeatureError, because LiteParse chooses which regions need OCR and offers no
switch that rasterizes every page. Only English data is pinned, so another language refuses.

Channels. Page `text` and `markdown` are LiteParse's own per page (N). Blocks are its classified
layout blocks in reading order, each with a box in top-left 72-DPI page points, the same space as
the page width and height (N). A table block becomes cells from its native header and rows.
Header cells are marked because LiteParse separates them. Spans are never inferred, and a cell
without a box keeps none. AcroForm widgets become `typed_fields` keyed by field name, falling
back to LiteParse's widget id when a name repeats or is missing. If that key also exists, a
numbered suffix preserves every widget and its citation, including names such as `f2#2`.
Each carries the value LiteParse resolved (text, checked state, or selected options) and its widget
rectangle as a citation (N).
Per-block confidence does not exist, because OCR confidence is per text item and averaging it
onto a block would be invented (X). Page errors make the response PARTIAL with a
`partial_conversion` warning.

Non-choices. No OCR server URL, password, page range, image extraction, screenshots, or
LiteParse's own process pool. An OCR server would send page pixels off this machine, and the
others are not exercised here. Usage is the page count, because nothing is billed.

Environment variables this module reads. The credential broker resolves both through
`config_spec` into `ctx.runtime`, and `request.backend.runtime` can override them.
`LITEPARSE_TESSDATA` names the absolute tessdata directory, and OCR stays off when it is unset.
`LITEPARSE_WORKER_MEMORY_BYTES` sets the sampled worker ceiling, and 2 GiB applies when unset.

Sources: https://github.com/run-llama/liteparse (README and packages/python/README.md),
https://developers.llamaindex.ai/liteparse/guides/ocr/, https://pypi.org/project/liteparse/2.14.4/
(accessed 2026-09-13).
"""

from __future__ import annotations

import base64
import importlib.metadata
import importlib.util
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Protocol

from openreading.adapters.base import BackendAdapter
from openreading.adapters.liteparse.assets import OcrAssetError, verify_tessdata
from openreading.derive.mime import resolve_mime_type
from openreading.types.blocks import Block, Citation, Table, TableCell, TypedField
from openreading.types.cost import CostReport, infra_only
from openreading.types.descriptor import (
    AdapterDescriptor,
    Capabilities,
    ConfigField,
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
    NativeOrigin,
    NativeUnit,
    PageUnit,
    ResponseState,
    WaitMode,
)
from openreading.types.errors import RetryableError, TerminalError, UnsupportedFeatureError
from openreading.types.geometry import to_canonical
from openreading.types.job import Job
from openreading.types.request import OpenReadingRequest, Outputs
from openreading.types.response import (
    BackendInfo,
    BackendRaw,
    Document,
    NormalizedResponse,
    Page,
    Status,
)
from openreading.types.runtime import Health, RawResult, RunContext

N = ChannelGrade.NATIVE
X = ChannelGrade.IMPOSSIBLE

_DEFAULT_DEADLINE_MS = 120_000
_DEFAULT_MEMORY_BYTES = 2 * 1024**3
_MAX_RESULT_BYTES = 512 * 1024**2
_SUFFIX = {"application/pdf": ".pdf", "image/png": ".png", "image/jpeg": ".jpg"}
_BLOCK_TYPES = {
    "heading": BlockType.SECTION_HEADER,
    "paragraph": BlockType.TEXT,
    "list_item": BlockType.LIST_ITEM,
    "code": BlockType.CODE,
    "table": BlockType.TABLE,
    "figure": BlockType.FIGURE,
}
# Worker control codes -> the backend codes a caller sees. Anything else is a parse failure.
_WORKER_CODES = {
    "password_required": "password_required",
    "unsupported_format": "unsupported_input",
    "timeout": "worker_timeout",
    "memory_limit": "worker_memory_limit",
    "engine_identity_unavailable": "ocr_assets_unverified",
    "worker_unavailable": "worker_unavailable",
    "result_too_large": "result_too_large",
}
# Fixed messages keep local paths and document text out of every error.
_MESSAGES = {
    "unsupported_input": "LiteParse accepts local PDF, PNG, or JPEG bytes without a password or page range.",
    "ocr_language_unverified": "LiteParse OCR supports one pinned language: eng.",
    "ocr_assets_missing": "Set an absolute LITEPARSE_TESSDATA directory containing eng.traineddata.",
    "ocr_assets_unverified": "The OCR language data does not match a pinned tessdata_best or tessdata_fast file.",
    "password_required": "This document requires a password, which the LiteParse profile does not accept.",
    "worker_timeout": "The LiteParse worker exceeded the request deadline and was stopped.",
    "worker_memory_limit": "The LiteParse worker exceeded its sampled memory ceiling and was stopped.",
    "worker_unavailable": "A frozen executable has no LiteParse worker entry point.",
    "result_too_large": "The LiteParse result exceeded the adapter's size limit.",
    "worker_config_invalid": "LITEPARSE_WORKER_MEMORY_BYTES must be a positive integer.",
    "local_parse_failed": "LiteParse could not parse this document.",
}


class LiteParseWorkerError(Exception):
    """A fixed failure code from the isolated worker."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class LiteParseRunner(Protocol):
    def parse(self, data: bytes, *, suffix: str, options: dict, ctx: RunContext) -> dict: ...


class WorkerRunner:
    """Run one parse in a fresh WarmWorker, then close it and remove its private directory."""

    def __init__(
        self, *, memory_bytes: int = _DEFAULT_MEMORY_BYTES, command: list[str] | None = None
    ) -> None:
        self.memory_bytes = memory_bytes
        self.command = command or [
            sys.executable,
            "-m",
            "openreading.adapters.liteparse.worker",
            "--serve",
        ]
        self.last_pid: int | None = None

    def parse(self, data: bytes, *, suffix: str, options: dict, ctx: RunContext) -> dict:
        if getattr(sys, "frozen", False):
            raise LiteParseWorkerError("worker_unavailable")
        from openreading.artifacts.limits import ArtifactError
        from openreading.artifacts.supervisor import WarmWorker

        budget_ms = ctx.deadline_ms if ctx.deadline_ms is not None else _DEFAULT_DEADLINE_MS
        started = time.monotonic()
        workdir = Path(tempfile.mkdtemp(prefix="openreading-liteparse-"))
        worker = WarmWorker(
            self.command, memory_bytes=self.memory_bytes, idle_seconds=1, cwd=workdir
        )

        def check() -> None:
            if worker.pid is not None:
                self.last_pid = worker.pid
            if (time.monotonic() - started) * 1000 >= budget_ms:
                raise ArtifactError("timeout")

        try:
            source = workdir / f"input{suffix}"
            source.write_bytes(data)
            output = workdir / "result.json"
            worker.run({**options, "input": str(source), "output": str(output)}, check=check)
            if not output.is_file():
                raise LiteParseWorkerError("parse_failed")
            if output.stat().st_size > _MAX_RESULT_BYTES:
                raise LiteParseWorkerError("result_too_large")
            return json.loads(output.read_text(encoding="utf-8"))
        except ArtifactError as error:
            raise LiteParseWorkerError(error.code) from None
        finally:
            worker.close()
            shutil.rmtree(workdir, ignore_errors=True)


def _descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        id="liteparse",
        type=BackendType.OSS_LIBRARY,
        provisioning=Provisioning(byo_mode=["pip"], auth="none"),
        protocol_version=2,
        adapter_impl="subprocess",
        wait_modes=[WaitMode.INLINE],
        operations=["parse"],
        capabilities=Capabilities(
            ocr="claimed",
            layout="claimed",
            reading_order="claimed",
            printed_tables="claimed",
            forms_key_value="claimed",
            languages=["eng"],
            input_formats=["pdf", "png", "jpg", "jpeg"],
            page_range_selection=False,
        ),
        runtime=RuntimeProfile(
            offline_capable=True,
            license="Apache-2.0",
            sandbox="subprocess",
            version_pin="liteparse==2.14.4",
            hardware="cpu",
        ),
        output=Output(
            channels=OutputChannels(
                text=N,
                markdown=N,
                blocks=N,
                block_bbox=N,
                block_confidence=X,
                table_cells=N,
                typed_fields=N,
            ),
            block_granularity="element",
        ),
        config_spec=[
            ConfigField(
                key="tessdata_path",
                env=["LITEPARSE_TESSDATA"],
                description="Absolute directory with a pinned eng.traineddata. OCR stays off without it.",
            ),
            ConfigField(
                key="worker_memory_bytes",
                env=["LITEPARSE_WORKER_MEMORY_BYTES"],
                description="Sampled worker memory ceiling in bytes. Defaults to 2147483648.",
            ),
        ],
        sources=[
            Source(
                url="https://github.com/run-llama/liteparse",
                accessed="2026-09-13",
                supports="Local Rust parser with bundled PDFium and Tesseract; Apache-2.0.",
            ),
            Source(
                url="https://developers.llamaindex.ai/liteparse/guides/ocr/",
                accessed="2026-09-13",
                supports="Automatic tessdata download unless tessdata_path is provided.",
            ),
            Source(
                url="https://pypi.org/project/liteparse/2.14.4/",
                accessed="2026-09-13",
                supports="Python wheel 2.14.4: blocks, table cells, form fields, 72-DPI top-left boxes.",
            ),
        ],
    )


def _bbox(box: dict | None, page: int, width: float, height: float):
    if not box or width <= 0 or height <= 0:
        return None
    x, y = box["x"], box["y"]
    return to_canonical(
        [x, y, x + box["width"], y + box["height"]],
        origin=NativeOrigin.TOP_LEFT,
        unit=NativeUnit.PDF_POINT,
        page_width=width,
        page_height=height,
        page=page,
    )


def _refuse(code: str) -> TerminalError:
    return TerminalError(_MESSAGES[code], backend_code=code)


class LiteParseAdapter(BackendAdapter):
    def __init__(self, runner: LiteParseRunner | None = None) -> None:
        self.descriptor = _descriptor()
        self._client = runner

    def health(self) -> Health:
        if self._client is not None:
            return Health(ready=True, detail="injected runner")
        try:
            version = importlib.metadata.version("liteparse")
        except importlib.metadata.PackageNotFoundError:
            return Health(
                ready=False, missing_deps=["liteparse (pip install 'openreading[liteparse]')"]
            )
        if importlib.util.find_spec("psutil") is None:
            return Health(
                ready=False, missing_deps=["psutil (pip install 'openreading[liteparse]')"]
            )
        return Health(
            ready=True, version=version, detail="Local worker; OCR needs verified tessdata."
        )

    def _get_client(self, ctx: RunContext) -> LiteParseRunner:
        if self._client is not None:
            return self._client
        raw = (ctx.runtime or {}).get("worker_memory_bytes")
        try:
            memory = int(raw) if raw is not None else _DEFAULT_MEMORY_BYTES
        except (TypeError, ValueError):
            raise _refuse("worker_config_invalid") from None
        if memory <= 0:
            raise _refuse("worker_config_invalid")
        return WorkerRunner(memory_bytes=memory)

    def _input(self, req: OpenReadingRequest) -> tuple[bytes, str]:
        document = req.document
        if document.url or document.file_id or document.password or req.pages:
            raise _refuse("unsupported_input")
        try:
            if document.path:
                data = Path(document.path).read_bytes()
            else:
                data = base64.b64decode(document.bytes_base64 or "", validate=True)
        except (OSError, ValueError):
            raise _refuse("unsupported_input") from None
        filename = document.filename or (Path(document.path).name if document.path else None)
        mime = resolve_mime_type(mime_type=document.mime_type, filename=filename, data=data)
        suffix = _SUFFIX.get(mime or "")
        if suffix is None:
            raise _refuse("unsupported_input")
        return data, suffix

    def _ocr_options(self, req: OpenReadingRequest, ctx: RunContext) -> tuple[dict, bool]:
        features = req.features
        mode = features.ocr if features is not None else "auto"
        if mode == "force":
            raise UnsupportedFeatureError(
                "LiteParse selects OCR regions itself and cannot force full-page OCR.",
                backend_code="ocr_force_unsupported",
                feature="ocr",
            )
        if mode == "off":
            return {"ocr_enabled": False}, False
        tessdata = (ctx.runtime or {}).get("tessdata_path")
        if not tessdata:
            return {"ocr_enabled": False}, True
        languages = (features.ocr_languages if features is not None else None) or ["eng"]
        if len(languages) != 1:
            raise _refuse("ocr_language_unverified")
        try:
            pinned = verify_tessdata(str(tessdata), languages[0])
        except OcrAssetError as error:
            raise _refuse(error.code) from None
        return {
            "ocr_enabled": True,
            "tessdata_path": str(tessdata),
            "ocr_language": pinned["language"],
            "expected_sha256": pinned["sha256"],
        }, False

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        self.assert_supports(req)
        data, suffix = self._input(req)
        options, ocr_skipped = self._ocr_options(req, ctx)
        runner = self._get_client(ctx)
        try:
            raw = runner.parse(data, suffix=suffix, options=options, ctx=ctx)
        except LiteParseWorkerError as error:
            raise _refuse(_WORKER_CODES.get(error.code, "local_parse_failed")) from None
        except (RetryableError, TerminalError):
            raise
        except Exception:
            raise _refuse("local_parse_failed") from None
        job = self.new_job(
            WaitMode.INLINE, state=JobState.SUCCEEDED, idempotency_key=ctx.idempotency_key
        )
        job.raw = RawResult(
            payload={"result": raw, "ocr_skipped": ocr_skipped},
            media_type="application/vnd.openreading.liteparse+json",
            encoding="json",
            object_class="parse",
        )
        return job

    def normalize(
        self, job: Job, ctx: RunContext, slim_req: OpenReadingRequest
    ) -> NormalizedResponse:
        assert job.raw is not None
        raw = job.raw.payload["result"]
        outputs = slim_req.outputs or Outputs()
        pages: list[Page] = []
        typed: dict[str, TypedField] = {}
        has_tables = False
        for page_raw in sorted(raw.get("pages") or [], key=lambda p: p["page_num"]):
            number = page_raw["page_num"]
            width = float(page_raw.get("width") or 0.0)
            height = float(page_raw.get("height") or 0.0)
            blocks = []
            for index, block_raw in enumerate(page_raw.get("blocks") or []):
                block = self._block(block_raw, index, number, width, height, outputs)
                has_tables = has_tables or block.table is not None
                blocks.append(block)
            pages.append(
                Page(
                    page_number=number,
                    width=width or None,
                    height=height or None,
                    unit=PageUnit.PDF_POINT,
                    text=page_raw.get("text") if outputs.text else None,
                    markdown=page_raw.get("markdown") if outputs.markdown else None,
                    blocks=blocks if outputs.blocks else None,
                )
            )
            if outputs.typed_fields:
                for field in page_raw.get("form_fields") or []:
                    self._typed_field(typed, field, number, width, height)
        page_errors = raw.get("page_errors") or []
        response = NormalizedResponse(
            status=Status(state=ResponseState.PARTIAL if page_errors else ResponseState.SUCCEEDED),
            backend=BackendInfo(id="liteparse", type=BackendType.OSS_LIBRARY, version="2.14.4"),
            document=Document(
                page_count=len(pages),
                pages=pages,
                text=raw.get("text") if outputs.text else None,
                markdown="\n\n".join(p.markdown or "" for p in pages) if outputs.markdown else None,
            ),
            typed_fields=typed or None,
        )
        if job.raw.payload.get("ocr_skipped"):
            response.add_warning(
                "ocr_skipped",
                "OCR was skipped because no verified LiteParse tessdata directory is configured.",
                "features.ocr",
            )
        if page_errors:
            response.add_warning(
                "partial_conversion",
                f"LiteParse reported errors on {len(page_errors)} page(s).",
                "text",
            )
        if outputs.tables == "cells" and not (has_tables and outputs.blocks):
            response.add_warning(
                "table_cells_unavailable",
                "LiteParse returned no table block for these pages.",
                "table_cells",
            )
        if outputs.include_backend_raw:
            response.backend_raw = BackendRaw(
                encoding="json", media_type=job.raw.media_type, object_class="parse", payload=raw
            )
        provenance = {}
        for channel, emitted in (
            ("text", outputs.text),
            ("markdown", outputs.markdown),
            ("blocks", outputs.blocks),
            ("block_bbox", outputs.blocks),
            ("table_cells", outputs.blocks and has_tables),
            ("typed_fields", bool(typed)),
        ):
            if emitted:
                provenance[channel] = "native"
        response.channel_provenance = provenance or None
        return response

    def _block(
        self, raw: dict, index: int, page: int, width: float, height: float, outputs: Outputs
    ) -> Block:
        kind = raw.get("kind") or "other"
        text = raw.get("text")
        if text is None and raw.get("lines"):
            text = "\n".join(raw["lines"])
        table = None
        if kind == "table" and outputs.tables != "none":
            table = self._table(raw, page, width, height)
        return Block(
            type=_BLOCK_TYPES.get(kind, BlockType.OTHER),
            id=f"p{page}-b{index}",
            native_type=kind,
            text=text,
            bbox=_bbox(raw.get("bbox"), page, width, height),
            reading_order=index,
            table=table,
        )

    def _table(self, raw: dict, page: int, width: float, height: float) -> Table:
        header = raw.get("header") or []
        grid = ([header] if header else []) + list(raw.get("rows") or [])
        cells = [
            TableCell(
                row=row,
                col=col,
                text=cell.get("text"),
                is_header=True if header and row == 0 else None,
                bbox=_bbox(cell.get("bbox"), page, width, height),
            )
            for row, cells_in_row in enumerate(grid)
            for col, cell in enumerate(cells_in_row)
        ]
        return Table(
            n_rows=len(grid),
            n_cols=max((len(row) for row in grid), default=0),
            cells=cells,
            rows=[[cell.get("text") for cell in row] for row in grid],
        )

    def _typed_field(
        self, typed: dict[str, TypedField], field: dict, page: int, width: float, height: float
    ) -> None:
        name = field.get("name")
        key = name if name and name not in typed else str(field.get("id"))
        # Widget names and IDs share one output namespace, so either can collide with a prior key.
        base = key
        suffix = 2
        while key in typed:
            key = f"{base}#{suffix}"
            suffix += 1
        if field.get("value") is not None:
            value: Any = field["value"]
        elif field.get("checked") is not None:
            value = field["checked"]
        elif field.get("selected_options"):
            value = list(field["selected_options"])
        else:
            value = None
        typed[key] = TypedField(
            value=value,
            type=field.get("type"),
            citations=[Citation(page=page, bbox=_bbox(field.get("rect"), page, width, height))],
        )

    def report_cost(self, job: Job) -> CostReport:
        pages = ((job.raw.payload.get("result") or {}).get("pages") or []) if job.raw else []
        return infra_only("page", len(pages))
