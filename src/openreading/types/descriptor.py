"""AdapterDescriptor: the static, machine-readable record each adapter ships.

The router reads it to check eligibility, rank candidates, and
build the capability matrix — which is what keeps the router from ever branching on backend
type. Mirrors the file `openreading.schemas.DESCRIPTOR_SCHEMA_FILE` names, currently
`src/openreading/schemas/adapter-descriptor.v0.7.json`. Bump the constant and this line
together.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from openreading.types.enums import BackendType, ChannelGrade, OutputParadigm, WaitMode

# A capability value is 'verified' (live-run/benchmark), 'claimed' (vendor doc only), or False.
CapabilityValue = Literal["verified", "claimed"] | bool


class Provisioning(BaseModel):
    model_config = ConfigDict(extra="forbid")

    byo_mode: list[
        Literal["api_key", "cloud_credential", "pip", "container", "weights", "endpoint"]
    ] = Field(default_factory=list)
    auth: Literal["none", "api_key", "sigv4", "oauth2", "entra", "gcp_adc"] = "none"
    # 'openreading' (resale) is intentionally never emitted; kept in the enum for schema parity.
    billing_target: Literal["caller_account", "openreading", "caller_infra"] = "caller_account"


class CredentialField(BaseModel):
    """One secret/credential an adapter reads from `ctx.credentials.values` (v0.2). The
    EnvCredentialBroker resolves `key` from the first present env var in `env` (precedence order,
    including legacy aliases). `secret=True` values must never be logged, echoed, or committed."""

    model_config = ConfigDict(extra="forbid")

    key: str
    required: bool = True
    secret: bool = True
    env: list[str] = Field(default_factory=list)
    description: str | None = None
    example: str | None = None


class ConfigField(BaseModel):
    """One non-secret config value an adapter reads from `ctx.runtime` (v0.2) — an endpoint URL,
    a model name, a processor id, a region. Resolved from `env` (precedence order) with
    `request.backend.runtime` overriding."""

    model_config = ConfigDict(extra="forbid")

    key: str
    required: bool = False
    env: list[str] = Field(default_factory=list)
    description: str | None = None
    example: str | None = None


class Capabilities(BaseModel):
    model_config = ConfigDict(extra="allow")

    ocr: CapabilityValue = False
    handwriting: CapabilityValue = False
    printed_tables: CapabilityValue = False
    complex_tables: CapabilityValue = False
    forms_key_value: CapabilityValue = False
    layout: CapabilityValue = False
    reading_order: CapabilityValue = False
    multi_column: CapabilityValue = False
    figures_charts: CapabilityValue = False
    signatures: CapabilityValue = False
    classification: CapabilityValue = False
    splitting: CapabilityValue = False
    custom_schema_extraction: CapabilityValue = False
    vlm_based: CapabilityValue = False
    human_in_the_loop: CapabilityValue = False
    languages: list[str] = Field(default_factory=list)
    input_formats: list[str] = Field(default_factory=list)
    # §2.7: the backend can parse a NAMED SUBSET of pages, so a page-granularity cascade can
    # re-run only the pages that failed a gate. A ceiling like `max_pages` is not selection and
    # does not qualify. Declared rather than left to `extra="allow"`: `_supports_page_ranges`
    # reads it through `getattr`, so while it was undeclared it returned False for every backend
    # and the whole per-page path was unreachable outside the fakes in `tests/fakes.py`.
    page_range_selection: bool = False
    max_pages_per_request: int | str | None = None
    max_file_size: str | None = None


class OutputChannels(BaseModel):
    """Per-channel N/D/X grade (internal/research/openreading/normalized_schema.md §4). The
    router never asks a backend for a channel graded X, and warns (never fabricates) when a
    requested channel is X."""

    model_config = ConfigDict(extra="forbid")

    markdown: ChannelGrade = ChannelGrade.IMPOSSIBLE
    text: ChannelGrade = ChannelGrade.IMPOSSIBLE
    blocks: ChannelGrade = ChannelGrade.IMPOSSIBLE
    block_bbox: ChannelGrade = ChannelGrade.IMPOSSIBLE
    block_confidence: ChannelGrade = ChannelGrade.IMPOSSIBLE
    typed_fields: ChannelGrade = ChannelGrade.IMPOSSIBLE
    table_cells: ChannelGrade = ChannelGrade.IMPOSSIBLE


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paradigms: list[OutputParadigm] = Field(default_factory=list)
    channels: OutputChannels = Field(default_factory=OutputChannels)
    # v0.3 — native block granularity hint (§4.3); lets consumers/compare reason about packaging.
    block_granularity: Literal["word", "line", "paragraph", "section", "element"] | None = None


class Cost(BaseModel):
    model_config = ConfigDict(extra="forbid")

    native_unit: Literal[
        "page", "credit", "token", "doc", "gpu_second", "cpu_second", "subscription"
    ] = "page"
    usd_per_page_equiv_low: float | None = None
    usd_per_page_equiv_high: float | None = None
    basis: Literal["billed", "estimated", "infra_only", "unknown"] = "unknown"
    lossiness: Literal["none", "page-def", "per-doc", "credit", "subscription"] = "none"


class RuntimeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offline_capable: bool = False
    license: str | None = None
    system_deps: list[str] = Field(default_factory=list)
    version_pin: str | None = None
    hardware: Literal["cpu", "gpu_small", "gpu_mid", "gpu_large", "managed"] | None = None
    vram_class: Literal["cpu", "le8", "le28", "ge40", "na"] | None = None
    cold_start_s: float | None = None
    serving: Literal["transformers", "vllm", "ollama", "sglang", "remote_endpoint", "na"] | None = (
        None
    )
    sandbox: Literal["in_process", "subprocess", "container"] | None = None


class RouterHints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    normalization_difficulty: Literal["low", "medium", "high"] | None = None


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    accessed: str | None = None
    supports: str | None = None


class BatchIntake(BaseModel):
    """v0.4 — native batch declaration (Manifest v0.6). Absent (or native=False) ⇒ the platform
    batches this backend by fanning out single-document runs. native truthy ⇒ the adapter accepts a
    whole item list via submit_many/normalize_many. Grades follow the honesty ladder: `verified`
    (proven live), `claimed` (documented, unaudited)."""

    model_config = ConfigDict(extra="forbid")

    native: Literal["verified", "claimed", False] = False
    max_items: int | None = None  # provider's per-batch cap
    max_concurrency: int | None = None  # platform-batching cap when native is False (locals)
    notes: str | None = None  # transport constraints (GCS/blob staging, completion windows)


class LivenessProbe(BaseModel):
    """v0.5 — liveness-probe declaration (internal/design/liveness.md §3.3). Absent (or
    `probe="none"`) ⇒ this adapter implements no probe and the platform INFERS a status from
    configuration instead. Present with `probe != "none"` ⇒ the adapter implements the optional
    `LivenessProbeAdapter` method and it may be called.

    Statically readable with no call — which is the point: a UI must be able to say "cannot be
    tested", or "checking this leaves your network", before probing anything.

    `probe` is a KIND, not a boolean, because the kind answers the question an operator actually
    has before pressing a button: `local` is in-process and free; `endpoint` reaches infrastructure
    they operate themselves; `vendor` leaves their network and may count against a rate limit.
    What it never means is "billed" — a probe is never a billed request, and a vendor with no free
    liveness call declares `none` and takes the inferred status instead (§4)."""

    model_config = ConfigDict(extra="forbid")

    probe: Literal["none", "local", "endpoint", "vendor"] = "none"
    method: str | None = None  # "GET /health", "GET /v1/models", "import fitz"
    timeout_s: float | None = None  # the adapter's own recommended bound; the caller still caps it
    notes: str | None = None


class AdapterDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: BackendType
    provisioning: Provisioning
    wait_modes: list[WaitMode]
    capabilities: Capabilities
    cost: Cost
    runtime: RuntimeProfile
    operations: list[str] = Field(default_factory=list)
    adapter_impl: Literal["http", "in_process", "subprocess", "container"] | None = None
    output: Output = Field(default_factory=Output)
    router: RouterHints | None = None
    sources: list[Source] = Field(default_factory=list)
    # v0.4 — native batch declaration (Manifest v0.6). None ⇒ platform batching.
    batch: BatchIntake | None = None
    # v0.5 — liveness-probe declaration (Pulse). None ⇒ no probe; the platform infers a status
    # from configuration instead. Never read by the router: liveness is a diagnostic, never
    # routing input, so it cannot widen the compliance-eligible set (internal/design/liveness.md §7).
    liveness: LivenessProbe | None = None
    # v0.2 — BYO-credential declaration (drives the env broker + readiness UI; the router never
    # branches on backend type, and the broker never hard-codes per-adapter keys).
    credentials_spec: list[CredentialField] = Field(default_factory=list)
    config_spec: list[ConfigField] = Field(default_factory=list)
    signup_url: str | None = None
    # native document-by-URL intake (the CLI/run() pass the URL through instead of downloading).
    accepts_url: bool = False
    # env vars that gate the @pytest.mark.live test (explicit for ambient-chain backends whose
    # spec keys are all optional, e.g. textract's boto3 chain); defaults to required spec envs.
    live_gate_env: list[str] = Field(default_factory=list)
    # v0.6 (BL-166) — true iff submit() forwards a caller/OpenReading idempotency key to the
    # vendor in a form it actually honors. false is an honest declaration, never an invented,
    # vendor-ignored token.
    idempotency_supported: bool = True
    # v0.6 (BL-164) — true iff cancel() actually stops the job at the vendor, not merely locally.
    cancel_supported: bool = True
    # v0.7 (Ledger T4a, AC-8): the adapter-contract version this adapter implements. No default,
    # so every adapter declares one explicitly. A default would let a new adapter skip the
    # declaration and still register, which is the failure this field exists to stop.
    # `1` is the pre-Ledger-T4a shape (poll/resolve_webhook/cancel take no
    # `ctx`, an adapter MAY cache a client on `self`); `2` is T4a's shape (poll/resolve_webhook/
    # cancel take `ctx: RunContext`, and no built-in client is ever cached on `self` — R1/R2 in
    # testing/conformance.py, see internal/design/ledger.md §11-§13). `adapters/registry.py` refuses to
    # register a descriptor declaring a version below the router's own current floor.
    protocol_version: int

    def to_schema_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)
