"""Tesseract adapter — the first SubprocessAdapter. Local OCR with zero data egress (offline
compliance floor for scanned docs). pytesseract shells out to the system `tesseract` binary.

Tesseract is the LOCAL EXCEPTION that genuinely has confidence: image_to_data returns per-word
conf 0-100, so block_confidence is NATIVE (not the "deterministic parser has no confidence" case).
It has no table/layout model, so those channels are X and warned when requested.

Sandbox posture (SubprocessAdapter): the subprocess runs with a wall-clock timeout; a hardened
multi-tenant deployment should additionally run it under an OS sandbox (rlimits: RLIMIT_CPU/AS,
no network namespace, seccomp) — the runner is an injectable port so that hardened runner can be
swapped in. Untrusted input is treated as untrusted: rasterization + OCR only, never shell-eval.

Geometry: coordinates are pixels at the rasterization DPI, top-left (internal/research/openreading/_data/live_runs.md verified: title
word left=152px @150dpi ≈ 72pt). We convert with to_canonical(unit=pixel, dpi, page px dims).
"""

from __future__ import annotations

import base64
import shutil
from typing import Any, Protocol

from openreading.adapters.base import BackendAdapter
from openreading.derive import aggregate_confidence, escape_md
from openreading.types.blocks import Block
from openreading.types.cost import CostReport, infra_only
from openreading.types.descriptor import (
    AdapterDescriptor,
    BatchIntake,
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
    TextType,
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

_DEFAULT_DPI = 150
_DEFAULT_TIMEOUT_S = 60


class RasterPage:
    """One rasterized page: RGB image bytes usable by PIL/pytesseract + pixel dims + dpi.

    `source_page` is the 1-based page number in the SOURCE document — preserved under page
    subsetting so requesting pages 3-4 reports pages 3-4, not renumbered 1..k (C9)."""

    def __init__(self, image, width_px: int, height_px: int, dpi: int, source_page: int) -> None:
        self.image = image
        self.width_px = width_px
        self.height_px = height_px
        self.dpi = dpi
        self.source_page = source_page


class TsvRunner(Protocol):
    # Single OCR pass: `image_to_data` (TSV) alone is both the block source AND the text source;
    # there is no separate image_to_string pass (that would double OCR cost and desync text/blocks).
    def image_to_data(self, image, lang: str, dpi: int, timeout: int) -> dict: ...
    def version(self) -> str: ...


class _PytesseractRunner:
    """Default runner: real pytesseract → system `tesseract` binary, with a wall-clock timeout."""

    def image_to_data(
        self, image, lang: str, dpi: int, timeout: int
    ) -> dict:  # pragma: no cover - exercised live
        import pytesseract
        from pytesseract import Output

        return pytesseract.image_to_data(
            image, lang=lang, output_type=Output.DICT, timeout=timeout, config=f"--dpi {dpi}"
        )

    def version(self) -> str:  # pragma: no cover
        import pytesseract

        return str(pytesseract.get_tesseract_version())


def _descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        id="tesseract",
        type=BackendType.OSS_LIBRARY,
        # Ledger T4b (AC-8): INLINE-only, so R1's resume scenario is structurally inapplicable
        # (no non-terminal state to resume) and R2 already holds trivially (no client is ever
        # cached on self) — verified against the real R1/R2 conformance kit, not assumed.
        protocol_version=2,
        adapter_impl="subprocess",
        provisioning=Provisioning(byo_mode=["pip"], auth="none", billing_target="caller_infra"),
        wait_modes=[WaitMode.INLINE],
        capabilities=Capabilities(
            ocr="verified",
            handwriting=False,
            printed_tables=False,
            complex_tables=False,
            forms_key_value=False,
            layout=False,
            reading_order="claimed",
            multi_column=False,
            languages=["eng", "and 100+ via traineddata"],
            input_formats=["png", "jpg", "tiff", "bmp", "pdf (rasterized)"],
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
            license="Apache-2.0",
            system_deps=[
                "tesseract binary >=5",
                "traineddata langpacks",
                "pdf rasterizer (pymupdf) for PDF input",
            ],
            version_pin="pytesseract>=0.3.13",
            sandbox="subprocess",
        ),
        output=Output(
            paradigms=[OutputParadigm.ELEMENT_LIST],
            block_granularity="line",  # TSV levels 2-4 (block/par) collapsed to line-level blocks
            channels=OutputChannels(
                markdown=D,
                text=N,
                blocks=N,
                block_bbox=N,
                block_confidence=N,
                typed_fields=X,
                table_cells=X,
            ),
        ),
        router=RouterHints(
            normalization_difficulty="low",
            integration_priority="P1",
            priority_reason="Offline OCR floor for scanned loan/clinical docs; first subprocess adapter.",
        ),
        # v0.4 (Manifest v0.6): no native multi-doc API (a local subprocess). CPU-bound OCR, so cap
        # platform batch concurrency near the core count (the runner takes min(--jobs, this)).
        batch=BatchIntake(
            native=False, max_concurrency=4, notes="CPU-bound local OCR; platform batching only"
        ),
        # v0.5 (Pulse): a `local` probe — asking the tesseract BINARY for its version is a real
        # measurement of the thing that actually does the work (a subprocess that runs), not an
        # inference from configuration. No network, nothing billable. internal/design/liveness.md §3.3.
        liveness=LivenessProbe(
            probe="local",
            method="tesseract --version (via the injected runner)",
            notes="Local subprocess: no network, nothing billable. Proves the binary runs here.",
        ),
        sources=[
            Source(
                url="https://github.com/tesseract-ocr/tesseract",
                accessed="2026-07-21",
                supports="image_to_data TSV columns, pixel@dpi geometry, per-word conf",
            )
        ],
    )


class TesseractAdapter(BackendAdapter):
    def __init__(self, runner: TsvRunner | None = None, dpi: int = _DEFAULT_DPI) -> None:
        self.descriptor = _descriptor()
        self._runner = runner or _PytesseractRunner()
        self._dpi = dpi

    def health(self) -> Health:
        missing = []
        try:
            import pytesseract  # noqa: F401
        except ImportError:
            missing.append("pytesseract (pip install 'openreading[tesseract]')")
        if shutil.which("tesseract") is None and self._runner.__class__ is _PytesseractRunner:
            missing.append("tesseract binary (brew install tesseract / apt install tesseract-ocr)")
        if missing:
            return Health(ready=False, detail="missing OCR deps", missing_deps=missing)
        try:
            return Health(ready=True, version=self._runner.version())
        except Exception:  # noqa: BLE001
            return Health(ready=True)

    def probe_liveness(self, ctx: RunContext, *, timeout_s: float) -> ProbeResult:
        """`local` kind: ask the tesseract BINARY for its version through the same injectable
        runner port that does the real OCR work. That is a genuine measurement of the thing that
        actually executes — a subprocess that either runs or doesn't — and unlike `health()`'s
        `shutil.which` it proves the binary is executable here, not merely present on PATH.

        No network, no timeout to honor, nothing billable. A runner that raises means the binary
        is installed but not usable, which is `unreachable`: the component exists and did not
        answer."""
        try:
            version = self._runner.version()
        except Exception as e:  # noqa: BLE001 - a probe reports, it never throws
            return ProbeResult.unreachable(
                f"the tesseract binary did not answer ({type(e).__name__}: {e})"
            )
        return ProbeResult.live("responding — the tesseract binary runs here", version=version)

    # ---- execution -----------------------------------------------------------
    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        self.assert_supports(req)  # raises UnsupportedFeatureError for extraction_schema
        feats = req.features
        lang = "+".join(feats.ocr_languages) if (feats and feats.ocr_languages) else "eng"
        # BL-154: both `or`s below used to be bare truthiness, silently discarding an explicit
        # deadline_ms=0 ("fail fast, no time left") and any sub-second remaining budget back to
        # the full 60s default. `is not None` resolves the None-vs-0 question; the floor below
        # resolves the 0-vs-1 one: pytesseract's own timeout_manager treats a *falsy* `timeout`
        # (0) as "no timeout" — `if not seconds: yield proc.communicate()[1]` waits forever with
        # no timeout kwarg at all (verified against the installed pytesseract==0.3.13,
        # site-packages/pytesseract/pytesseract.py:141-152) — so passing 0 through would silently
        # grant UNLIMITED time, the exact opposite of the "no time left" signal being forwarded.
        # The floor is therefore 1, never 0.
        deadline_ms = ctx.deadline_ms if ctx.deadline_ms is not None else _DEFAULT_TIMEOUT_S * 1000
        timeout = max(1, int(deadline_ms / 1000))
        try:
            pages = self._rasterize(req)
        except TerminalError:
            raise
        except Exception as e:  # noqa: BLE001
            raise TerminalError(f"rasterization failed: {e}", backend_code=type(e).__name__) from e

        page_results = []
        for rp in pages:
            # Single OCR pass: the TSV is both the block source and (via its words) the text source.
            try:
                tsv = self._runner.image_to_data(rp.image, lang, rp.dpi, timeout)
            except Exception as e:  # noqa: BLE001
                raise TerminalError(f"tesseract failed: {e}", backend_code=type(e).__name__) from e
            page_results.append(
                {
                    "page": rp.source_page,  # SOURCE page number, preserved under subsetting (C9)
                    "width_px": rp.width_px,
                    "height_px": rp.height_px,
                    "dpi": rp.dpi,
                    "tsv": tsv,
                }
            )

        job = self.new_job(
            WaitMode.INLINE, state=JobState.SUCCEEDED, idempotency_key=ctx.idempotency_key
        )
        job.raw = RawResult(
            payload={"lang": lang, "pages": page_results},
            media_type="application/json",
            encoding="json",
            object_class="pytesseract.image_to_data",
        )
        return job

    def _rasterize(self, req: OpenReadingRequest) -> list[RasterPage]:
        d = req.document
        mime = (d.mime_type or "").lower()
        data = base64.b64decode(d.bytes_base64) if d.bytes_base64 else None
        if data is None and d.path:
            with open(d.path, "rb") as fh:
                data = fh.read()
        if data is None:
            raise TerminalError(
                "Tesseract needs document bytes/path (image or PDF)",
                backend_code="unsupported_input",
            )

        is_pdf = (
            mime == "application/pdf"
            or (d.path or "").lower().endswith(".pdf")
            or data[:5] == b"%PDF-"
        )
        if not is_pdf:
            from io import BytesIO

            from PIL import Image

            img = Image.open(BytesIO(data)).convert("RGB")
            return [RasterPage(img, img.width, img.height, self._dpi, source_page=1)]

        # PDF → rasterize each page with PyMuPDF (lazy; a real rasterizer dep)
        try:
            import fitz
        except ImportError as e:  # pragma: no cover
            raise TerminalError(
                "PDF OCR needs a rasterizer (pip install pymupdf)", backend_code="no_rasterizer"
            ) from e
        from io import BytesIO

        from PIL import Image

        doc = fitz.open(stream=data, filetype="pdf")
        selected = self._selected_pages(doc.page_count, req)
        out = []
        for pno in selected:  # pno is a 0-based page index into the source document
            pix = doc[pno].get_pixmap(dpi=self._dpi)
            img = Image.open(BytesIO(pix.tobytes("png"))).convert("RGB")
            out.append(RasterPage(img, pix.width, pix.height, self._dpi, source_page=pno + 1))
        doc.close()
        return out

    def _selected_pages(self, n: int, req: OpenReadingRequest) -> list[int]:
        if req.pages is None:
            return list(range(n))
        idx = []
        for rng in req.pages.ranges or []:
            # Clamp BEFORE building the list: the requested span is caller-controlled and
            # unbounded, the document's page count is not.
            end = min(rng.end or rng.start, n)
            idx += [p - 1 for p in range(rng.start, end + 1)]
        idx = idx or list(range(n))
        if req.pages.max_pages:
            idx = idx[: req.pages.max_pages]
        return sorted(set(idx))

    # ---- normalize -----------------------------------------------------------
    def normalize(
        self, job: Job, ctx: RunContext, slim_req: OpenReadingRequest
    ) -> NormalizedResponse:
        data: dict[str, Any] = job.raw.payload if job.raw else {}
        outputs = slim_req.outputs or Outputs()
        pages, text_parts, md_parts = [], [], []

        for pr in data.get("pages", []):
            blocks = self._tsv_to_blocks(
                pr["tsv"], pr["width_px"], pr["height_px"], pr["dpi"], pr["page"]
            )
            # C11: the plain text channel is the block spine — SAME TSV that produced the blocks,
            # not a second OCR pass. C1: OCR words are already plain (no markup to strip).
            ptext = "\n".join(b.text or "" for b in blocks)
            pages.append(
                Page(
                    page_number=pr["page"],
                    width=pr["width_px"],
                    height=pr["height_px"],
                    unit=PageUnit.PIXEL,
                    dpi=pr["dpi"],
                    blocks=blocks,
                    text=ptext if outputs.text else None,
                )
            )
            text_parts.append(ptext)
            # C3: the structure-free markdown channel is the escaped literal text (no invented
            # structure) so a stray OCR metacharacter can never be reparsed as markup.
            md_parts.append(escape_md(ptext))

        resp = NormalizedResponse(
            status=Status(state=ResponseState.SUCCEEDED),
            backend=BackendInfo(
                id="tesseract",
                type=BackendType.OSS_LIBRARY,
                output_paradigm=[OutputParadigm.ELEMENT_LIST],
            ),
            document=Document(
                text="\n\n".join(text_parts) if outputs.text else None,
                markdown="\n\n".join(md_parts) if outputs.markdown else None,
                page_count=len(pages),
                pages=pages,
            ),
        )
        if outputs.typed_fields:
            resp.add_warning(
                "typed_fields_unsupported",
                "Tesseract is an OCR engine, not an extractor",
                "typed_fields",
            )
        if slim_req.features and slim_req.features.tables:
            resp.add_warning(
                "tables_unsupported", "Tesseract has no table model (word soup only)", "tables"
            )
        if outputs.include_backend_raw:
            resp.backend_raw = BackendRaw(
                encoding="json",
                media_type="application/json",
                object_class="pytesseract.image_to_data",
                payload=data,
            )
        # v0.3 provenance (§3.3/§6.4): text/blocks/geometry/confidence are tesseract's native OCR
        # output (words + pixel bboxes + per-word conf); markdown is the platform's escaped
        # projection of that text (an OCR engine emits no markdown of its own).
        resp.channel_provenance = {
            "text": "native",
            "markdown": "derived",
            "blocks": "native",
            "block_bbox": "native",
            "block_confidence": "native",
        }
        return resp

    def _tsv_to_blocks(self, tsv: dict, w: int, h: int, dpi: int, page: int) -> list[Block]:
        n = len(tsv.get("text", []))
        lines: dict[tuple, dict] = {}
        for i in range(n):
            if int(tsv["level"][i]) != 5:  # 5 = word
                continue
            word = (tsv["text"][i] or "").strip()
            conf = float(tsv["conf"][i])
            if not word or conf < 0:
                continue
            key = (tsv["block_num"][i], tsv["par_num"][i], tsv["line_num"][i])
            L, T = int(tsv["left"][i]), int(tsv["top"][i])
            R, B = L + int(tsv["width"][i]), T + int(tsv["height"][i])
            ln = lines.setdefault(
                key, {"words": [], "confs": [], "x0": L, "y0": T, "x1": R, "y1": B}
            )
            ln["words"].append(word)
            ln["confs"].append(conf)
            ln["x0"], ln["y0"] = min(ln["x0"], L), min(ln["y0"], T)
            ln["x1"], ln["y1"] = max(ln["x1"], R), max(ln["y1"], B)

        blocks = []
        for order, (_, ln) in enumerate(sorted(lines.items())):
            bbox = to_canonical(
                [ln["x0"], ln["y0"], ln["x1"], ln["y1"]],
                origin=NativeOrigin.TOP_LEFT,
                unit=NativeUnit.PIXEL,
                page_width=w,
                page_height=h,
                page=page,
                dpi=dpi,
            )
            # C7/§4.5: line confidence is the MIN of member word confidences (a usable quality
            # floor — mean would hide one garbage word), rescaled from tesseract's 0-100 to [0,1].
            agg = aggregate_confidence(ln["confs"])
            blocks.append(
                Block(
                    type=BlockType.TEXT,
                    native_type="line",
                    text=" ".join(ln["words"]),
                    bbox=bbox,
                    confidence=agg / 100.0 if agg is not None else None,
                    reading_order=order,
                    text_type=TextType.PRINTED,
                )
            )
        return blocks

    def report_cost(self, job: Job) -> CostReport:
        pages = len((job.raw.payload or {}).get("pages", [])) if job.raw else 0
        return infra_only("page", float(pages))
