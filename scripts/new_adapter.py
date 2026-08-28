#!/usr/bin/env python3
"""Adapter scaffold generator (BL-161; internal/product/specs/adapter-scaffold.product-spec.md).

Repository-development tool, NOT a verb on the shipped `openreading` CLI — scaffolding a new
Python source package into this repo's own tree is a contributor action, run with:

    uv run python scripts/new_adapter.py <slug> --template <existing-slug> --type <type> [--force]

Given a slug and the nearest-shape template adapter, generates every file
the `openreading.adapters` runbook (src/openreading/adapters/__init__.py) section 2's "Files to CREATE" table names and edits
every file its "Files to EDIT" table names, in one run, so the seven edits can never partially
land. Every capability/channel/cost/compliance field it writes takes its safe, unverified, or
false default — NEVER a value copied from `--template`'s own researched descriptor (the generator
reads the template for structural shape only: its BackendType, wait_modes, and adapter_impl, which
are architectural facts about the code shape, not researched claims about a vendor).

Every unfinished placeholder — a field still needing primary-source research, a test still needing
a real fixture — carries the literal marker `TODO-SCAFFOLD`, grep-able and checked by
`tests/test_scaffold_sentinel.py`, which fails `make verify` until every marker is gone.

Makes NO network call, ever (matches commit 133a3c0's hard ban on live/network calls in this
repo's own build/discovery tooling) — every value below is either a safe static default or a
mechanical string transform of the slug you passed on the command line, seeded as a starting guess
and marked TODO-SCAFFOLD, never asserted as fact. Writes no wire-format logic, no fixture content,
and no descriptor value that would require primary-source research to assert honestly.

`--type` is a closed set of the seven shapes named in the openreading.adapters runbook §0's picker table
(collapsing the two self-hosted rows — an OpenAI-compatible endpoint and a generic container — into
one `self_hosted_endpoint` type, since both need the same scaffold shape). A `--template`/`--type`
pair outside that set is declined, never improvised — you're pointed back at a fully manual
the openreading.adapters runbook §1 pass. The native multi-document-batch shape (`anthropic_claude`'s row) is
deliberately not offered as a template: generating a `submit_many`/`normalize_many` stub is
explicitly out of scope (the openreading.adapters runbook §3 already scopes native batch as an opt-in addendum
most adapters skip), so every generated adapter leaves a one-line comment pointing at it instead.
"""

from __future__ import annotations

import argparse
import contextlib
import re
import subprocess
import sys
from pathlib import Path

MARKER = "TODO-SCAFFOLD"
REPO_ROOT = Path(__file__).resolve().parent.parent
ADAPTERS_DIR = REPO_ROOT / "src" / "openreading" / "adapters"
TESTS_DIR = REPO_ROOT / "tests"

# --type -> the --template slugs the openreading.adapters runbook §0's picker table pairs with that shape.
# anthropic-claude (native multi-document batch) is deliberately absent from every bucket — see
# module docstring. Slugs are the hyphenated registry.BUILTIN_ADAPTERS keys.
TYPE_TEMPLATES: dict[str, set[str]] = {
    "hosted_api": {"chunkr", "nuextract"},
    "hosted_sync_async": {"pulse"},
    "hosted_webhook": {"reducto"},
    "hosted_aggregator": {"open-ocr"},
    "self_hosted_endpoint": {"qwen-vl", "docling"},
    "cloud_sdk": {"aws-textract", "google-document-ai", "azure-document-intelligence"},
    "in_process": {"pymupdf", "tesseract"},
}
ALL_TEMPLATE_SLUGS = {slug for slugs in TYPE_TEMPLATES.values() for slug in slugs}

_SLUG_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_TYPE_FIELD_RE = re.compile(r"\btype\s*=\s*BackendType\.([A-Z_]+)\s*,")
_ADAPTER_IMPL_RE = re.compile(r'\badapter_impl\s*=\s*"([a-z_]+)"\s*,')
_WAIT_MODES_RE = re.compile(r"\bwait_modes\s*=\s*\[([^\]]*)\]")


class GeneratorError(Exception):
    """A declined request — bad input, an unsupported shape, or an already-generated slug
    without --force. Always caught in main() and reported as a clean, non-traceback message."""


# --------------------------------------------------------------------------------------------
# naming
# --------------------------------------------------------------------------------------------


def pkg_name(slug: str) -> str:
    return slug.replace("-", "_")


def class_prefix(slug: str) -> str:
    return "".join(word.capitalize() for word in slug.replace("_", "-").split("-"))


def env_prefix(slug: str) -> str:
    return slug.upper().replace("-", "_")


# --------------------------------------------------------------------------------------------
# template introspection — structural shape ONLY (BackendType / wait_modes / adapter_impl are
# architectural facts about the code, not researched claims about a vendor); see module docstring.
# --------------------------------------------------------------------------------------------


class TemplateShape:
    def __init__(self, backend_type: str, adapter_impl: str, wait_modes: list[str]) -> None:
        self.backend_type = backend_type  # e.g. "HOSTED_API"
        self.adapter_impl = adapter_impl  # e.g. "http"
        self.wait_modes = wait_modes  # e.g. ["POLL", "WEBHOOK"]
        if adapter_impl in ("in_process", "subprocess"):
            self.category = "local"
        elif backend_type == "SELF_HOSTED_MODEL" or adapter_impl == "container":
            self.category = "self_hosted"
        else:
            self.category = "hosted"


def read_template_shape(template_slug: str) -> TemplateShape:
    path = ADAPTERS_DIR / pkg_name(template_slug) / "adapter.py"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise GeneratorError(f"cannot read template adapter {path}: {e}") from e
    type_m = _TYPE_FIELD_RE.search(text)
    impl_m = _ADAPTER_IMPL_RE.search(text)
    wm_m = _WAIT_MODES_RE.search(text)
    if not (type_m and impl_m and wm_m):
        raise GeneratorError(
            f"could not read a structural shape (type=/adapter_impl=/wait_modes=) out of "
            f"{path} — pick a different --template or finish this adapter by hand per "
            f"the openreading.adapters runbook §1"
        )
    wait_modes = re.findall(r"WaitMode\.([A-Z]+)", wm_m.group(1))
    return TemplateShape(type_m.group(1), impl_m.group(1), wait_modes)


# --------------------------------------------------------------------------------------------
# CREATE: src/openreading/adapters/<pkg>/__init__.py
# --------------------------------------------------------------------------------------------


def render_init_py(slug: str, shape: TemplateShape) -> str:
    cls = class_prefix(slug)
    pkg = pkg_name(slug)
    has_client = shape.category != "local"
    body = [
        f'"""{cls} adapter (optional extra `{slug}`) — scaffolded by `scripts/new_adapter.py`.',
        f"Resolve every {MARKER} marker in adapter.py against the vendor's primary docs before",
        'this is a real adapter — see the openreading.adapters docstring (src/openreading/adapters/__init__.py)."""',
        "",
        "from __future__ import annotations",
        "",
    ]
    if has_client:
        body.append(f"from openreading.adapters.{pkg}.adapter import {cls}Adapter, {cls}Client")
        body.append("")
        body.append(f'__all__ = ["{cls}Adapter", "{cls}Client"]')
    else:
        body.append(f"from openreading.adapters.{pkg}.adapter import {cls}Adapter")
        body.append("")
        body.append(f'__all__ = ["{cls}Adapter"]')
    body.append("")
    return "\n".join(body)


# --------------------------------------------------------------------------------------------
# CREATE: src/openreading/adapters/<pkg>/adapter.py
# --------------------------------------------------------------------------------------------


def render_adapter_py(slug: str, template_slug: str, shape: TemplateShape) -> str:
    cls = class_prefix(slug)
    env = env_prefix(slug)
    has_client = shape.category != "local"
    has_poll = "POLL" in shape.wait_modes
    has_webhook = "WEBHOOK" in shape.wait_modes
    cred_required = shape.category == "hosted"

    lines: list[str] = []
    a = lines.append

    a(
        f'"""{cls} adapter — SCAFFOLDED by `scripts/new_adapter.py --template {template_slug} '
        f"--type <type>`; not yet a real adapter."
    )
    a("")
    a(f"Every {MARKER} marker below is a placeholder seeded by a mechanical guess (never a")
    a("researched claim) and must be resolved from the vendor's own primary docs before this")
    a(
        "ships — see the openreading.adapters docstring (src/openreading/adapters/__init__.py) §1/§3. Record here once known: what"
    )
    a("the backend is + which ops; the exact flow (endpoints, poll target, status vocabulary);")
    a("channel posture; pricing/usage mapping; credential/env conventions; compliance fail-closed")
    a(
        f"notes; and `Sources: <urls> (accessed YYYY-MM-DD)`. {MARKER}: none of the above is filled in"
    )
    a("yet.")
    a('"""')
    a("")
    a("from __future__ import annotations")
    a("")
    if has_client:
        a("from typing import Protocol")
        a("")
    a("from openreading.adapters.base import BackendAdapter")
    a("from openreading.types.cost import CostReport")
    imp = [
        "AdapterDescriptor",
        "Capabilities",
        "ComplianceProfile",
        "Cost",
        "Provisioning",
        "RuntimeProfile",
    ]
    if shape.category == "self_hosted":
        imp += ["ConfigField", "CredentialField"]
    elif shape.category == "hosted":
        imp.append("CredentialField")
    imp = sorted(set(imp))
    a("from openreading.types.descriptor import (")
    for name in imp:
        a(f"    {name},")
    a(")")
    a("from openreading.types.enums import BackendType, WaitMode")
    a("from openreading.types.errors import RetryableError, TerminalError")
    a("from openreading.types.job import Job")
    a("from openreading.types.request import OpenReadingRequest")
    a("from openreading.types.response import NormalizedResponse")
    a("from openreading.types.runtime import Health, RunContext")
    a("")
    if has_client:
        a(
            f'_DEFAULT_ENDPOINT = "https://{MARKER}.example"  # {MARKER}: the vendor\'s real base URL'
        )
        a("")
    a("")
    if has_client:
        a(f"class {cls}Client(Protocol):")
        a(f"    # {MARKER}: rename these to the real endpoint verbs once the wire shape is known.")
        a("    def submit_job(self, body: dict) -> dict: ...")
        if has_poll:
            a("    def get_status(self, job_id: str) -> dict: ...")
        a("")
        a("")
        a(f"class _Httpx{cls}Client:  # pragma: no cover - real network path")
        a("    def __init__(self, api_key: str | None, base_url: str) -> None:")
        a("        from openreading.adapters._http import build_httpx_client")
        a("")
        a(f"        # {MARKER}: confirm the real auth header shape against the vendor's docs.")
        a("        self._http = build_httpx_client(")
        a("            base_url=base_url.rstrip('/'),")
        a('            headers={"Authorization": api_key} if api_key else {},')
        a("            timeout=120.0,")
        a("        )")
        a("")
        a(f"    def submit_job(self, body: dict) -> dict:  # {MARKER}: wire the real endpoint path")
        a("        from openreading.adapters._http import error_for_status")
        a("")
        a(f'        r = self._http.post("/{MARKER}", json=body)')
        a("        if r.status_code >= 400:")
        a("            raise error_for_status(r.status_code, r.headers, message=r.text)")
        a("        return r.json()")
        if has_poll:
            a("")
            a("    def get_status(self, job_id: str) -> dict:")
            a("        from openreading.adapters._http import error_for_status")
            a("")
            a(f'        r = self._http.get(f"/{MARKER}/{{job_id}}")')
            a("        if r.status_code >= 400:")
            a("            raise error_for_status(r.status_code, r.headers, message=r.text)")
            a("        return r.json()")
        a("")
        a("")

    # ---- _descriptor() ----
    a("def _descriptor() -> AdapterDescriptor:")
    a("    return AdapterDescriptor(")
    a(f'        id="{slug}",')
    a(f"        type=BackendType.{shape.backend_type},")
    a(f'        adapter_impl="{shape.adapter_impl}",')
    a(
        f'        operations=["parse"],  # {MARKER}: confirm against vendor docs; add "extract" if supported'
    )
    a("        provisioning=Provisioning(")
    if shape.category == "hosted":
        a('            byo_mode=["api_key"],')
        a('            auth="api_key",')
        a('            billing_target="caller_account",')
    elif shape.category == "self_hosted":
        a('            byo_mode=["endpoint"],')
        a('            auth="none",')
        a('            billing_target="caller_infra",')
    else:
        a("            byo_mode=[],")
        a('            auth="none",')
        a('            billing_target="caller_infra",')
    a("        ),")
    wm = ", ".join(f"WaitMode.{m}" for m in shape.wait_modes) or "WaitMode.INLINE"
    a(f"        wait_modes=[{wm}],")
    a(
        f"        capabilities=Capabilities(),  # {MARKER}: every flag is False/empty until verified — see the openreading.adapters runbook §3"
    )
    a(f"        cost=Cost(),  # {MARKER}: basis defaults 'unknown' — never invent a rate")
    a("        compliance=ComplianceProfile(")
    a(
        f'            trains_on_customer_data="unverified",  # {MARKER}: confirm from a primary source; stays fail-closed until then'
    )
    a("        ),")
    a("        runtime=RuntimeProfile(")
    a(f"            offline_capable={shape.category == 'local'},")
    a("        ),")
    if shape.category == "hosted":
        a("        credentials_spec=[")
        a("            CredentialField(")
        a('                key="api_key",')
        a("                required=True,")
        a("                secret=True,")
        a(f'                env=["{env}_API_KEY"],')
        a(
            f'                description="{MARKER}: guessed convention — verify against the vendor\'s real auth docs",'
        )
        a("            ),")
        a("        ],")
        a("        config_spec=[],")
    elif shape.category == "self_hosted":
        a("        credentials_spec=[")
        a("            CredentialField(")
        a('                key="api_key",')
        a("                required=False,")
        a("                secret=True,")
        a(f'                env=["{env}_API_KEY"],')
        a(
            f'                description="{MARKER}: optional — only if the self-hosted endpoint requires auth",'
        )
        a("            ),")
        a("        ],")
        a("        config_spec=[")
        a("            ConfigField(")
        a('                key="endpoint",')
        a("                required=False,")
        a(f'                env=["{env}_ENDPOINT"],')
        a(f'                description="{MARKER}: the self-hosted endpoint URL",')
        a("            ),")
        a("        ],")
    else:
        a("        credentials_spec=[],")
        a("        config_spec=[],")
    if shape.category == "hosted":
        a(f'        signup_url="{MARKER}: vendor signup URL",')
    else:
        a("        signup_url=None,")
    a(
        "        accepts_url=False,"
        f"  # {MARKER}: flip to True only once confirmed the vendor accepts a document URL directly"
    )
    if shape.category == "hosted":
        a(f'        live_gate_env=["{env}_API_KEY"],')
    elif shape.category == "self_hosted":
        a(f'        live_gate_env=["{env}_ENDPOINT"],')
    else:
        a("        live_gate_env=[],")
    a(
        f"        sources=[],  # {MARKER}: add Source(url=..., accessed=...) once primary docs are read"
    )
    a(
        "        # Native multi-document batch (BatchIntake/submit_many/normalize_many) is a separate,"
    )
    a(
        "        # opt-in addendum most adapters skip — see the openreading.adapters runbook's 'Native batch' section"
    )
    a("        # and internal/design/batch-intake.md §7 if this vendor actually offers one.")
    a("    )")
    a("")
    a("")

    # ---- class ----
    a(f"class {cls}Adapter(BackendAdapter):")
    ctor_client_type = f"{cls}Client | None" if has_client else "None"
    a(f"    def __init__(self, client: {ctor_client_type} = None) -> None:")
    a("        self.descriptor = _descriptor()")
    a("        self._client = client")
    if has_client:
        a(f"        self._active_client: {cls}Client | None = None")
    a("")
    a("    def capabilities(self) -> dict:")
    a("        return self.descriptor.capabilities.model_dump(mode='json')")
    a("")
    a("    def health(self) -> Health:")
    a("        return Health(")
    a("            ready=False,")
    a(f'            detail="{MARKER}: scaffolded, not yet implemented",')
    a(
        f'            missing_deps=["{MARKER}: implement submit/poll/normalize, see the openreading.adapters runbook"],'
    )
    a("        )")
    a("")
    if has_client:
        a(f"    def _get_client(self, ctx: RunContext) -> {cls}Client:")
        a("        if self._client is not None:")
        a("            return self._client")
        a("        creds = (ctx.credentials.values if ctx.credentials else {}) or {}")
        a('        key = creds.get("api_key")')
        if cred_required:
            a("        if not key:")
            a("            raise TerminalError(")
            a(
                f'                "{cls} needs credentials_ref → {{api_key}}", backend_code="no_credentials"'
            )
            a("            )")
        a(
            f"        endpoint = (ctx.runtime or {{}}).get('endpoint') or _DEFAULT_ENDPOINT  # {MARKER}"
        )
        a(f"        return _Httpx{cls}Client(key, endpoint)  # pragma: no cover")
        a("")
    a("    def submit(self, req: OpenReadingRequest, ctx: RunContext) -> Job:")
    if has_client:
        a("        self._get_client(ctx)")
    a("        raise NotImplementedError(")
    a(f'            "{MARKER}: {slug} submit() not yet implemented — see "')
    a('            "src/openreading/adapters/__init__.py"')
    a("        )")
    a("")
    if has_poll:
        a("    def poll(self, job: Job) -> Job:")
        a(f'        raise NotImplementedError("{MARKER}: {slug} poll() not yet implemented")')
        a("")
    if has_webhook:
        a("    def resolve_webhook(self, event: dict, job: Job) -> Job:")
        a(
            f'        raise NotImplementedError("{MARKER}: {slug} resolve_webhook() not yet implemented")'
        )
        a("")
    a("    def cancel(self, job: Job) -> Job:")
    a("        return super().cancel(job)")
    a("")
    a("    def normalize(self, job: Job, req: OpenReadingRequest) -> NormalizedResponse:")
    a("        raise NotImplementedError(")
    a(f'            "{MARKER}: {slug} normalize() not yet implemented — see "')
    a('            "src/openreading/adapters/__init__.py §3"')
    a("        )")
    a("")
    a("    def report_cost(self, job: Job) -> CostReport:")
    a(f'        raise NotImplementedError("{MARKER}: {slug} report_cost() not yet implemented")')
    a("")
    a("    def _map_error(self, e: Exception):")
    a("        if isinstance(e, (TerminalError, RetryableError)):")
    a("            return e")
    a("        return TerminalError(str(e), backend_code=type(e).__name__)")
    a("")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------
# CREATE: tests/fixtures/<slug>/ placeholder (no fixture CONTENT is ever generated — out of scope)
# --------------------------------------------------------------------------------------------


def render_fixtures_placeholder(slug: str) -> str:
    return (
        f"{MARKER}\n\n"
        f"No fixture content is generated for `{slug}` — `scripts/new_adapter.py` never invents "
        "wire-shape data (see the openreading.adapters runbook §1/§4). Add hand-written, documented-shape JSON "
        f"fixtures here (e.g. `parse.json`), then delete this file. Its presence is what makes "
        "`tests/test_scaffold_sentinel.py` fail until real fixtures exist.\n"
    )


# --------------------------------------------------------------------------------------------
# CREATE: tests/test_<pkg>.py and tests/test_<pkg>_faults.py
# --------------------------------------------------------------------------------------------


def render_test_happy_py(slug: str, shape: TemplateShape) -> str:
    cls = class_prefix(slug)
    pkg = pkg_name(slug)
    has_client = shape.category != "local"
    cred_required = shape.category == "hosted"
    lines: list[str] = []
    a = lines.append
    a(f'"""{cls} adapter — SCAFFOLDED by scripts/new_adapter.py. Every {MARKER} marker must be')
    a("resolved (real fixtures added, the fake client wired, the skip removed) before this file")
    a(
        'represents a finished adapter — see the openreading.adapters docstring (src/openreading/adapters/__init__.py) §4."""'
    )
    a("")
    a("from __future__ import annotations")
    a("")
    a("import pytest")
    a("")
    a(f"from openreading.adapters.{pkg} import {cls}Adapter")
    a("from openreading.testing import ConformanceCase, check_adapter_conformance")
    a("from openreading.types.request import OpenReadingRequest")
    a("from openreading.types.runtime import RunContext")
    a("")
    a("")
    a("def _req(**over) -> OpenReadingRequest:")
    a("    body = {")
    a(
        '        "document": {"url": "https://example.com/sample.pdf", "mime_type": "application/pdf"},'
    )
    a(f'        "backend": {{"id": "{slug}"}},')
    a("    }")
    a("    body.update(over)")
    a("    return OpenReadingRequest.model_validate(body)")
    a("")
    a("")
    if has_client:
        a(f"class Fake{cls}Client:")
        a(f'    """{MARKER}: replace with a fake that replays tests/fixtures/{slug}/*.json."""')
        a("")
        a("    def submit_job(self, body: dict) -> dict:")
        a(f'        raise NotImplementedError("{MARKER}: wire this fake to a real fixture")')
        if "POLL" in shape.wait_modes:
            a("")
            a("    def get_status(self, job_id: str) -> dict:")
            a(f'        raise NotImplementedError("{MARKER}: wire this fake to a real fixture")')
        a("")
        a("")
    a(
        f'@pytest.mark.skip(reason="{MARKER}: add tests/fixtures/{slug}/*.json and wire the fake client, then remove this skip")'
    )
    a(f"def test_{pkg}_conforms():")
    if has_client:
        a(f"    adapter = {cls}Adapter(client=Fake{cls}Client())")
    else:
        a(f"    adapter = {cls}Adapter()")
    a("    check_adapter_conformance(")
    a("        adapter, [ConformanceCase(request=_req(), deterministic=True, label='parse')]")
    a("    )")
    a("")
    a("")
    a("def test_health_reports_not_ready():")
    a(
        "    # a fresh scaffold reports not-ready with a reason — never a capability it hasn't earned."
    )
    a(f"    health = {cls}Adapter().health()")
    a("    assert health.ready is False")
    a("    assert health.missing_deps")
    a("")
    a("")
    if has_client and cred_required:
        a("def test_no_credentials_is_terminal():")
        a("    from openreading.types.errors import TerminalError")
        a("")
        a(f"    adapter = {cls}Adapter()  # no injected client, no creds in ctx")
        a("    with pytest.raises(TerminalError) as exc:")
        a("        adapter.submit(_req(), RunContext())")
        a('    assert exc.value.backend_code == "no_credentials"')
        a("")
        a("")
    a("@pytest.mark.live")
    a("def test_live_parse():  # pragma: no cover")
    a("    from tests.live_helpers import run_live, sample_pdf_request, skip_unless_creds")
    a("")
    a(f'    skip_unless_creds("{slug}")')
    a(f'    resp = run_live("{slug}", {cls}Adapter(), sample_pdf_request("{slug}"))')
    a("    assert resp.document.pages or resp.document.markdown")
    a("")
    return "\n".join(lines)


# the standard fault-coverage checklist the openreading.adapters runbook §4 names, as named/skipped stubs.
_FAULT_CHECKLIST_ALWAYS = [
    (
        "test_input_variants_accepted_and_rejected",
        "each accepted document intake + each rejected one (backend_code == 'unsupported_input')",
    ),
    (
        "test_submit_taxonomy_errors_reraised_unchanged",
        "a TerminalError/RetryableError raised by the client passes through submit() unchanged",
    ),
    (
        "test_submit_unexpected_exception_is_mapped",
        "a non-taxonomy exception from the client is wrapped via _map_error",
    ),
    (
        "test_provider_failure_response_at_submit_time",
        "a synchronous failure/rejected status at submit time becomes a TerminalError",
    ),
    (
        "test_op_specific_edges_partial_and_empty_result",
        "PARTIAL error state, empty result, missing optional wire fields",
    ),
    (
        "test_disabled_outputs_suppress_channels_and_backend_raw",
        "outputs.markdown/text/blocks/include_backend_raw=False suppress the matching channel",
    ),
    (
        "test_report_cost_before_and_after_result",
        "report_cost() both before a result exists and after",
    ),
    (
        "test_unsupported_schema_extraction_raises_if_applicable",
        "assert_supports() -> UnsupportedFeatureError if this backend can't do schema extraction",
    ),
]
_FAULT_CHECKLIST_POLL = [
    (
        "test_poll_terminal_status_raises",
        "a terminal failure status from poll() raises TerminalError (+ any cleanup asserted)",
    ),
    (
        "test_poll_status_fetch_exception_is_mapped",
        "an exception fetching poll status is mapped via _map_error",
    ),
    (
        "test_poll_in_progress_reschedules_then_succeeds",
        "an in-progress status reschedules next_poll_at, then succeeds via run_to_completion + FakeClock",
    ),
]
_FAULT_CHECKLIST_WEBHOOK = [
    (
        "test_webhook_mismatched_id_is_ignored",
        "an event whose id doesn't match webhook_token/backend_job_id is a no-op",
    ),
    (
        "test_webhook_idless_event_against_idless_job_is_ignored",
        "an id-less event against an id-less job is ignored, not hijacked (BL-70)",
    ),
    (
        "test_webhook_matching_event_finishes",
        "a matching event with a result body finishes the job",
    ),
    (
        "test_webhook_bare_notification_refetches",
        "a bare notification (no result body) triggers a refetch via the client",
    ),
    (
        "test_webhook_already_terminal_is_noop",
        "an event against an already-terminal job is a no-op",
    ),
    (
        "test_webhook_url_forwarded_to_vendor_on_submit",
        "submit()'s vendor request actually carries req.async_.webhook_url when set (BL-67) — assert against the fake client's recorded body, not just job.wait_mode",
    ),
]


def render_test_faults_py(slug: str, shape: TemplateShape) -> str:
    cls = class_prefix(slug)
    pkg = pkg_name(slug)
    checklist = list(_FAULT_CHECKLIST_ALWAYS)
    if "POLL" in shape.wait_modes:
        checklist += _FAULT_CHECKLIST_POLL
    if "WEBHOOK" in shape.wait_modes:
        checklist += _FAULT_CHECKLIST_WEBHOOK

    lines: list[str] = []
    a = lines.append
    a(f'"""{cls} adapter — fault-injection checklist, SCAFFOLDED by scripts/new_adapter.py. Every')
    a(
        "branch the openreading.adapters runbook §4's standard set names gets one named, skipped stub below; fill"
    )
    a("each in (small scripted fakes, no mocking libraries) or deliberately remove it, resolving")
    a(f'every {MARKER} marker, before this counts as done."""')
    a("")
    a("from __future__ import annotations")
    a("")
    a("import pytest")
    a("")
    a(f"from openreading.adapters.{pkg} import {cls}Adapter  # noqa: F401")
    a("")
    a("")
    for name, reason in checklist:
        a(f'@pytest.mark.skip(reason="{MARKER}: {reason}")')
        a(f"def {name}():")
        a(f'    raise AssertionError("{MARKER}: not yet implemented")')
        a("")
        a("")
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------------------------
# EDIT helpers — every one is idempotent (checks a slug-specific marker before inserting), so a
# --force re-run never double-applies an edit.
# --------------------------------------------------------------------------------------------


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def edit_registry(slug: str, shape: TemplateShape) -> bool:
    path = ADAPTERS_DIR / "registry.py"
    text = _read(path)
    pkg = pkg_name(slug)
    cls = class_prefix(slug)
    if f'"{slug}":' in text:
        return False
    import_line = f"from openreading.adapters.{pkg} import {cls}Adapter\n"
    # Insert the import in alphabetical order among the existing `adapters.<pkg>` imports.
    import_re = re.compile(r"^from openreading\.adapters\.(\w+) import .+\n", re.MULTILINE)
    imports = list(import_re.finditer(text))
    insert_at = imports[-1].end()
    for m in imports:
        if pkg < m.group(1):
            insert_at = m.start()
            break
    text = text[:insert_at] + import_line + text[insert_at:]
    # Append the dict entry just before BUILTIN_ADAPTERS' closing brace.
    marker = "\n}\n"
    idx = text.index("BUILTIN_ADAPTERS: dict")
    close_idx = text.index(marker, idx)
    text = text[:close_idx] + f'    "{slug}": {cls}Adapter,' + text[close_idx:]
    _write(path, text)
    return True


def edit_pyproject(slug: str, shape: TemplateShape) -> bool:
    path = REPO_ROOT / "pyproject.toml"
    text = _read(path)
    if f"\n{slug} = [" in text:
        return False
    if shape.category == "local":
        dep_line = f"{slug} = []  # {MARKER}: add the library's own PyPI dependency\n"
    else:
        dep_line = (
            f'{slug} = ["httpx>=0.27"]  # {MARKER}: confirm httpx is right '
            "(or swap for the vendor SDK)\n"
        )
    anchor = "\nhttp = ["
    idx = text.index(anchor)
    text = text[:idx] + "\n" + dep_line.rstrip("\n") + text[idx:]
    _write(path, text)
    return True


def edit_env_example(slug: str, shape: TemplateShape) -> bool:
    path = REPO_ROOT / ".env.example"
    text = _read(path)
    if f"--- {slug} " in text:
        return False
    env = env_prefix(slug)
    if shape.category == "local":
        block = f"\n# --- {slug} — no credentials needed (local library) ---\n"
    elif shape.category == "self_hosted":
        block = (
            f"\n# --- {slug} (self-hosted; {MARKER}: confirm no data leaves your infra) ---\n"
            f"{env}_ENDPOINT=\n"
            f"{env}_API_KEY=                        # optional, only if your endpoint requires auth\n"
        )
    else:
        block = f"\n# --- {slug} (signup: {MARKER} vendor signup URL) ---\n{env}_API_KEY=\n"
    _write(path, text.rstrip("\n") + "\n" + block)
    return True


def edit_readme(already_generated: bool) -> bool:
    if already_generated:
        return False
    path = REPO_ROOT / "README.md"
    text = _read(path)
    count_re = re.compile(r"\b(\d+) adapters\b")

    def _bump(m: re.Match) -> str:
        return f"{int(m.group(1)) + 1} adapters"

    new_text, n = count_re.subn(_bump, text)
    if n == 0:
        return False
    _write(path, new_text)
    return True


def edit_test_descriptor_specs(slug: str, shape: TemplateShape) -> bool:
    path = TESTS_DIR / "test_descriptor_specs.py"
    text = _read(path)
    if f'"{slug}":' in text:
        return False
    if shape.category == "hosted":
        cred_keys = "{'api_key'}"
        config_keys = None
    elif shape.category == "self_hosted":
        cred_keys = "{'api_key'}"
        config_keys = "{'endpoint'}"
    else:
        cred_keys = "set()"
        config_keys = None
    cred_keys = cred_keys.replace("'", '"')
    if config_keys:
        config_keys = config_keys.replace("'", '"')

    def _insert_before_close(src: str, dict_name: str, entry: str) -> str:
        idx = src.index(f"{dict_name} = {{")
        close_idx = src.index("\n}", idx)
        return src[:close_idx] + f'    "{slug}": {entry},' + src[close_idx:]

    text = _insert_before_close(text, "EXPECTED_CRED_KEYS", cred_keys)
    if config_keys:
        text = _insert_before_close(text, "EXPECTED_CONFIG_KEYS", config_keys)
    _write(path, text)
    return True


def edit_test_server(already_generated: bool) -> bool:
    if already_generated:
        return False
    path = TESTS_DIR / "test_server.py"
    text = _read(path)
    m = re.search(r"assert len\(rows\) == (\d+)", text)
    if not m:
        return False
    new_count = int(m.group(1)) + 1
    text = text[: m.start()] + f"assert len(rows) == {new_count}" + text[m.end() :]
    _write(path, text)
    return True


# --------------------------------------------------------------------------------------------
# orchestration
# --------------------------------------------------------------------------------------------


def _run_ruff(paths: list[Path]) -> None:
    """Best-effort auto-format/fix of the files we just wrote — makes the generator's output
    match `ruff format`/`ruff check` exactly regardless of hand-authored template spacing, so a
    freshly generated scaffold never fails `make verify`'s lint step for a formatting reason."""
    str_paths = [str(p) for p in paths]
    for cmd in (["ruff", "format"], ["ruff", "check", "--fix", "--quiet"]):
        # formatting is a nicety; a missing `uv`/`ruff` must not break generation itself
        with contextlib.suppress(OSError):
            subprocess.run(
                ["uv", "run", *cmd, *str_paths], cwd=REPO_ROOT, check=False, capture_output=True
            )


def generate(slug: str, template_slug: str, type_name: str, force: bool) -> list[str]:
    if not _SLUG_RE.match(slug):
        raise GeneratorError(
            f"invalid slug {slug!r} — must be lowercase, hyphen-separated (e.g. 'foo-vendor')"
        )
    if type_name not in TYPE_TEMPLATES:
        raise GeneratorError(
            f"unknown --type {type_name!r}. Valid types: {', '.join(sorted(TYPE_TEMPLATES))}. "
            "A shape outside this set isn't supported — follow the openreading.adapters runbook §1 by hand."
        )
    if template_slug not in ALL_TEMPLATE_SLUGS:
        raise GeneratorError(
            f"--template {template_slug!r} is not a supported scaffold template. Supported: "
            f"{', '.join(sorted(ALL_TEMPLATE_SLUGS))}. (anthropic-claude's native-batch shape is "
            "deliberately not generated — see the openreading.adapters runbook's 'Native batch' section and "
            "finish that one by hand.)"
        )
    if template_slug not in TYPE_TEMPLATES[type_name]:
        owning_type = next(t for t, s in TYPE_TEMPLATES.items() if template_slug in s)
        raise GeneratorError(
            f"--template {template_slug!r} belongs to --type {owning_type!r}, not {type_name!r}. "
            f"Valid templates for {type_name!r}: {', '.join(sorted(TYPE_TEMPLATES[type_name]))}."
        )

    pkg = pkg_name(slug)
    pkg_dir = ADAPTERS_DIR / pkg
    already_generated = pkg_dir.exists()
    if already_generated and not force:
        raise GeneratorError(f"{pkg_dir} already exists — pass --force to regenerate/overwrite it")

    shape = read_template_shape(template_slug)

    report: list[str] = []
    written_py_paths: list[Path] = []

    pkg_dir.mkdir(parents=True, exist_ok=True)
    init_path = pkg_dir / "__init__.py"
    _write(init_path, render_init_py(slug, shape))
    written_py_paths.append(init_path)
    report.append(f"CREATE {init_path.relative_to(REPO_ROOT)}")

    adapter_path = pkg_dir / "adapter.py"
    _write(adapter_path, render_adapter_py(slug, template_slug, shape))
    written_py_paths.append(adapter_path)
    report.append(f"CREATE {adapter_path.relative_to(REPO_ROOT)}")

    fixtures_dir = TESTS_DIR / "fixtures" / slug
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    fixtures_placeholder = fixtures_dir / f"{MARKER}.md"
    _write(fixtures_placeholder, render_fixtures_placeholder(slug))
    report.append(f"CREATE {fixtures_placeholder.relative_to(REPO_ROOT)}")

    test_happy_path = TESTS_DIR / f"test_{pkg}.py"
    _write(test_happy_path, render_test_happy_py(slug, shape))
    written_py_paths.append(test_happy_path)
    report.append(f"CREATE {test_happy_path.relative_to(REPO_ROOT)}")

    test_faults_path = TESTS_DIR / f"test_{pkg}_faults.py"
    _write(test_faults_path, render_test_faults_py(slug, shape))
    written_py_paths.append(test_faults_path)
    report.append(f"CREATE {test_faults_path.relative_to(REPO_ROOT)}")

    if edit_registry(slug, shape):
        report.append("EDIT   src/openreading/adapters/registry.py")
        written_py_paths.append(ADAPTERS_DIR / "registry.py")
    if edit_pyproject(slug, shape):
        report.append("EDIT   pyproject.toml")
    if edit_env_example(slug, shape):
        report.append("EDIT   .env.example")
    # The per-backend credential reference is the `openreading.credentials` module docstring
    # (a hand-written list, not a generated table): the generator does not edit it. Say so.
    report.append(
        "HAND   src/openreading/credentials.py -- add the backend's line to the docstring's "
        "'Per-backend reference'"
    )
    if edit_readme(already_generated):
        report.append("EDIT   README.md")
    if edit_test_descriptor_specs(slug, shape):
        report.append("EDIT   tests/test_descriptor_specs.py")
        written_py_paths.append(TESTS_DIR / "test_descriptor_specs.py")
    if edit_test_server(already_generated):
        report.append("EDIT   tests/test_server.py")
        written_py_paths.append(TESTS_DIR / "test_server.py")

    _run_ruff(written_py_paths)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="new_adapter.py",
        description="Scaffold a new openreading backend adapter (repo-development tool; see "
        "src/openreading/adapters/__init__.py).",
    )
    parser.add_argument("slug", help="hyphenated adapter slug, e.g. 'foo-vendor'")
    parser.add_argument(
        "--template", required=True, help="the closest existing adapter slug to copy the shape of"
    )
    parser.add_argument(
        "--type",
        required=True,
        choices=sorted(TYPE_TEMPLATES),
        help="the target API shape (the openreading.adapters runbook §0's picker table)",
    )
    parser.add_argument(
        "--force", action="store_true", help="overwrite an already-generated slug's files"
    )
    args = parser.parse_args(argv)

    try:
        report = generate(args.slug, args.template, args.type, args.force)
    except GeneratorError as e:
        print(f"[new_adapter] {e}", file=sys.stderr)
        return 2

    print(f"Scaffolded '{args.slug}' from --template {args.template} (--type {args.type}):")
    for line in report:
        print(f"  {line}")
    print()
    print(
        f"`make verify` will fail now — every {MARKER} marker is a real gap. Work "
        "the openreading.adapters runbook §1/§3 by hand, replacing each one with a sourced value, then remove "
        "the matching `@pytest.mark.skip` as each test is filled in."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
