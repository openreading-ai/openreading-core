"""Qwen-VL adapter — a self-hosted VLM behind a BYO OpenAI-compatible endpoint (vLLM / Ollama /
SGLang). Because the endpoint runs in the caller's own infra, data never leaves the environment
(runs_fully_local=True). Token-stream paradigm: the model GENERATES the output — there are no
calibrated per-element scores, so block_confidence is X and never fabricated; generation is
non-deterministic (conformance runs with deterministic=False).

Doc parsing is a trained skill: the 'qwenvl html' prompt emits layout-aware HTML with
`<div class="text|table|image|title" data-bbox="x0 y0 x1 y1">…</div>` (verbatim from the repo
cookbook), which we parse into blocks with bbox; 'qwenvl markdown' emits markdown; an
extraction_schema drives a JSON-extraction prompt → typed_fields. Qwen3-VL weights are Apache-2.0.
"""

from __future__ import annotations

import base64
import html as _htmllib
import json
from html.parser import HTMLParser
from typing import Any, Protocol

from openreading.adapters.base import BackendAdapter
from openreading.derive import (
    escape_md,
    html_table_to_table,
    md_to_text,
    table_to_pipe_md,
    table_to_text,
)
from openreading.liveness import probe_http
from openreading.types.blocks import Block, TypedField
from openreading.types.cost import CostReport, infra_only
from openreading.types.descriptor import (
    AdapterDescriptor,
    Capabilities,
    ComplianceProfile,
    ConfigField,
    Cost,
    CredentialField,
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
from openreading.types.errors import RetryableError, TerminalError
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
_HTML_PROMPT = "QwenVL HTML"
_MD_PROMPT = "QwenVL Markdown"
_DIV_CLASS_MAP = {
    "title": BlockType.TITLE,
    "text": BlockType.TEXT,
    "table": BlockType.TABLE,
    "image": BlockType.IMAGE,
    "figure": BlockType.FIGURE,
    "header": BlockType.HEADER,
    "footer": BlockType.FOOTER,
    "list": BlockType.LIST,
    "formula": BlockType.FORMULA,
}

# v0.3 channel_provenance (§3.3/§6.4) for the ACTIVE mode: which channels the model produced
# natively vs which the platform derived. html-mode markdown/text/cells are DERIVED from the
# model's layout HTML; markdown-mode markdown is NATIVE; extract-mode typed_fields is native.
_PROVENANCE = {
    "html": {
        "blocks": "native",
        "block_bbox": "native",
        "markdown": "derived",
        "text": "derived",
        "table_cells": "derived",
    },
    "markdown": {"markdown": "native", "text": "derived"},
    "extract": {"typed_fields": "native"},
}

# The channel set each mode can produce (mode exclusivity, §6.5) — drives the C6 warning.
_MODE_PRODUCES = {
    "html": frozenset({"markdown", "text", "blocks", "table_cells"}),
    "markdown": frozenset({"markdown", "text"}),
    "extract": frozenset({"typed_fields"}),
}

# The channels a caller can explicitly request (mirrors the conformance kit's _WARN_WHEN).
_REQUESTED = {
    "markdown": lambda o: o.markdown,
    "text": lambda o: o.text,
    "blocks": lambda o: o.blocks,
    "typed_fields": lambda o: o.typed_fields,
    "table_cells": lambda o: o.tables == "cells",
}


class QwenVLClient(Protocol):
    # returns (content, finish_reason); finish_reason carries the truncation signal (C10).
    def chat(self, image_data_url: str, prompt: str, model: str) -> tuple[str, str | None]: ...


class _HttpxQwenClient:  # pragma: no cover - real endpoint path
    def __init__(self, endpoint: str, api_key: str | None = None) -> None:
        from openreading.adapters._http import build_httpx_client

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = build_httpx_client(
            base_url=endpoint.rstrip("/"), headers=headers, timeout=300.0
        )

    def chat(self, image_data_url: str, prompt: str, model: str) -> tuple[str, str | None]:
        body = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                        {"type": "text", "text": prompt},
                    ],
                }
            ],
            "max_tokens": 8192,
        }
        r = self._http.post("/chat/completions", json=body)
        if r.status_code == 503:
            raise RetryableError("model loading (cold start)", backend_code="503", retry_after=5.0)
        if r.status_code >= 400:
            from openreading.adapters._http import error_for_status

            raise error_for_status(r.status_code, r.headers, message=r.text)
        choice = r.json()["choices"][0]
        return choice["message"]["content"], choice.get("finish_reason")


def _descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        id="qwen-vl",
        type=BackendType.SELF_HOSTED_MODEL,
        # Ledger T4b (AC-8): INLINE-only, so R1's resume scenario is structurally inapplicable
        # (no non-terminal state to resume) and R2 already holds trivially (no client is ever
        # cached on self) — verified against the real R1/R2 conformance kit, not assumed.
        protocol_version=2,
        adapter_impl="http",
        provisioning=Provisioning(
            byo_mode=["weights", "endpoint"], auth="none", billing_target="caller_infra"
        ),
        wait_modes=[WaitMode.INLINE],
        capabilities=Capabilities(
            ocr="claimed",
            handwriting="claimed",
            printed_tables="claimed",
            complex_tables="claimed",
            forms_key_value="claimed",
            layout="claimed",
            reading_order="claimed",
            multi_column="claimed",
            figures_charts="claimed",
            custom_schema_extraction="claimed",
            vlm_based="verified",
            languages=["en", "zh", "and 30+ (Qwen3-VL)"],
            input_formats=["png", "jpg", "pdf (rasterized)"],
        ),
        cost=Cost(native_unit="gpu_second", basis="infra_only", usd_per_page_equiv_low=0.0),
        compliance=ComplianceProfile(
            hipaa_baa="na_local",
            trains_on_customer_data="na_local",
            runs_fully_local=True,
            data_region_options=["*"],
            max_retention_hours=0,
        ),
        runtime=RuntimeProfile(
            offline_capable=True,
            license="Apache-2.0 (Qwen3-VL; Qwen2.5-VL per-size)",
            system_deps=[
                "self-hosted OpenAI-compatible endpoint (vLLM/Ollama/SGLang)",
                "GPU",
                "pdf rasterizer",
            ],
            version_pin="Qwen3-VL",
            hardware="gpu_mid",
            vram_class="le28",
            serving="vllm",
        ),
        output=Output(
            block_granularity="element",  # v0.3 hint (§4.3): html-mode blocks are layout elements
            paradigms=[OutputParadigm.TOKEN_STREAM],
            channels=OutputChannels(
                # MODE-EXCLUSIVE backend (§3.3, §6.5): a single request runs exactly one prompt
                # mode — html (layout), markdown, or extraction — and each mode produces a
                # DIFFERENT channel set, so the modes destroy each other's channels:
                #   • html:     blocks + block_bbox (native), table_cells/text/markdown (derived)
                #   • markdown: markdown (native), text (derived); NO blocks/cells/typed_fields
                #   • extract:  typed_fields (native); NO markdown/text/blocks/cells
                # A static grade can't express that, so every mode-dependent channel takes its
                # honest floor D. The per-response channel_provenance signal records native vs
                # derived for the ACTIVE mode, and normalize() emits a machine-readable
                # `channel_unavailable_in_mode` warning (C6) for every requested channel the active
                # mode cannot produce — the RouterHints has no structured notes slot (extra=forbid).
                markdown=D,
                text=D,
                blocks=D,
                block_bbox=D,
                block_confidence=X,
                typed_fields=D,
                table_cells=D,
            ),
        ),
        router=RouterHints(
            normalization_difficulty="high",
            integration_priority="P1",
            priority_reason="Local VLM fallback: zero-egress PHI path when specialists are ineligible.",
        ),
        config_spec=[
            ConfigField(
                key="endpoint",
                required=True,
                env=["QWEN_VL_ENDPOINT"],
                example="http://localhost:8000/v1",
                description="OpenAI-compatible endpoint (vLLM/Ollama/SGLang); no data leaves your infra.",
            ),
            ConfigField(
                key="model",
                required=False,
                env=["QWEN_VL_MODEL"],
                example="Qwen/Qwen3-VL-8B-Instruct",
            ),
        ],
        credentials_spec=[
            CredentialField(
                key="api_key",
                required=False,
                env=["QWEN_VL_API_KEY"],
                description="optional Bearer token if your endpoint requires auth.",
            ),
        ],
        accepts_url=False,
        live_gate_env=["QWEN_VL_ENDPOINT"],
        liveness=LivenessProbe(
            probe="endpoint",
            method="GET {QWEN_VL_ENDPOINT}/models",
            timeout_s=5.0,
            notes=(
                "The OpenAI-compatible model list every serving runtime (vLLM/Ollama/SGLang) "
                "exposes: free, non-generating, and served from your own infrastructure. Also "
                "reports which model is actually loaded, which the endpoint URL alone cannot."
            ),
        ),
        sources=[
            Source(
                url="https://github.com/QwenLM/Qwen3-VL",
                accessed="2026-07-21",
                supports="qwenvl html data-bbox output, OpenAI-compatible serving, Apache-2.0 weights",
            )
        ],
    )


class _QwenHTMLParser(HTMLParser):
    """Parse `qwenvl html`: top-level <div class=.. data-bbox=..> blocks. For a table block the
    RAW inner `<table>…</table>` HTML is captured verbatim so it can be handed to the shared
    derive.html_table_to_table (true rowspan/colspan grid, is_header from <th>) — the naive
    tr/td accumulator that discarded spans and fabricated is_header=row-0 is gone (P1)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self._depth = 0
        self._cur: dict | None = None
        self._table_depth = 0
        self._table_parts: list[str] = []

    def _emit_start(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        parts = [tag]
        for k, v in attrs:
            parts.append(k if v is None else f'{k}="{_htmllib.escape(v, quote=True)}"')
        return "<" + " ".join(parts) + ">"

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "div":
            self._depth += 1
            if self._depth == 1:
                a = dict(attrs)
                self._cur = {
                    "class": a.get("class", "text"),
                    "bbox": a.get("data-bbox"),
                    "text_parts": [],
                    "table_html": None,
                }
            return
        if self._cur is None:
            return
        if tag == "table":
            self._table_depth += 1
        if self._table_depth > 0:
            self._table_parts.append(self._emit_start(tag, attrs))

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == "div":
            if self._depth == 1 and self._cur is not None:
                self.blocks.append(self._cur)
                self._cur = None
            self._depth = max(0, self._depth - 1)
            return
        if self._cur is None or self._table_depth == 0:
            return
        self._table_parts.append(f"</{tag}>")
        if tag == "table":
            self._table_depth -= 1
            if self._table_depth == 0:
                self._cur["table_html"] = "".join(self._table_parts)
                self._table_parts = []

    def handle_data(self, data):
        if self._cur is None:
            return
        if self._table_depth > 0:
            self._table_parts.append(_htmllib.escape(data))
        elif data.strip():
            self._cur["text_parts"].append(data)


class QwenVLAdapter(BackendAdapter):
    def __init__(
        self,
        client: QwenVLClient | None = None,
        dpi: int = _DEFAULT_DPI,
        *,
        probe_client: Any | None = None,
    ) -> None:
        self.descriptor = _descriptor()
        self._client = client
        self._dpi = dpi
        # Separate injectable port for the LIVENESS probe (internal/design/liveness.md §3.2) — the
        # execution client speaks `chat`, the probe speaks a plain HTTP GET.
        self._probe_client = probe_client

    def health(self) -> Health:
        if self._client is not None:
            return Health(ready=True, detail="injected client")
        try:
            import httpx  # noqa: F401
        except ImportError:
            return Health(ready=False, missing_deps=["httpx (pip install 'openreading[qwen-vl]')"])
        # Offline by construction: QWEN_VL_ENDPOINT being set proves a URL was typed into .env, not
        # that anything is listening on it. `probe_liveness` below is what can tell the difference.
        return Health(ready=True, detail="requires a self-hosted Qwen-VL endpoint")

    def probe_liveness(self, ctx: RunContext, *, timeout_s: float) -> ProbeResult:
        """The OpenAI-compatible model list. `endpoint` already ends in `/v1` (vLLM/Ollama/SGLang
        all serve it there), so this is the standard `GET /v1/models` — free, non-generating, and
        served by every OpenAI-compatible runtime. It also yields the served model id, which is
        genuinely useful: "responding, serving Qwen/Qwen3-VL-8B-Instruct" answers a different
        question than "responding" alone (is the model you configured the one that is loaded?).

        `endpoint` kind: the server runs in the caller's own infrastructure, so no data leaves it
        and nothing can be billed."""
        endpoint = self._endpoint(ctx)
        if not endpoint:  # pragma: no cover - check_liveness gates on readiness before calling
            return ProbeResult.unreachable("no endpoint configured (QWEN_VL_ENDPOINT)")
        creds = (ctx.credentials.values if ctx.credentials else {}) or {}
        api_key = creds.get("api_key")
        return probe_http(
            f"{endpoint.rstrip('/')}/models",
            timeout_s=timeout_s,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else None,
            env_hint="QWEN_VL_ENDPOINT",
            on_ok=self._served_model,
            client=self._probe_client,
        )

    @staticmethod
    def _served_model(response: Any) -> ProbeResult:
        """Read the first served model id out of an OpenAI-compatible `/models` body. A malformed
        or empty body is still a LIVE answer — something is serving — just without a version to
        report; inventing one, or downgrading to `error`, would both be dishonest."""
        try:
            data = response.json().get("data") or []
            model = data[0].get("id") if data else None
        except Exception:  # noqa: BLE001 - a body we can't parse doesn't unmake the 2xx
            model = None
        detail = "responding (QWEN_VL_ENDPOINT)"
        return ProbeResult.live(detail, version=model)

    def _endpoint(self, ctx: RunContext) -> str | None:
        runtime = ctx.runtime or {}
        creds = (ctx.credentials.values if ctx.credentials else {}) or {}
        return runtime.get("endpoint") or creds.get("endpoint")

    def _get_client(self, ctx: RunContext) -> QwenVLClient:
        if self._client is not None:
            return self._client
        runtime = ctx.runtime or {}
        creds = (ctx.credentials.values if ctx.credentials else {}) or {}
        endpoint = runtime.get("endpoint") or creds.get("endpoint")
        if not endpoint:
            raise TerminalError(
                "Qwen-VL needs runtime.endpoint (OpenAI-compatible URL)", backend_code="no_endpoint"
            )
        return _HttpxQwenClient(endpoint, creds.get("api_key"))  # pragma: no cover

    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        client = self._get_client(ctx)
        runtime = ctx.runtime or {}
        model = (
            runtime.get("model")
            or ((ctx.credentials.values if ctx.credentials else {}) or {}).get("model")
            or "Qwen/Qwen3-VL-8B-Instruct"
        )

        mode = (
            "extract"
            if req.extraction_schema
            else (
                "markdown"
                if (req.outputs and not req.outputs.blocks and req.outputs.markdown)
                else "html"
            )
        )
        prompt = self._prompt_for(mode, req)
        try:
            images = self._render(req)
        except (RetryableError, TerminalError):
            raise
        except Exception as e:  # noqa: BLE001
            raise self._map_error(e, "rasterization failed") from e

        pages = []
        for data_url, w, h, src_page in images:
            try:
                content, finish_reason = client.chat(data_url, prompt, model)
            except (RetryableError, TerminalError):
                raise
            except Exception as e:  # noqa: BLE001
                raise self._map_error(e) from e
            pages.append(
                {
                    "page": src_page,  # SOURCE-document page number, preserved under subsetting (C9)
                    "content": content,
                    "finish_reason": finish_reason,
                    "width_px": w,
                    "height_px": h,
                }
            )

        job = self.new_job(
            WaitMode.INLINE, state=JobState.SUCCEEDED, idempotency_key=ctx.idempotency_key
        )
        job.raw = RawResult(
            payload={"mode": mode, "model": model, "pages": pages},
            media_type="application/json",
            encoding="json",
            object_class="chat.completions",
        )
        return job

    def _map_error(self, e: Exception, context: str | None = None):
        # `context` keeps the two submit() failure sites distinguishable (the rasterizer runs
        # before the endpoint is ever reached) without re-forking the mapping itself.
        if isinstance(e, (TerminalError, RetryableError)):
            return e
        msg = f"{context}: {e}" if context else str(e)
        return TerminalError(msg, backend_code=type(e).__name__)

    def _prompt_for(self, mode: str, req: OpenReadingRequest) -> str:
        if mode == "extract":
            schema = (
                json.dumps(req.extraction_schema.json_schema or {})
                if req.extraction_schema
                else "{}"
            )
            instr = (
                (req.extraction_schema.instructions + " ")
                if (req.extraction_schema and req.extraction_schema.instructions)
                else ""
            )
            return f"{instr}Extract the fields described by this JSON schema and reply with ONLY valid JSON:\n{schema}"
        return _MD_PROMPT if mode == "markdown" else _HTML_PROMPT

    def _render(self, req: OpenReadingRequest) -> list[tuple[str, int, int, int]]:
        """Each tuple is (data_url, width_px, height_px, source_page). `source_page` is the
        1-based SOURCE-document page number — preserved so page subsetting never renumbers to
        1..k (C9)."""
        d = req.document
        data = base64.b64decode(d.bytes_base64) if d.bytes_base64 else None
        if data is None and d.path:
            with open(d.path, "rb") as fh:
                data = fh.read()
        if data is None:
            raise TerminalError(
                "Qwen-VL needs document bytes/path (image or PDF)", backend_code="unsupported_input"
            )
        is_pdf = (d.mime_type or "").lower() == "application/pdf" or data[:5] == b"%PDF-"
        if not is_pdf:
            from io import BytesIO

            from PIL import Image

            img = Image.open(BytesIO(data))
            return [(self._data_url(data, d.mime_type or "image/png"), img.width, img.height, 1)]

        import fitz

        doc = fitz.open(stream=data, filetype="pdf")
        pages = self._selected(doc.page_count, req)
        out = []
        for pno in pages:  # pno is a 0-based page index
            pix = doc[pno].get_pixmap(dpi=self._dpi)
            png = pix.tobytes("png")
            out.append((self._data_url(png, "image/png"), pix.width, pix.height, pno + 1))
        doc.close()
        return out

    def _selected(self, n: int, req: OpenReadingRequest) -> list[int]:
        if req.pages is None:
            return list(range(n))
        idx = []
        for rng in req.pages.ranges or []:
            # Clamp BEFORE building the list: the requested span is caller-controlled and
            # unbounded, the document's page count is not.
            end = min(rng.end or rng.start, n)
            idx += [p - 1 for p in range(rng.start, end + 1)]
        idx = idx or list(range(n))
        return sorted(set(idx[: req.pages.max_pages] if req.pages.max_pages else idx))

    def _data_url(self, data: bytes, mime: str) -> str:
        return f"data:{mime};base64,{base64.b64encode(data).decode()}"

    # ---- normalize -----------------------------------------------------------
    def normalize(
        self, job: Job, ctx: RunContext, slim_req: OpenReadingRequest
    ) -> NormalizedResponse:
        data: dict[str, Any] = job.raw.payload if job.raw else {}
        mode = data.get("mode", "html")
        outputs = slim_req.outputs or Outputs()
        pages, text_parts, md_parts, typed_fields = [], [], [], {}

        for pr in data.get("pages", []):
            content = pr["content"]
            if mode == "extract":
                # P0: the fenced JSON blob is NOT the text channel — typed_fields already carries
                # the extraction. Appending it would put `{...}`/```json into document.text.
                typed_fields.update(self._parse_extract(content))
            elif mode == "markdown":
                # P0/C1: the model emits GFM (native markdown); the text channel is its PLAIN
                # projection via derive.md_to_text — raw markup must never land in `text`.
                ptext = md_to_text(content)
                pages.append(
                    Page(
                        page_number=pr["page"],
                        text=(ptext or None) if outputs.text else None,
                        markdown=(content or None) if outputs.markdown else None,
                    )
                )
                text_parts.append(ptext)
                md_parts.append(content)
            else:  # html
                blocks, ptext, pmd = self._parse_html(
                    content, pr["width_px"], pr["height_px"], pr["page"]
                )
                pages.append(
                    Page(
                        page_number=pr["page"],
                        width=pr["width_px"],
                        height=pr["height_px"],
                        unit=PageUnit.PIXEL,
                        dpi=self._dpi,
                        blocks=blocks,
                        text=ptext if outputs.text else None,
                        markdown=pmd if outputs.markdown else None,
                    )
                )
                text_parts.append(ptext)
                md_parts.append(pmd)

        # C10 status.honest: a `finish_reason == length` truncation is PARTIAL, never a bare
        # SUCCEEDED with silently incomplete content.
        truncated = any(
            (pr.get("finish_reason") or "").lower() == "length" for pr in data.get("pages", [])
        )
        resp = NormalizedResponse(
            status=Status(state=ResponseState.PARTIAL if truncated else ResponseState.SUCCEEDED),
            backend=BackendInfo(
                id="qwen-vl",
                type=BackendType.SELF_HOSTED_MODEL,
                version=data.get("model"),
                output_paradigm=[OutputParadigm.TOKEN_STREAM],
            ),
            document=Document(
                # empty joins collapse to None — an absent channel is None, never "" (a mode that
                # produces no text/markdown must report absence honestly, not an empty string).
                text=("\n\n".join(t for t in text_parts if t) or None) if outputs.text else None,
                markdown=("\n\n".join(m for m in md_parts if m) or None)
                if (outputs.markdown and mode != "extract")
                else None,
                page_count=len(data.get("pages", [])),
                pages=pages or None,
            ),
            typed_fields=typed_fields or None,
        )
        resp.channel_provenance = _PROVENANCE.get(mode)
        if truncated:
            resp.add_warning(
                "output_truncated",
                "Qwen-VL stopped at finish_reason=length (max_tokens); output is TRUNCATED and "
                "the extracted content is incomplete.",
                "status",
            )
        self._warn_unavailable_channels(resp, mode, outputs)
        if data.get("pages") and mode == "html":
            resp.add_warning(
                "bbox_space_approximate",
                "Qwen-VL bboxes are in the model's resized image space; normalized by sent-image dims (UNVERIFIED exactness)",
                "block_bbox",
            )
        if outputs.include_backend_raw:
            resp.backend_raw = BackendRaw(
                encoding="text",
                media_type="text/html",
                object_class="chat.completions.content",
                payload=data,
            )
        return resp

    def _parse_extract(self, content: str) -> dict[str, TypedField]:
        raw = content.strip()
        if raw.startswith("```"):
            raw = raw.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return {k: TypedField(value=v) for k, v in obj.items()} if isinstance(obj, dict) else {}

    def _parse_html(self, content: str, w: int, h: int, page: int):
        parser = _QwenHTMLParser()
        parser.feed(content)
        parser.close()
        blocks, text_parts, md_parts = [], [], []
        for order, b in enumerate(parser.blocks):
            btype = _DIV_CLASS_MAP.get((b["class"] or "text").split()[0], BlockType.TEXT)
            bbox = self._bbox(b.get("bbox"), w, h, page)
            text = " ".join(b["text_parts"]).strip() or None
            table = html_table_to_table(b["table_html"]) if b.get("table_html") else None
            if table is not None:
                # P1/C2: true-span grid → plain projection into text (never left markup-only in
                # markdown) and the GFM pipe rendition (escaped) into markdown.
                text = table_to_text(table) or None
                if text:
                    text_parts.append(text)
                md_parts.append(table_to_pipe_md(table))
            elif text:
                # literal document text embedded in markdown is escaped (C3); a native TITLE div
                # is a real heading signal, so it earns an ATX prefix.
                esc = escape_md(text)
                md_parts.append(f"# {esc}" if btype is BlockType.TITLE else esc)
                text_parts.append(text)
            blocks.append(
                Block(
                    type=btype,
                    native_type=f"div.{b['class']}",
                    text=text,
                    bbox=bbox,
                    reading_order=order,
                    table=table,
                )
            )
        return blocks, "\n".join(text_parts), "\n\n".join(md_parts)

    def _warn_unavailable_channels(self, resp, mode: str, outputs: Outputs) -> None:
        """C6 / mode-exclusivity (P2): the active mode produces only one channel set; any channel
        the caller REQUESTED that this mode cannot produce is named in a machine-readable warning
        (never silently dropped). Modes are mutually exclusive — one prompt per request."""
        produced = _MODE_PRODUCES.get(mode, frozenset())
        for channel, requested in _REQUESTED.items():
            if requested(outputs) and channel not in produced:
                resp.add_warning(
                    "channel_unavailable_in_mode",
                    f"The selected output mode does not populate the {channel} channel; Qwen-VL "
                    "processes one mode (layout / markdown / extraction) per request and their "
                    "channel sets are mutually exclusive.",
                    channel,
                )

    def _bbox(self, raw: str | None, w: int, h: int, page: int):
        if not raw:
            return None
        try:
            x0, y0, x1, y1 = (float(v) for v in raw.split()[:4])
        except (ValueError, IndexError):
            return None
        return to_canonical(
            [x0, y0, x1, y1],
            origin=NativeOrigin.TOP_LEFT,
            unit=NativeUnit.PIXEL,
            page_width=w,
            page_height=h,
            page=page,
            dpi=self._dpi,
        )

    def report_cost(self, job: Job) -> CostReport:
        pages = len((job.raw.payload or {}).get("pages", [])) if job.raw else 0
        return infra_only("gpu_second", float(pages))
