"""PyMuPDF (fitz) adapter — the first InProcessAdapter. Born-digital PDF text/tables/layout,
executed in-process with zero data egress (the local compliance floor).

License: PyMuPDF is AGPL-3.0. It is isolated in the `pymupdf` optional extra, imported lazily
INSIDE this module (never by core), and flagged in the descriptor so the router can surface it.

Geometry: PyMuPDF reports top-left/y-down PDF points with ascender-inflated span boxes
(internal/research/openreading/_data/live_runs.md: the title span top is 58.5 = 80 − 20×1.075, not
the tight glyph box). Every box goes through to_canonical (top_left, pdf_point) to canonical [0,1]
with bbox_native, so the inflation is preserved rather than fabricated away. confidence is
structurally impossible for a deterministic parser → channel X + a warning, never a fake score.

Concurrency: PyMuPDF extraction is NOT thread-safe. find_tables() flips the process-global
`pymupdf._globals.small_glyph_heights` (a plain module attribute, not thread-local) to True for
its whole duration and parks the page's characters in module-global lists that Table.extract() /
Table.header read back. A get_text('dict') racing that window silently returns tight glyph boxes
(title top 64.35 instead of 58.5), and two interleaved save/restores leave the flag stuck True —
poisoning every LATER parse in the process, with no error or warning. Reachable from
`batch --jobs>1` and the server's concurrent dispatch, so extraction is serialized process-wide.
"""

from __future__ import annotations

import base64
import threading
from pathlib import PurePath
from typing import Any

from openreading.adapters.base import BackendAdapter
from openreading.derive import escape_md, order_by_position, table_to_pipe_md, table_to_text
from openreading.types.blocks import Block, Table, TableCell
from openreading.types.cost import CostReport, infra_only
from openreading.types.descriptor import (
    AdapterDescriptor,
    Capabilities,
    ComplianceProfile,
    Cost,
    LivenessProbe,
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
    PageUnit,
    ResponseState,
    WaitMode,
)
from openreading.types.errors import TerminalError
from openreading.types.geometry import to_canonical
from openreading.types.job import Job
from openreading.types.liveness import ProbeResult
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

X = ChannelGrade.IMPOSSIBLE
N = ChannelGrade.NATIVE
D = ChannelGrade.DERIVABLE

# Serializes get_text() + find_tables() + reading find_tables' results, which together race
# pymupdf's process-global state — see the module docstring. Module-level on purpose: the state
# it guards is the library's, shared by every adapter instance in the process.
_EXTRACT_LOCK = threading.Lock()


def _descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        id="pymupdf",
        type=BackendType.OSS_LIBRARY,
        # Ledger T4b (AC-8): INLINE-only, so R1's resume scenario is structurally inapplicable
        # (no non-terminal state to resume) and R2 already holds trivially (no client is ever
        # cached on self) — verified against the real R1/R2 conformance kit, not assumed.
        protocol_version=2,
        adapter_impl="in_process",
        provisioning=Provisioning(byo_mode=["pip"], auth="none", billing_target="caller_infra"),
        wait_modes=[WaitMode.INLINE],
        capabilities=Capabilities(
            ocr=False,  # born-digital only; no OCR
            printed_tables="verified",  # find_tables verified against live run
            complex_tables=False,
            layout="verified",
            reading_order="verified",
            multi_column="claimed",
            figures_charts="verified",
            input_formats=["pdf", "xps", "epub", "mobi", "cbz", "svg"],
            max_pages_per_request="unbounded",
        ),
        cost=Cost(native_unit="cpu_second", basis="infra_only", usd_per_page_equiv_low=0.0),
        compliance=ComplianceProfile(
            hipaa_baa="na_local",
            trains_on_customer_data="na_local",
            runs_fully_local=True,
            data_region_options=["*"],
            max_retention_hours=0,
        ),
        runtime=RuntimeProfile(
            offline_capable=True,
            license="AGPL-3.0",  # copyleft — isolated in the `pymupdf` extra
            system_deps=[],
            version_pin="pymupdf>=1.24",
            sandbox="in_process",
        ),
        output=Output(
            channels=OutputChannels(
                markdown=D,
                text=N,
                blocks=N,
                block_bbox=N,
                block_confidence=X,
                typed_fields=X,
                table_cells=N,
            ),
            block_granularity="paragraph",  # get_text('dict') blocks are paragraph-grained
        ),
        router=RouterHints(
            normalization_difficulty="medium",
            integration_priority="P0",
            priority_reason="Local zero-cost floor for born-digital loan files; first in-process adapter.",
        ),
        # v0.5 (Pulse): a `local` probe — the backend is a library in this process, so liveness is
        # a real measurement (does it import and report a version?) rather than an inference, and
        # it costs nothing. internal/design/liveness.md §3.3.
        liveness=LivenessProbe(
            probe="local",
            method="import pymupdf",
            notes="In-process: no network, no timeout, nothing billable. Instant and free to run.",
        ),
        sources=[
            Source(
                url="https://pymupdf.readthedocs.io/",
                accessed="2026-07-21",
                supports="get_text('dict') shape, find_tables, AGPL license",
            )
        ],
    )


def _rect_contains(outer: tuple[float, ...], inner_bbox: list[float], frac: float = 0.6) -> bool:
    """True if >= `frac` of inner_bbox's area overlaps outer (used to drop table text blocks)."""
    ox0, oy0, ox1, oy1 = outer[:4]
    ix0, iy0, ix1, iy1 = inner_bbox[:4]
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    if iw == 0 or ih == 0:
        return False
    inter = max(0.0, min(ox1, ix1) - max(ox0, ix0)) * max(0.0, min(oy1, iy1) - max(oy0, iy0))
    return inter / (iw * ih) >= frac


class PyMuPDFAdapter(BackendAdapter):
    def __init__(self) -> None:
        self.descriptor = _descriptor()

    # ---- lifecycle -----------------------------------------------------------
    def health(self) -> Health:
        try:
            import pymupdf as fitz  # noqa: F401
        except ImportError:
            return Health(
                ready=False,
                detail="PyMuPDF not installed",
                missing_deps=["pymupdf (pip install 'openreading[pymupdf]')"],
            )
        import pymupdf as fitz

        return Health(ready=True, version=getattr(fitz, "__version__", None))

    def probe_liveness(self, ctx: RunContext, *, timeout_s: float) -> ProbeResult:
        """`local` kind: the "backend" is a library in this very process, so liveness is answerable
        instantly, for free, with no network and no timeout to honor — importing it IS contacting
        it, and a successful import is a real measurement, not an inference.

        This is why pymupdf is not one of the `not_supported` cases despite declaring no
        credentials: it has nothing to infer FROM, but it does have something to measure
        (internal/design/liveness.md §2 M4)."""
        try:
            import pymupdf as fitz
        except ImportError as e:  # pragma: no cover - readiness already gates this branch
            return ProbeResult.unreachable(f"pymupdf is not importable here ({e})")
        return ProbeResult.live(
            "responding, the library imports and runs in this process",
            version=getattr(fitz, "__version__", None),
        )

    # ---- execution (INLINE: submit does the work) ----------------------------
    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        self.assert_supports(req)  # raises UnsupportedFeatureError for extraction_schema
        self._assert_readable_format(req)
        try:
            import pymupdf as fitz
        except ImportError as e:  # pragma: no cover - environment guard
            raise TerminalError("PyMuPDF not installed", backend_code="import_error") from e

        # _open stays INSIDE the try: fitz raises FileDataError/EmptyFileError (plain RuntimeErrors)
        # on a corrupt stream, and hoisted out of here they escape the taxonomy entirely — a bare
        # 500 at /v1/parse and an unhandled crash in the executor's narrow except tuple.
        doc = None
        try:
            doc = self._open(fitz, req)
            if req.document.password:
                doc.authenticate(req.document.password)
            intermediate = self._extract(doc, req)
        except TerminalError:
            raise
        except Exception as e:  # noqa: BLE001 - map any fitz failure to Terminal
            raise TerminalError(f"PyMuPDF failed: {e}", backend_code=type(e).__name__) from e
        finally:
            if doc is not None:
                doc.close()

        job = self.new_job(WaitMode.INLINE, state=JobState.SUCCEEDED)
        job.raw = RawResult(
            payload=intermediate,
            media_type="application/vnd.openreading.pymupdf+json",
            object_class="fitz.Page.get_text.dict",
            encoding="json_serialized_object",
        )
        return job

    def _assert_readable_format(self, req: OpenReadingRequest) -> None:
        """Refuse a file this backend does not read, by name, before PyMuPDF is asked to open it.

        A .txt, a .docx and a truncated PDF all come back from PyMuPDF as `Failed to open stream`,
        which names neither the problem nor the fix. The folder path already answers honestly, with
        `skip_reason: unsupported_format`, so the single-document path answers the same way. The
        format list is read from the descriptor rather than restated here, so the message cannot
        drift from the catalog. A supported extension whose bytes are corrupt still falls through
        to the generic PyMuPDF error below, because the extension is all this check can see.
        """
        # `filename` is what every caller above the adapter carries: the CLI and the Python API
        # read a path into `bytes_base64` and keep the name here (`api._document_dict`), and only
        # a hand-built request still holds `path`. Read both, so the check is not silently dead
        # on the surface a newcomer actually types.
        source = req.document.filename or req.document.path
        if not source:
            return
        name = PurePath(source).name
        ext = PurePath(name).suffix.lstrip(".").lower()
        formats = self.descriptor.capabilities.input_formats
        if not ext or ext in formats:
            return
        raise TerminalError(
            f"pymupdf cannot read {name}. It reads {', '.join(formats)}, and this file is not one "
            "of them.",
            backend_code="unsupported_format",
        )

    def _open(self, fitz, req: OpenReadingRequest):
        d = req.document
        if d.path:
            return fitz.open(d.path)
        if d.bytes_base64:
            return fitz.open(stream=base64.b64decode(d.bytes_base64), filetype="pdf")
        raise TerminalError(
            "PyMuPDF is in-process: provide document.path or document.bytes_base64 "
            "(url must be downloaded by the router first)",
            backend_code="unsupported_input",
        )

    def _selected_pages(self, doc, req: OpenReadingRequest) -> list[int]:
        n = doc.page_count
        if req.pages is None:
            idx = list(range(n))
        else:
            idx = []
            for rng in req.pages.ranges or []:
                # Clamp BEFORE building the list: the requested span is caller-controlled and
                # unbounded, the document's page count is not.
                end = min(rng.end or rng.start, n)
                idx += [p - 1 for p in range(rng.start, end + 1)]
            # An entirely out-of-range selection would leave nothing to parse, so fall back to the
            # whole document rather than returning an empty success the caller cannot distinguish
            # from a blank PDF.
            if not idx:
                idx = list(range(n))
            if req.pages.max_pages:
                idx = idx[: req.pages.max_pages]
        return sorted(set(idx))

    def _extract(self, doc, req: OpenReadingRequest) -> dict[str, Any]:
        with _EXTRACT_LOCK:
            return self._extract_serialized(doc, req)

    def _extract_serialized(self, doc, req: OpenReadingRequest) -> dict[str, Any]:
        """Caller MUST hold `_EXTRACT_LOCK`. Everything that touches pymupdf's process-global
        state lives here, and only plain JSON-able values leave — no fitz object outlives the
        lock, so the next find_tables() clearing those globals cannot corrupt this payload."""
        pages_out = []
        for pno in self._selected_pages(doc, req):
            page = doc[pno]
            d = page.get_text("dict")
            raw_blocks = []
            for b in d["blocks"]:
                if b.get("type") == 1:  # image block: drop bytes, keep a ref
                    raw_blocks.append(
                        {
                            "type": 1,
                            "number": b.get("number"),
                            "bbox": list(b.get("bbox", [])),
                            "width": b.get("width"),
                            "height": b.get("height"),
                            "ext": b.get("ext"),
                            "image_ref": f"page{pno}:img{b.get('number')}:{b.get('size')}b",
                        }
                    )
                else:
                    raw_blocks.append(
                        {
                            "type": 0,
                            "number": b.get("number"),
                            "bbox": list(b.get("bbox", [])),
                            "lines": [
                                {
                                    "bbox": list(ln.get("bbox", [])),
                                    "spans": [
                                        {
                                            "text": sp.get("text", ""),
                                            "bbox": list(sp.get("bbox", [])),
                                            "size": sp.get("size"),
                                            "font": sp.get("font"),
                                        }
                                        for sp in ln.get("spans", [])
                                    ],
                                }
                                for ln in b.get("lines", [])
                            ],
                        }
                    )
            tables = []
            try:
                for t in page.find_tables().tables:
                    hdr = getattr(t, "header", None)
                    header = None
                    if hdr is not None:
                        header = {
                            # pymupdf gives the real header (`Table.header`); we carry its cell
                            # geometry so is_header is set from the provider signal, not row 0.
                            "external": bool(getattr(hdr, "external", False)),
                            "names": list(getattr(hdr, "names", None) or []),
                            "cells": [
                                [float(x) for x in c]
                                for c in (getattr(hdr, "cells", None) or [])
                                if c
                            ],
                        }
                    tables.append(
                        {
                            "bbox": [float(x) for x in t.bbox],
                            "row_count": t.row_count,
                            "col_count": t.col_count,
                            "extract": t.extract(),
                            "cells": [list(c) if c else None for c in t.cells],
                            "header": header,
                        }
                    )
            except Exception:  # noqa: BLE001 - table detection is best-effort
                pass
            pages_out.append(
                {
                    "number": pno,
                    "width": float(d["width"]),
                    "height": float(d["height"]),
                    "text": page.get_text("text"),
                    "blocks": raw_blocks,
                    "tables": tables,
                }
            )
        return {
            "metadata": dict(doc.metadata or {}),
            "page_count": doc.page_count,
            "pages": pages_out,
        }

    # ---- transform -----------------------------------------------------------
    def normalize(
        self, job: Job, ctx: RunContext, slim_req: OpenReadingRequest
    ) -> NormalizedResponse:
        data: dict[str, Any] = job.raw.payload if job.raw else {}
        title = (data.get("metadata") or {}).get("title", "")
        outputs = slim_req.outputs or Outputs()

        pages: list[Page] = []
        doc_md_parts: list[str] = []
        doc_text_parts: list[str] = []

        for p in data.get("pages", []):
            w, h = p["width"], p["height"]
            page_no = p["number"] + 1
            table_regions = [tuple(t["bbox"]) for t in p["tables"]]
            blocks: list[Block] = []

            for rb in p["blocks"]:
                bbox_native = rb["bbox"]
                if rb["type"] == 1:
                    blocks.append(
                        Block(
                            type=BlockType.IMAGE,
                            native_type="image",
                            bbox=_canon(bbox_native, w, h, page_no),
                        )
                    )
                    continue
                # skip text blocks that live inside a detected table (avoid double-counting)
                if any(_rect_contains(tr, bbox_native) for tr in table_regions):
                    continue
                text = " ".join(sp["text"] for ln in rb["lines"] for sp in ln["spans"]).strip()
                if not text:
                    continue
                btype = BlockType.TITLE if title and text == title else BlockType.TEXT
                # markdown channel: escape literal body text so content can't become markup (C3)
                md = f"# {escape_md(text)}" if btype is BlockType.TITLE else escape_md(text)
                blocks.append(
                    Block(
                        type=btype,
                        native_type="text",
                        text=text,
                        markdown=md,
                        bbox=_canon(bbox_native, w, h, page_no),
                    )
                )

            for t in p["tables"]:
                table = _build_table(t, w, h, page_no)
                blocks.append(
                    Block(
                        type=BlockType.TABLE,
                        native_type="table",
                        text=table_to_text(table),  # C1/C2: plain tab-joined rows in the text spine
                        markdown=table_to_pipe_md(table),  # escaped pipe table (C3)
                        bbox=_canon(t["bbox"], w, h, page_no),
                        table=table,
                    )
                )

            # interleave everything into document reading order by bbox position (P5): pymupdf
            # has no span offsets, so order_by_position falls back to (page, y, x). This fixes the
            # "tables appended after all text" bug — a paragraph below a table now follows it.
            ordered = order_by_position(blocks, [None] * len(blocks))
            md_parts: list[str] = []
            for i, b in enumerate(ordered):
                b.reading_order = i
                if b.markdown:
                    md_parts.append(b.markdown)

            page_text = p["text"].strip()
            page_md = "\n\n".join(md_parts)
            pages.append(
                Page(
                    page_number=page_no,
                    width=w,
                    height=h,
                    unit=PageUnit.PDF_POINT,
                    blocks=ordered,
                    text=page_text if outputs.text else None,
                    markdown=page_md if outputs.markdown else None,
                )
            )
            doc_text_parts.append(page_text)
            doc_md_parts.append(page_md)

        resp = NormalizedResponse(
            status=Status(state=ResponseState.SUCCEEDED),
            backend=BackendInfo(
                id="pymupdf",
                type=BackendType.OSS_LIBRARY,
                output_paradigm=[OutputParadigm.BLOCK_TREE],
            ),
            document=Document(
                markdown="\n\n".join(doc_md_parts) if outputs.markdown else None,
                text="\n\n".join(doc_text_parts) if outputs.text else None,
                page_count=data.get("page_count"),
                pages=pages,
            ),
        )
        # deterministic parser: confidence is structurally impossible — warn, never fabricate
        resp.add_warning(
            "confidence_unavailable",
            "PyMuPDF is a deterministic parser; per-element confidence does not exist",
            "block_confidence",
        )
        if outputs.typed_fields:
            resp.add_warning(
                "typed_fields_unsupported",
                "PyMuPDF extracts structure, not typed fields; use an extraction backend",
                "typed_fields",
            )
        if slim_req.outputs and slim_req.outputs.include_backend_raw is False:
            pass
        else:
            resp.backend_raw = BackendRaw(
                encoding="json_serialized_object",
                media_type="application/vnd.openreading.pymupdf+json",
                object_class="fitz.Page.get_text.dict",
                payload=job.raw.payload if job.raw else None,
            )
        # deterministic parser: text/blocks/geometry/cells are read straight from the PDF (native);
        # only the markdown channel (pipe tables + escaped body) is derived here.
        resp.channel_provenance = {
            "markdown": "derived",
            "text": "native",
            "blocks": "native",
            "block_bbox": "native",
            "table_cells": "native",
        }
        return resp

    def report_cost(self, job: Job) -> CostReport:
        pages = len((job.raw.payload or {}).get("pages", [])) if job.raw else 0
        return infra_only("page", float(pages))


def _canon(bbox_native: list[float], page_width: float, page_height: float, page: int):
    return to_canonical(
        bbox_native,
        origin=NativeOrigin.TOP_LEFT,
        unit=NativeUnit.PDF_POINT,
        page_width=page_width,
        page_height=page_height,
        page=page,
    )


def _bbox_key(bb: list[float]) -> tuple[float, ...]:
    return tuple(round(float(v), 1) for v in bb[:4])


def _build_table(t: dict[str, Any], page_width: float, page_height: float, page: int) -> Table:
    """Build the canonical Table. Two fidelity fixes over the naive path:
    - (row, col) come from cell geometry, not `divmod(i, col_count)`: pymupdf emits `t.cells`
      column-major, so the old mapping mislabelled every cell but (0,0) — text↔bbox desynced.
    - `is_header` is set from `Table.header` cell geometry (the provider signal), never `row == 0`.
    """
    rows: list[list[str | None]] = t["extract"]
    row_count = t["row_count"] or (len(rows) if rows else 0)
    col_count = t["col_count"] or (len(rows[0]) if rows and rows[0] else 0)
    cell_bboxes: list[list[float] | None] = t["cells"]
    present = [cb for cb in cell_bboxes if cb]
    ys = sorted({round(float(cb[1]), 2) for cb in present})
    xs = sorted({round(float(cb[0]), 2) for cb in present})
    header = t.get("header") or {}
    header_keys = {_bbox_key(cb) for cb in (header.get("cells") or [])}

    cells: list[TableCell] = []
    for i, cbbox in enumerate(cell_bboxes):
        if cbbox and round(float(cbbox[1]), 2) in ys and round(float(cbbox[0]), 2) in xs:
            r = ys.index(round(float(cbbox[1]), 2))
            c = xs.index(round(float(cbbox[0]), 2))
        else:  # no geometry to place it: fall back to pymupdf's column-major flat order
            c, r = divmod(i, row_count) if row_count else (0, 0)
        cell_text = rows[r][c] if r < len(rows) and c < len(rows[r]) else None
        cells.append(
            TableCell(
                row=r,
                col=c,
                text=cell_text,
                is_header=(_bbox_key(cbbox) in header_keys) if cbbox else False,
                bbox=_canon(cbbox, page_width, page_height, page) if cbbox else None,
            )
        )
    return Table(n_rows=row_count, n_cols=col_count, cells=cells, rows=rows)
