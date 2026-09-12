"""Convert local bytes with an explicit CPU Docling pipeline and verified local assets.

The existing docling adapter still speaks HTTP. This adapter never downloads inputs
or models. Configuration comes from its declared broker fields or an explicit Python
configuration. Automatic OCR uses that configuration's OCR default, which environment setup enables
when both Tesseract paths are supplied. Otherwise automatic requests disclose skipped OCR.
The force setting rasterizes every page for OCR and requires both Tesseract paths.
The off setting disables OCR even when the adapter's configured default enables it.
Text and physical-page blocks are supported. Table structure, confidence, typed fields,
and markdown are omitted with warnings because this profile cannot establish them.
Running headers, footers, and page numbers are Docling furniture, omitted with a warning.
Sources: https://docling-project.github.io/docling/usage/advanced_options/ (2026-09-10).
"""

from __future__ import annotations

import base64
import importlib.metadata
from pathlib import Path
from typing import Protocol

from openreading.adapters.base import BackendAdapter
from openreading.adapters.docling_local.config import LocalDoclingConfig
from openreading.adapters.docling_local.projection import project_document
from openreading.types.cost import infra_only
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
from openreading.types.enums import BackendType, ChannelGrade, JobState, WaitMode
from openreading.types.errors import TerminalError
from openreading.types.request import OpenReadingRequest, Outputs
from openreading.types.runtime import Health, RawResult, RunContext


class LocalClient(Protocol):
    def convert(self, data: bytes) -> dict: ...


def _descriptor():
    x = ChannelGrade.IMPOSSIBLE
    return AdapterDescriptor(
        id="docling_local",
        type=BackendType.OSS_LIBRARY,
        provisioning=Provisioning(byo_mode=["pip", "weights"], auth="none"),
        protocol_version=2,
        adapter_impl="in_process",
        wait_modes=[WaitMode.INLINE],
        capabilities=Capabilities(
            ocr="verified",
            layout="verified",
            reading_order="claimed",
            input_formats=["pdf"],
            page_range_selection=False,
        ),
        runtime=RuntimeProfile(
            offline_capable=True,
            license="MIT",
            sandbox="in_process",
            version_pin="docling-slim==2.126.0",
            system_deps=["Tesseract when OCR is enabled"],
        ),
        output=Output(
            channels=OutputChannels(
                text=ChannelGrade.DERIVABLE,
                blocks=ChannelGrade.NATIVE,
                block_bbox=ChannelGrade.NATIVE,
                markdown=x,
                table_cells=x,
                block_confidence=x,
                typed_fields=x,
            )
        ),
        config_spec=[
            ConfigField(
                key="assets_path",
                required=True,
                env=["DOCLING_LOCAL_ASSETS"],
                description="Absolute verified local model directory.",
            ),
            ConfigField(
                key="tesseract_cmd",
                env=["DOCLING_LOCAL_TESSERACT"],
                description="Explicit OCR executable path.",
            ),
            ConfigField(
                key="tessdata_path",
                env=["DOCLING_LOCAL_TESSDATA"],
                description="Explicit OCR language data directory.",
            ),
        ],
        sources=[
            Source(
                url="https://docling-project.github.io/docling/usage/advanced_options/",
                accessed="2026-09-10",
                supports="Local artifacts and disabled remote services; local feasibility verifies CPU/OCR execution.",
            )
        ],
    )


class DoclingLocalAdapter(BackendAdapter):
    def __init__(
        self, *, config: LocalDoclingConfig | None = None, client: LocalClient | None = None
    ):
        self.descriptor = _descriptor()
        self.config = config
        self._client = client

    def health(self):
        try:
            version = importlib.metadata.version("docling-slim")
            return Health(
                ready=True, version=version, detail="Local assets are checked before conversion."
            )
        except importlib.metadata.PackageNotFoundError:
            return Health(
                ready=self._client is not None,
                missing_deps=[] if self._client else ["openreading[docling-local]"],
            )

    def submit(self, req: OpenReadingRequest, ctx: RunContext):
        self.assert_supports(req)
        missing_ocr_setup = False
        try:
            if req.document.url or req.document.password or req.pages:
                raise ValueError("Unsupported local input.")
            data = (
                Path(req.document.path).read_bytes()
                if req.document.path
                else base64.b64decode(req.document.bytes_base64 or "", validate=True)
            )
            client = self._client
            if client is None:
                from openreading.adapters.docling_local.client import convert_shared

                runtime = ctx.runtime or {}
                config = self.config or LocalDoclingConfig(
                    artifacts_path=Path(runtime["assets_path"]),
                    ocr=bool(runtime.get("tesseract_cmd") and runtime.get("tessdata_path")),
                    tesseract_cmd=Path(runtime["tesseract_cmd"])
                    if runtime.get("tesseract_cmd")
                    else None,
                    tessdata_path=Path(runtime["tessdata_path"])
                    if runtime.get("tessdata_path")
                    else None,
                )
                requested = req.features.ocr if req.features else "auto"
                mode = "off" if requested == "auto" and not config.ocr else requested
                if mode != "off" and not (config.tesseract_cmd and config.tessdata_path):
                    missing_ocr_setup = True
                    raise ValueError("Missing local OCR setup.")
                raw = convert_shared(config, data, ocr_mode=mode)
                if requested == "auto" and mode == "off":
                    raw = {**raw, "ocr_skipped": True}
            else:
                raw = client.convert(data)
        except Exception:
            raise TerminalError(
                "Local OCR requires DOCLING_LOCAL_TESSERACT and DOCLING_LOCAL_TESSDATA, "
                "or both paths in the explicit adapter configuration."
                if missing_ocr_setup
                else "Local Docling could not process this input or configuration.",
                backend_code="ocr_unavailable" if missing_ocr_setup else "local_conversion_failed",
            ) from None
        job = self.new_job(
            WaitMode.INLINE, state=JobState.SUCCEEDED, idempotency_key=ctx.idempotency_key
        )
        job.raw = RawResult(
            payload=raw,
            media_type="application/vnd.openreading.docling-local+json",
            encoding="json",
        )
        return job

    def normalize(self, job, ctx, slim_req):
        assert job.raw is not None
        response, _ = project_document(job.raw.payload, slim_req.outputs or Outputs())
        if job.raw.payload.get("ocr_skipped"):
            response.add_warning(
                "ocr_skipped",
                "Automatic OCR was skipped because local OCR is disabled or Tesseract paths "
                "are not configured.",
                "features.ocr",
            )
        return response

    def report_cost(self, job):
        assert job.raw is not None
        return infra_only("page", len(job.raw.payload["pages"]))
