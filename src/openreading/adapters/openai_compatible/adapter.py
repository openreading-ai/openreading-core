"""One adapter for every endpoint that speaks the OpenAI chat-completions API.

You get a backend for OpenAI, OpenRouter, Groq, Ollama, vLLM or LM Studio by writing a provider
profile in `openreading.yaml`, with no adapter code. A provider profile is a named backend whose
endpoint, model and key variable come from that file. `openreading.config` documents the
`providers:` grammar, and `openreading.adapters.registry` builds one instance of this class per
profile.

Flow
----
Both operations are INLINE: `submit()` makes the calls and returns a finished job.

- parse: each selected page becomes one PNG image, rasterized at 150 dpi with `pymupdf`, and one
  `POST {base_url}/chat/completions` call. The call carries an `image_url` content part with a
  base64 data URL and a prompt asking for GitHub-flavored Markdown. An image document is sent as
  it is, as a single page.
- extract: one call carries every selected page image, the caller's instructions, and
  `response_format: {type: json_schema, json_schema: {name, schema}}` when the request has a JSON
  Schema. A request with instructions only sends `{type: json_object}`. The reply's JSON object
  becomes `typed_fields` after local validation against the supplied schema.

The parse request body holds only `model` and `messages`. Extraction also sends `response_format`.
Both omit `max_tokens` and `temperature` because some models reject those parameters.

Channel posture (law PS7: a profile claims only what the protocol carries)
-------------------------------------------------------------------------
A channel is one named part of the response, such as text, blocks or confidence. The protocol
carries generated text and nothing else, so this adapter fills only that.

- parse fills `markdown` (native, the model's own output) and `text` (derived with
  `openreading.derive.md_to_text`). Pages keep their source page numbers.
- extract fills `typed_fields` (native). It fills no markdown or text, and a requested one gets a
  `channel_unavailable_in_mode` warning.
- `blocks`, `block_bbox`, `block_confidence` and `table_cells` are graded X. A chat reply has no
  geometry and no calibrated score, and a fabricated confidence is indistinguishable from a
  measured one downstream. A requested X channel gets a `channel_unavailable` warning.
- `finish_reason: length` marks the response PARTIAL with an `output_truncated` warning.

Extraction keeps any reply that parses as a JSON object, with status SUCCEEDED, even when it
misses the requested schema. The router's shared check (`openreading.router.finalize`) then
reports each mismatch in `schema_check`, the same way for every backend. A reply that is not a
JSON object returns PARTIAL without typed fields and sets `status.error.code` to
`backend_validation`, because there are no fields to return. An explicit model refusal becomes a `TerminalError`
with code `model_refused`. A 401 or 403 maps to `auth_rejected`, and `openreading.readiness`
names the profile's `key_env` in the hint.

Descriptor
----------
`_descriptor(profile)` is a static template that the profile fills in. The `id` is the profile
name. `runtime.version_pin` is the model. With a `key_env`, the descriptor declares one secret
`credentials_spec` field, `api_key`, whose only env name is that variable, and the type is
`hosted_api` with `signup_url` set to the origin of `base_url`. Without one, the endpoint needs no
key (a local Ollama, for example), the type is `self_hosted_model`, and the descriptor declares
no credentials. `config_spec` stays empty, because `base_url` and the model come from the
operator's file and never from the environment or a request. `backend.runtime.endpoint` in a
request is refused by `openreading.credentials` before this adapter runs (BL-162), and this
adapter never reads `ctx.runtime` for a destination.

Each usage counter is reported only when every call supplies it. The cost report uses the
observed total or the sum of both observed counters for each call. Missing totals produce
`cost_unavailable`. Each chat call checks a 300-second budget while reading the reply.
An explicit document budget limits when the next page can start and clips its timeout.
If the budget runs out between pages, the response includes the completed pages.

The liveness probe calls `GET {base_url}/models` with the key when configured.
A readable model list confirms reachability, even when empty or missing the model.
A matching id also confirms the listing for the configured model,
including an untagged Ollama model listed with its default `:latest` tag.

Sources: https://platform.openai.com/docs/api-reference/chat/create and
https://platform.openai.com/docs/api-reference/models/list (accessed 2026-11-20).
"""

from __future__ import annotations

import base64
import json
from contextlib import ExitStack
from dataclasses import dataclass
from time import monotonic
from typing import Any, Protocol
from urllib.parse import urlsplit

from openreading.adapters.base import BackendAdapter
from openreading.derive import md_to_text
from openreading.liveness import probe_http
from openreading.types.blocks import TypedField
from openreading.types.cost import CostReport
from openreading.types.descriptor import (
    AdapterDescriptor,
    Capabilities,
    CredentialField,
    LivenessProbe,
    Output,
    OutputChannels,
    Provisioning,
    RuntimeProfile,
    Source,
)
from openreading.types.enums import (
    BackendType,
    ChannelGrade,
    JobState,
    OutputParadigm,
    ResponseState,
    WaitMode,
)
from openreading.types.errors import RetryableError, TerminalError
from openreading.types.job import Job
from openreading.types.liveness import ProbeResult
from openreading.types.request import OpenReadingRequest, Outputs
from openreading.types.response import (
    BackendInfo,
    BackendRaw,
    Document,
    NormalizedResponse,
    Page,
    ResponseError,
    Status,
    Usage,
)
from openreading.types.runtime import Health, RawResult, RunContext

X = ChannelGrade.IMPOSSIBLE
D = ChannelGrade.DERIVABLE

_DEFAULT_DPI = 150
_PARSE_PROMPT = (
    "Transcribe this document page into GitHub-flavored Markdown. Keep the reading order, "
    "render tables as Markdown tables, and reply with the Markdown only."
)
_EXTRACT_PROMPT = (
    "Extract the requested fields from the document pages and reply with one JSON object."
)
_IMAGE_MIME = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}

# Which channels each mode fills. The two modes never share a channel, so a channel the caller
# requested that the active mode cannot fill is named in a warning instead of dropped.
_MODE_PRODUCES = {
    "parse": frozenset({"markdown", "text"}),
    "extract": frozenset({"typed_fields"}),
}
_PROVENANCE = {
    "parse": {"markdown": "native", "text": "derived"},
    "extract": {"typed_fields": "native"},
}
_REQUESTED = {
    "markdown": lambda o: o.markdown,
    "text": lambda o: o.text,
    "blocks": lambda o: o.blocks,
    "typed_fields": lambda o: o.typed_fields,
    "table_cells": lambda o: o.tables == "cells",
}
_NEVER_PRODUCED = frozenset({"blocks", "table_cells"})


@dataclass(frozen=True)
class ProviderProfile:
    """One validated `providers:` entry. `key_env` is a variable NAME, never a key value."""

    name: str
    base_url: str
    model: str
    key_env: str | None = None


class OpenAICompatibleClient(Protocol):
    """What tests fake: one chat-completions call, returning the decoded JSON body."""

    def chat(self, body: dict[str, Any]) -> dict[str, Any]: ...


class _HttpxChatClient:
    def __init__(self, base_url: str, api_key: str | None, deadline: float | None) -> None:
        from openreading.adapters._http import build_httpx_client

        self._deadline = deadline
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = build_httpx_client(
            base_url=base_url.rstrip("/"), headers=headers, timeout=300.0
        )

    def close(self) -> None:
        self._http.close()

    def chat(self, body: dict[str, Any]) -> dict[str, Any]:
        import httpx

        timeout = _remaining(self._deadline)
        call_deadline = monotonic() + timeout
        try:
            with self._http.stream("POST", "/chat/completions", json=body, timeout=timeout) as r:
                return _read_chat_reply(r, call_deadline)
        except httpx.TimeoutException as exc:
            raise RetryableError("The chat request timed out.", backend_code="timeout") from exc


def _read_chat_reply(response: Any, deadline: float) -> dict[str, Any]:
    """Keep complete JSON at the deadline. HTTP read timeouts restart for every chunk."""
    from openreading.adapters._http import error_for_status

    data = bytearray()
    for chunk in response.iter_bytes():
        data.extend(chunk)
        if monotonic() >= deadline:
            break
    try:
        payload = json.loads(data)
    except ValueError:
        if response.status_code < 400:
            _remaining(deadline)
            raise
        payload = {}
    if response.status_code >= 400:
        # The execution boundary redacts resolved keys from the provider's error message.
        err = payload.get("error") if isinstance(payload, dict) else None
        message = err.get("message") if isinstance(err, dict) else None
        status = f"HTTP {response.status_code}"
        raise error_for_status(
            response.status_code,
            response.headers,
            message=f"{status}: {message}" if message else status,
        )
    return payload


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def _descriptor(profile: ProviderProfile) -> AdapterDescriptor:
    keyed = profile.key_env is not None
    descriptor = AdapterDescriptor(
        id=profile.name,
        # A keyed endpoint is somebody's service, and a keyless one is usually your own server.
        # Neither is verifiable, and nothing routes on `type`, so this only labels the catalog.
        type=BackendType.HOSTED_API if keyed else BackendType.SELF_HOSTED_MODEL,
        # INLINE-only: no non-terminal state exists to resume, and no client is cached on self.
        # Both were run through the R1/R2/R3 kit in tests/test_openai_compatible.py.
        protocol_version=2,
        adapter_impl="http",
        operations=["parse", "extract"],
        # A keyless profile declares no byo_mode: its endpoint comes from openreading.yaml, not
        # from an environment variable the conformance kit could ask it to name.
        provisioning=Provisioning(
            byo_mode=["api_key"] if keyed else [], auth="api_key" if keyed else "none"
        ),
        wait_modes=[WaitMode.INLINE],
        capabilities=Capabilities(
            custom_schema_extraction="claimed",
            page_range_selection=True,
            input_formats=["pdf (rasterized)", "png", "jpeg", "webp", "gif"],
        ),
        runtime=RuntimeProfile(
            version_pin=profile.model,
            serving="remote_endpoint",
            system_deps=["pymupdf, to rasterize PDF pages"],
        ),
        output=Output(
            paradigms=[OutputParadigm.TOKEN_STREAM],
            # Mode-exclusive: parse fills markdown and text, extract fills typed_fields. A static
            # grade cannot say "only in one mode", so each takes its honest floor D.
            channels=OutputChannels(
                markdown=D,
                text=D,
                typed_fields=D,
                blocks=X,
                block_bbox=X,
                block_confidence=X,
                table_cells=X,
            ),
        ),
        credentials_spec=(
            [
                CredentialField(
                    key="api_key",
                    required=True,
                    secret=True,
                    env=[profile.key_env],
                    description=f"API key for the {profile.name} provider profile.",
                )
            ]
            if profile.key_env is not None
            else []
        ),
        signup_url=_origin(profile.base_url) if keyed else None,
        accepts_url=False,
        idempotency_supported=False,
        cancel_supported=False,
        liveness=LivenessProbe(
            probe="vendor" if keyed else "endpoint",
            method="GET {base_url}/models",
            timeout_s=5.0,
            notes="The OpenAI-compatible model list. It never generates a token, so it is free.",
        ),
        sources=[
            Source(
                url="https://platform.openai.com/docs/api-reference/chat/create",
                accessed="2026-11-20",
                supports="image_url content parts, response_format json_schema, finish_reason, "
                "usage token counts",
            ),
            Source(
                url="https://platform.openai.com/docs/api-reference/models/list",
                accessed="2026-11-20",
                supports="GET /models, the non-generating liveness call",
            ),
        ],
    )

    descriptor._profile_base_url = profile.base_url.rstrip("/")
    return descriptor


class OpenAICompatibleAdapter(BackendAdapter):
    """One provider profile as a backend. `client` and `probe_client` are the test seams."""

    def __init__(
        self,
        profile: ProviderProfile,
        client: OpenAICompatibleClient | None = None,
        dpi: int = _DEFAULT_DPI,
        *,
        probe_client: Any | None = None,
    ) -> None:
        self.profile = profile
        self.descriptor = _descriptor(profile)
        self._client = client
        self._dpi = dpi
        self._probe_client = probe_client

    def health(self) -> Health:
        if self._client is not None:
            return Health(ready=True, detail="injected client")
        try:
            import httpx  # noqa: F401
        except ImportError:  # pragma: no cover - httpx ships with every install that tests run
            return Health(ready=False, missing_deps=["httpx (pip install 'openreading[http]')"])
        return Health(ready=True, detail="requires an OpenAI-compatible endpoint")

    def _api_key(self, ctx: RunContext) -> str | None:
        creds = (ctx.credentials.values if ctx.credentials else {}) or {}
        value = creds.get("api_key")
        return str(value) if value else None

    def _get_client(
        self, ctx: RunContext, deadline: float | None, stack: ExitStack
    ) -> OpenAICompatibleClient:
        if self._client is not None:
            return self._client
        api_key = self._api_key(ctx)
        if self.profile.key_env is not None and not api_key:
            raise TerminalError(
                f"{self.profile.name} needs {self.profile.key_env}",
                backend_code="no_credentials",
            )
        client = _HttpxChatClient(self.profile.base_url, api_key, deadline)
        stack.callback(client.close)
        return client

    def probe_liveness(self, ctx: RunContext, *, timeout_s: float) -> ProbeResult:
        """`GET {base_url}/models`. The env hint names the key variable for a keyed profile and
        the profile itself otherwise, never the URL, because a URL can embed credentials."""
        api_key = self._api_key(ctx)
        return probe_http(
            f"{self.profile.base_url.rstrip('/')}/models",
            timeout_s=timeout_s,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else None,
            env_hint=self.profile.key_env or f"providers.{self.profile.name}",
            on_ok=self._check_models,
            client=self._probe_client,
        )

    def _check_models(self, response: Any) -> ProbeResult:
        if not 200 <= response.status_code < 300:
            return ProbeResult.error(f"The model endpoint returned HTTP {response.status_code}.")
        try:
            body = response.json()
        except ValueError:
            return ProbeResult.error("The endpoint returned an unreadable model list.")
        models = body.get("data") if isinstance(body, dict) else body
        if not isinstance(models, list):
            return ProbeResult.error("The endpoint returned an unreadable model list.")
        model = self.profile.model
        for entry in models:
            model_id = entry.get("id") if isinstance(entry, dict) else entry
            if model_id == model or (
                ":" not in model.rsplit("/", 1)[-1] and model_id == f"{model}:latest"
            ):
                return ProbeResult.live("configured model listed", version=model_id)
        return ProbeResult.live("The configured model was not listed.")

    # ---- submit --------------------------------------------------------------------------
    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:
        deadline = None
        if ctx.deadline_ms is not None and not ctx.deadline_is_default:
            deadline = monotonic() + ctx.deadline_ms / 1000
        _remaining(deadline)
        with ExitStack() as stack:
            client = self._get_client(ctx, deadline, stack)
            mode = (
                "extract"
                if (req.extraction_schema or req.backend.operation == "extract")
                else "parse"
            )
            try:
                images = self._render(req, deadline)
            except (RetryableError, TerminalError):
                raise
            except Exception as e:  # noqa: BLE001
                raise self._map_error(e, "rasterization failed") from e

            calls: list[tuple[int | None, dict[str, Any]]]
            if mode == "extract":
                calls = [(None, self._extract_body(req, [url for url, _ in images]))]
            else:
                calls = [(page, self._parse_body(url)) for url, page in images]

            pages: list[dict[str, Any]] = []
            totals: dict[str, int] | None = None
            for page, body in calls:
                try:
                    _remaining(deadline)
                    reply = client.chat(body)
                except RetryableError as exc:
                    if (
                        exc.backend_code == "timeout"
                        and pages
                        and deadline is not None
                        and monotonic() >= deadline
                    ):
                        break
                    raise
                except TerminalError:
                    raise
                except Exception as e:  # noqa: BLE001
                    raise self._map_error(e) from e
                content, finish_reason = self._choice(reply)
                pages.append({"page": page, "content": content, "finish_reason": finish_reason})
                counts = _token_counts(reply.get("usage"))
                if totals is None:
                    totals = counts
                else:
                    totals = {
                        key: totals[key] + counts[key] for key in totals.keys() & counts.keys()
                    }

        job = self.new_job(
            WaitMode.INLINE, state=JobState.SUCCEEDED, idempotency_key=ctx.idempotency_key
        )
        job.raw = RawResult(
            payload={
                "mode": mode,
                "model": self.profile.model,
                "page_count": len(images),
                "pages": pages,
                "usage": totals or {},
            },
            media_type="application/json",
            encoding="json",
            object_class="chat.completion",
        )
        return job

    def _choice(self, reply: dict[str, Any]) -> tuple[str, str | None]:
        """The first choice's text and finish reason. A reply with no choices, or one whose
        message is a refusal, fails this call, so the chain moves to the next backend."""
        choices = reply.get("choices") if isinstance(reply, dict) else None
        if not choices or not isinstance(choices[0], dict):
            raise TerminalError(
                f"{self.profile.name} returned no choices", backend_code="malformed_response"
            )
        message = choices[0].get("message") or {}
        if message.get("refusal"):
            raise TerminalError(
                f"{self.profile.name} refused the request", backend_code="model_refused"
            )
        content = message.get("content")
        if not isinstance(content, str):
            raise TerminalError(
                f"{self.profile.name} returned no text content", backend_code="malformed_response"
            )
        return content, choices[0].get("finish_reason")

    def _parse_body(self, image_url: str) -> dict[str, Any]:
        return {
            "model": self.profile.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _PARSE_PROMPT},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
        }

    def _extract_body(self, req: OpenReadingRequest, image_urls: list[str]) -> dict[str, Any]:
        es = req.extraction_schema
        instructions = (es.instructions if es else None) or ""
        schema = es.json_schema if es else None
        prompt = f"{instructions}\n\n{_EXTRACT_PROMPT}".strip()
        response_format: dict[str, Any] = (
            {"type": "json_schema", "json_schema": {"name": "extraction", "schema": schema}}
            if schema
            else {"type": "json_object"}
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        content += [{"type": "image_url", "image_url": {"url": u}} for u in image_urls]
        return {
            "model": self.profile.model,
            "messages": [{"role": "user", "content": content}],
            "response_format": response_format,
        }

    def _map_error(self, e: Exception, context: str | None = None) -> Exception:
        if isinstance(e, (TerminalError, RetryableError)):
            return e
        msg = f"{context}: {e}" if context else str(e)
        return TerminalError(msg, backend_code=type(e).__name__)

    def _render(self, req: OpenReadingRequest, deadline: float | None) -> list[tuple[str, int]]:
        """Each tuple is (data_url, source_page). `source_page` is the 1-based page number in
        the source document, kept under page subsetting so pages are never renumbered."""
        d = req.document
        data = base64.b64decode(d.bytes_base64) if d.bytes_base64 else None
        if data is None and d.path:
            with open(d.path, "rb") as fh:
                data = fh.read()
        if data is None:
            raise TerminalError(
                f"{self.profile.name} needs document bytes or a path",
                backend_code="unsupported_input",
            )
        mime = (d.mime_type or "").lower()
        if mime == "application/pdf" or data[:5] == b"%PDF-":
            return self._rasterize(data, req, deadline)
        if mime in _IMAGE_MIME:
            _selected(1, req)
            return [(_data_url(data, "image/jpeg" if mime == "image/jpg" else mime), 1)]
        raise TerminalError(
            f"{self.profile.name} reads PDF and image documents, not {mime or 'this input'}",
            backend_code="unsupported_input",
        )

    def _rasterize(
        self, data: bytes, req: OpenReadingRequest, deadline: float | None
    ) -> list[tuple[str, int]]:
        try:
            import pymupdf
        except ImportError as e:  # pragma: no cover - the dev group installs pymupdf
            raise TerminalError(
                "rasterizing a PDF needs pymupdf (pip install 'openreading[pymupdf]')",
                backend_code="missing_dependency",
            ) from e
        doc = pymupdf.open(stream=data, filetype="pdf")
        try:
            out = []
            for index in _selected(doc.page_count, req):
                _remaining(deadline)
                png = doc[index].get_pixmap(dpi=self._dpi).tobytes("png")
                out.append((_data_url(png, "image/png"), index + 1))
            return out
        finally:
            doc.close()

    # ---- normalize -----------------------------------------------------------------------
    def normalize(
        self, job: Job, ctx: RunContext, slim_req: OpenReadingRequest
    ) -> NormalizedResponse:
        data: dict[str, Any] = job.raw.payload if job.raw else {}
        mode = data.get("mode", "parse")
        outputs = slim_req.outputs or Outputs()
        calls = data.get("pages", [])
        truncated = any((c.get("finish_reason") or "").lower() == "length" for c in calls)
        missed_pages = mode == "parse" and len(calls) < data.get("page_count", 0)
        status = Status(
            state=ResponseState.PARTIAL if truncated or missed_pages else ResponseState.SUCCEEDED
        )
        typed_fields: dict[str, TypedField] = {}
        pages: list[Page] = []
        markdown_parts: list[str] = []
        text_parts: list[str] = []

        if mode == "extract":
            content = calls[0]["content"] if calls else ""
            parsed = _parse_json_object(content)
            # A JSON object is kept even when it misses the schema: the router's shared check
            # (openreading.router.finalize) reports the mismatch in `schema_check`. Only a reply
            # with no object at all is PARTIAL, because there are no fields to return.
            if parsed is None:
                status = Status(
                    state=ResponseState.PARTIAL,
                    error=ResponseError(
                        code="backend_validation",
                        message="The reply must be a JSON object.",
                    ),
                )
            else:
                typed_fields = {k: TypedField(value=v) for k, v in parsed.items()}
        else:
            for c in calls:
                md = c["content"]
                text = md_to_text(md)
                markdown_parts.append(md)
                text_parts.append(text)
                pages.append(
                    Page(
                        page_number=c["page"],
                        markdown=(md or None) if outputs.markdown else None,
                        text=(text or None) if outputs.text else None,
                    )
                )

        u = data.get("usage") or {}
        resp = NormalizedResponse(
            status=status,
            backend=BackendInfo(
                id=self.descriptor.id,
                type=self.descriptor.type,
                version=data.get("model"),
                operation=mode,
                output_paradigm=[OutputParadigm.TOKEN_STREAM],
            ),
            document=Document(
                markdown=("\n\n".join(m for m in markdown_parts if m) or None)
                if outputs.markdown
                else None,
                text=("\n\n".join(t for t in text_parts if t) or None) if outputs.text else None,
                page_count=data.get("page_count"),
                pages=pages or None,
            ),
            typed_fields=typed_fields or None,
            usage=Usage(
                input_tokens=u.get("prompt_tokens"), output_tokens=u.get("completion_tokens")
            ),
        )
        resp.channel_provenance = _PROVENANCE.get(mode)
        if missed_pages:
            resp.add_warning(
                "output_truncated",
                f"The document deadline expired after {len(calls)} of {data['page_count']} selected pages.",
                "status",
            )
        if truncated:
            resp.add_warning(
                "output_truncated",
                f"{self.descriptor.id} stopped at finish_reason=length, so the output is "
                "incomplete.",
                "status",
            )
        self._warn_unavailable(resp, mode, outputs)
        if outputs.include_backend_raw:
            resp.backend_raw = BackendRaw(
                encoding="json",
                media_type="application/json",
                object_class="chat.completion",
                payload=data,
            )
        return resp

    def _warn_unavailable(self, resp: NormalizedResponse, mode: str, outputs: Outputs) -> None:
        """Name every requested channel this response leaves empty, never drop one silently."""
        produced = _MODE_PRODUCES.get(mode, frozenset())
        for channel, requested in _REQUESTED.items():
            if not requested(outputs) or channel in produced:
                continue
            if channel in _NEVER_PRODUCED:
                resp.add_warning(
                    "channel_unavailable",
                    f"An OpenAI-compatible chat reply carries no {channel} channel, so "
                    f"{self.descriptor.id} never fills it.",
                    channel,
                )
            else:
                resp.add_warning(
                    "channel_unavailable_in_mode",
                    f"The {mode} operation does not fill the {channel} channel. Parse fills "
                    "markdown and text, and extract fills typed_fields.",
                    channel,
                )

    def report_cost(self, job: Job) -> CostReport:
        u = ((job.raw.payload or {}).get("usage") or {}) if job.raw else {}
        total = _token_counts(u).get("total_tokens")
        if total is None:
            raise ValueError("The provider omitted token usage for one or more calls.")
        return CostReport(native_unit="token", native_quantity=float(total))


def _remaining(deadline: float | None) -> float:
    if deadline is None:
        return 300.0
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise RetryableError("The request deadline expired.", backend_code="timeout")
    return min(300.0, remaining)


def _token_counts(usage: Any) -> dict[str, int]:
    if not isinstance(usage, dict):
        return {}
    counts = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(key)
        if type(value) is int and value >= 0:
            counts[key] = value
    if "prompt_tokens" in counts and "completion_tokens" in counts:
        counts.setdefault("total_tokens", counts["prompt_tokens"] + counts["completion_tokens"])
    return counts


def _data_url(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


def _selected(n: int, req: OpenReadingRequest) -> list[int]:
    """0-based page indexes the request selects, clamped to the document's page count.

    A selection that misses every page raises instead of widening to the whole document. The
    widened set would upload pages the caller never chose to a hosted endpoint."""
    if req.pages is None:
        return list(range(n))
    idx: list[int] = []
    for rng in req.pages.ranges or []:
        end = min(rng.end or rng.start, n)
        idx += [p - 1 for p in range(rng.start, end + 1)]
    if req.pages.ranges and not idx:
        raise TerminalError(
            f"the page selection matches no page, and the document has {n} "
            f"page{'' if n == 1 else 's'}",
            backend_code="unsupported_input",
        )
    idx = idx or list(range(n))
    return sorted(set(idx[: req.pages.max_pages] if req.pages.max_pages else idx))


def _parse_json_object(content: str) -> dict[str, Any] | None:
    """The reply as a JSON object, tolerating a Markdown code fence around it. None when the
    model answered with something else, which normalize reports as PARTIAL."""
    raw = content.strip()
    if raw.startswith("```"):
        raw = raw.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None
