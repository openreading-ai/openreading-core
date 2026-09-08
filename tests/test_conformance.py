"""Phase 0.8: prove the conformance kit. NullAdapter and a real-geometry FixtureAdapter pass;
deliberately-broken adapters (fabricated X channel, missing warning, non-idempotent) fail —
so the kit has teeth."""

from __future__ import annotations

from typing import Any

import pytest

from openreading.testing import (
    ConformanceCase,
    ConformanceError,
    FixtureAdapter,
    NullAdapter,
    check_adapter_conformance,
)
from openreading.testing.adapters import N, X
from openreading.types import (
    AdapterDescriptor,
    BackendInfo,
    BackendType,
    Block,
    BlockType,
    Capabilities,
    ChannelGrade,
    Document,
    JobState,
    NativeOrigin,
    NativeUnit,
    NormalizedResponse,
    Output,
    OutputChannels,
    Page,
    Provisioning,
    RuntimeProfile,
    Status,
    Table,
    TableCell,
    WaitMode,
    to_canonical,
)
from openreading.types.descriptor import ConfigField, CredentialField
from openreading.types.request import OpenReadingRequest
from openreading.types.response import BackendRaw

REQ = OpenReadingRequest.model_validate(
    {"document": {"path": "/doc.pdf", "mime_type": "application/pdf"}, "backend": {"id": "x"}}
)
CELLS_REQ = OpenReadingRequest.model_validate(
    {
        "document": {"path": "/doc.pdf", "mime_type": "application/pdf"},
        "backend": {"id": "x"},
        "outputs": {"tables": "cells"},
    }
)


def _case() -> ConformanceCase:
    return ConformanceCase(request=REQ, deterministic=True, label="pdf")


def _cells_case() -> ConformanceCase:
    return ConformanceCase(request=CELLS_REQ, deterministic=True, label="pdf-cells")


# --- NullAdapter passes -----------------------------------------------------------------


def test_null_adapter_conforms():
    check_adapter_conformance(NullAdapter(), [_case()])


# --- real-geometry FixtureAdapter passes ------------------------------------------------


def _geometry_descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        id="fixture-geo",
        type=BackendType.OSS_LIBRARY,
        protocol_version=1,
        provisioning=Provisioning(byo_mode=["pip"], auth="none"),
        wait_modes=[WaitMode.INLINE],
        capabilities=Capabilities(printed_tables="verified", input_formats=["pdf"]),
        runtime=RuntimeProfile(offline_capable=True, license="AGPL-3.0", sandbox="in_process"),
        adapter_impl="in_process",
        output=Output(
            channels=OutputChannels(
                markdown=ChannelGrade.DERIVABLE,
                text=N,
                blocks=N,
                block_bbox=N,
                block_confidence=X,
                typed_fields=X,
                table_cells=N,
            )
        ),
    )


def _geometry_response(req, raw):
    bbox = to_canonical(
        [72.0, 64.14, 200.0, 84.14],
        origin=NativeOrigin.TOP_LEFT,
        unit=NativeUnit.PDF_POINT,
        page_width=612.0,
        page_height=792.0,
        page=1,
    )
    resp = NormalizedResponse(
        status=Status(state="succeeded"),
        backend=BackendInfo(id="fixture-geo", type=BackendType.OSS_LIBRARY),
        document=Document(
            markdown="# Title",
            text="Title",
            page_count=1,
            pages=[
                Page(
                    page_number=1,
                    width=612.0,
                    height=792.0,
                    unit="pdf_point",
                    blocks=[
                        Block(type=BlockType.TITLE, text="Title", bbox=bbox, native_type="title"),
                        Block(
                            type=BlockType.TABLE,
                            native_type="table",
                            table=Table(
                                n_rows=1,
                                n_cols=2,
                                cells=[
                                    TableCell(row=0, col=0, text="a"),
                                    TableCell(row=0, col=1, text="b"),
                                ],
                                rows=[["a", "b"]],
                            ),
                        ),
                    ],
                )
            ],
        ),
        backend_raw=BackendRaw(
            encoding="json_serialized_object",
            object_class="fitz.Page.get_text.dict",
            payload={"blocks": []},
        ),
    )
    # deterministic parser: confidence is structurally impossible -> warn, never fake
    resp.add_warning(
        "confidence_unavailable", "deterministic parser has no confidence", "block_confidence"
    )
    return resp


def test_fixture_adapter_with_real_geometry_conforms():
    adapter = FixtureAdapter(_geometry_descriptor(), _geometry_response)
    check_adapter_conformance(adapter, [_case()])


# --- the kit catches violations ---------------------------------------------------------


class _FabricatesXChannel(NullAdapter):
    """Declares typed_fields X (inherited) but fabricates one anyway."""

    def normalize(self, job, ctx, req):
        from openreading.types import TypedField

        resp = super().normalize(job, ctx, req)
        resp.typed_fields = {"ghost": TypedField(value="fabricated")}
        return resp


def test_kit_catches_fabricated_x_channel():
    with pytest.raises(ConformanceError, match="graded X but present"):
        check_adapter_conformance(_FabricatesXChannel(), [_case()])


class _SkipsWarnings(NullAdapter):
    """Declares blocks X but never warns when blocks are requested."""

    def normalize(self, job, ctx, req):
        return NormalizedResponse(
            status=Status(state="succeeded"),
            backend=BackendInfo(id=self.descriptor.id, type=self.descriptor.type),
            document=Document(text=""),
        )


def test_kit_catches_missing_warning_for_requested_x_channel():
    with pytest.raises(ConformanceError, match="no warning surfaced"):
        check_adapter_conformance(_SkipsWarnings(), [_case()])


class _NonIdempotent(NullAdapter):
    def __init__(self):
        super().__init__("flaky-null")
        self._n = 0

    def normalize(self, job, ctx, req):
        self._n += 1
        resp = super().normalize(job, ctx, req)
        resp.document.text = f"call-{self._n}"  # changes every call
        return resp


def test_kit_catches_non_idempotent_deterministic_adapter():
    with pytest.raises(ConformanceError, match="not idempotent"):
        check_adapter_conformance(_NonIdempotent(), [_case()])


def test_kit_requires_at_least_one_case():
    with pytest.raises(ConformanceError, match="at least one"):
        check_adapter_conformance(NullAdapter(), [])


# --- BL-114: the five static/per-case self-checks (capabilities()/health() return the declared
# type, submitted Job.backend_id matches the descriptor, Job.wait_mode is declared, the driver
# reaches SUCCEEDED) each get their own purpose-built broken fixture proving the kit fires -------


class _CapabilitiesWrongType(NullAdapter):
    """capabilities() returns a list instead of the declared dict."""

    def capabilities(self):
        return ["not", "a", "dict"]


def test_kit_catches_capabilities_returning_the_wrong_type():
    with pytest.raises(ConformanceError, match=r"capabilities\(\) must return a dict"):
        check_adapter_conformance(_CapabilitiesWrongType(), [_case()])


class _HealthWrongType(NullAdapter):
    """health() returns a bare dict instead of the declared Health."""

    def health(self):
        return {"ready": True}


def test_kit_catches_health_returning_the_wrong_type():
    with pytest.raises(ConformanceError, match=r"health\(\) must return a Health"):
        check_adapter_conformance(_HealthWrongType(), [_case()])


class _StampsWrongBackendId(NullAdapter):
    """submit() stamps the job with another backend's id instead of its own descriptor id."""

    def submit(self, req, ctx):
        job = super().submit(req, ctx)
        job.backend_id = "impostor-backend"
        return job


def test_kit_catches_a_submitted_job_stamped_with_the_wrong_backend_id():
    with pytest.raises(ConformanceError, match=r"job\.backend_id='impostor-backend'"):
        check_adapter_conformance(_StampsWrongBackendId(), [_case()])


class _SubmitsUndeclaredWaitMode(NullAdapter):
    """submit() returns a job in a wait_mode the descriptor never declares (only INLINE)."""

    def submit(self, req, ctx):
        job = super().submit(req, ctx)
        job.wait_mode = WaitMode.POLL
        return job


def test_kit_catches_a_submitted_job_in_an_undeclared_wait_mode():
    with pytest.raises(ConformanceError, match="not in descriptor.wait_modes"):
        check_adapter_conformance(_SubmitsUndeclaredWaitMode(), [_case()])


class _SubmitsFailedJob(NullAdapter):
    """submit() itself returns a terminal-but-FAILED job — a broken driver/backend that never
    reaches the happy path a ConformanceCase is supposed to prove."""

    def submit(self, req, ctx):
        return self.new_job(WaitMode.INLINE, state=JobState.FAILED)


def test_kit_catches_a_driver_that_never_reaches_succeeded():
    with pytest.raises(ConformanceError, match="expected SUCCEEDED after driver"):
        check_adapter_conformance(_SubmitsFailedJob(), [_case()])


# --- the cost check has teeth (report_cost feeds response.usage) -------------------------


def _cost_findings(adapter) -> list[tuple[str, str]]:
    """Run `_check_cost` directly against a job the adapter just finished, collecting findings."""
    from openreading.testing.conformance import _check_cost

    found: list[tuple[str, str]] = []
    job = adapter.submit(REQ, ConformanceCase(request=REQ).ctx)
    _check_cost(adapter, job, lambda check, case, msg: found.append((check, msg)), "pdf")
    return found


class _RaisingMeter(NullAdapter):
    def report_cost(self, job):
        raise RuntimeError("meter exploded")


def test_check_cost_fires_when_report_cost_raises():
    findings = _cost_findings(_RaisingMeter())
    assert len(findings) == 1 and findings[0][0] == "cost"
    assert "report_cost raised" in findings[0][1]


def test_check_cost_is_silent_on_a_conforming_adapter():
    assert _cost_findings(NullAdapter()) == []


# --- §7 rollout: ConformanceReport + strict_checks (advisory → strict per adapter) -------


def test_check_returns_conformance_report():
    report = check_adapter_conformance(
        FixtureAdapter(_geometry_descriptor(), _geometry_response), [_case()]
    )
    assert report.violations == []
    assert isinstance(report.advisories, list)


def test_classification_strict_advisory_and_permanent():
    from openreading.testing.conformance import _is_violation

    assert _is_violation("C4", set()) is True  # always strict
    assert _is_violation("C7", set()) is True  # strict from Phase A
    assert _is_violation("C1", set()) is False  # advisory by default...
    assert _is_violation("C1", {"C1"}) is True  # ...promotable per adapter
    assert _is_violation("C6", set()) is False and _is_violation("C6", {"C6"}) is True
    assert _is_violation("C11", set()) is False and _is_violation("C11", {"C11"}) is False  # never


class _MarkupInText(NullAdapter):
    """Leaks a markdown heading + HTML into the plain text channel (text is N, so not a C4)."""

    def normalize(self, job, ctx, req):
        resp = super().normalize(job, ctx, req)
        resp.document.text = "# Heading\nplain body line\n<b>bold</b>"
        return resp


def test_c1_markup_in_text_is_strict_by_default_phase_c():
    # Phase C: the kit default flipped to all-strict (C1/C6/C7). Markup in the text channel now
    # RAISES by default; an explicit empty strict set opts back out to advisory.
    with pytest.raises(ConformanceError, match="markup in plain text"):
        check_adapter_conformance(_MarkupInText(), [_case()])


def test_c1_can_be_opted_out_to_advisory():
    report = check_adapter_conformance(_MarkupInText(), [_case()], strict_checks=frozenset())
    assert any(f.check == "C1" for f in report.advisories)
    assert report.violations == []


# --- BL-40: `_MarkupInText` above pairs an ATX heading with an HTML pair in one string, so the
# early return on the ATX match means `_markup_reason`'s other two signatures never run. These two
# fixtures each carry exactly one signature and no ATX heading, so the early return can't mask them.


class _MarkupTableSeparatorOnly(NullAdapter):
    """Leaks ONLY a markdown table-separator row into plain text — no ATX heading, no HTML pair, so
    `_markup_reason`'s early return on the ATX signature can't mask this branch."""

    def normalize(self, job, ctx, req):
        resp = super().normalize(job, ctx, req)
        resp.document.text = "plain body line\n| --- | --- |\nmore plain text"
        return resp


def test_c1_flags_a_bare_markdown_table_separator_row():
    with pytest.raises(ConformanceError, match="markdown table separator row"):
        check_adapter_conformance(_MarkupTableSeparatorOnly(), [_case()])


class _MarkupHtmlPairOnly(NullAdapter):
    """Leaks ONLY an HTML tag pair into plain text — no ATX heading, no table-separator row, so
    neither earlier `_markup_reason` signature can mask this branch."""

    def normalize(self, job, ctx, req):
        resp = super().normalize(job, ctx, req)
        resp.document.text = "plain body line\n<span>emphasis</span>\nmore plain text"
        return resp


def test_c1_flags_a_bare_html_tag_pair():
    with pytest.raises(ConformanceError, match="HTML tag pair"):
        check_adapter_conformance(_MarkupHtmlPairOnly(), [_case()])


class _DGradeButEmpty(NullAdapter):
    """Grades markdown D but never produces it and never warns → C6 deliver-or-warn."""

    def __init__(self):
        super().__init__("d-empty")
        self.descriptor.output.channels.markdown = ChannelGrade.DERIVABLE

    def normalize(self, job, ctx, req):
        resp = NormalizedResponse(
            status=Status(state="succeeded"),
            backend=BackendInfo(id=self.descriptor.id, type=self.descriptor.type),
            document=Document(text="hello world"),
        )
        resp.add_warning("channel_unsupported", "blocks not produced", "blocks")  # X channel warned
        return resp


def test_c6_deliver_or_warn_is_strict_by_default_phase_c():
    with pytest.raises(ConformanceError, match="neither .*populated nor named in a warning"):
        check_adapter_conformance(_DGradeButEmpty(), [_case()])


def test_c6_can_be_opted_out_to_advisory():
    report = check_adapter_conformance(_DGradeButEmpty(), [_case()], strict_checks=frozenset())
    assert any(f.check == "C6" for f in report.advisories)
    assert report.violations == []


def test_c7_confidence_bounds_walk_flags_out_of_range():
    from openreading.testing.conformance import _check_confidence_bounds

    found: list[tuple[str, str]] = []
    resp = {
        "document": {
            "confidence": 5.0,  # doc-level out of range
            "doc_type": {"confidence": 0.9},  # ok
            "pages": [{"blocks": [{"type": "text", "confidence": -0.1}]}],  # block out of range
        },
        "typed_fields": {"total": {"confidence": "High"}},  # qualitative string → exempt
    }
    _check_confidence_bounds(resp, lambda cid, c, m: found.append((cid, m)), "case")
    assert len(found) == 2 and all(cid == "C7" for cid, _ in found)


def test_c7_confidence_bounds_exempts_bool_values():
    """BL-40: `isinstance(val, bool)` guards the range check because `isinstance(True, int)` is
    True in Python — proving that guard fires (not just that it exists) needs a fixture that
    actually sends a bool where a confidence value is expected, for both True and False."""
    from openreading.testing.conformance import _check_confidence_bounds

    found: list[tuple[str, str]] = []
    resp = {
        "document": {
            "confidence": True,  # would be in-range as an int anyway; the guard's the point
            "doc_type": {"confidence": False},
            "pages": [{"blocks": [{"type": "text", "confidence": True}]}],
        },
    }
    _check_confidence_bounds(resp, lambda cid, c, m: found.append((cid, m)), "case")
    assert found == []


# --- BL-5: a warning must NAME the dropped channel, not merely contain its letters ------


# 'context' contains 'text' and 'notable' contains 'table' — under a substring search over the
# concatenated warning blob either of these vouches for a channel nothing actually mentions.
_COLLIDING_WARNINGS = (
    ("output_truncated", "the model context window truncated this document", "status"),
    ("no_notable_regions", "no notable regions were detected on this page", "regions"),
)


def _bare_response(adapter) -> NormalizedResponse:
    resp = NormalizedResponse(
        status=Status(state="succeeded"),
        backend=BackendInfo(id=adapter.descriptor.id, type=adapter.descriptor.type),
        # empty `pages` satisfies the schema's anyOf denominator without populating text/markdown
        document=Document(pages=[]),
    )
    for code, message, field in _COLLIDING_WARNINGS:
        resp.add_warning(code, message, field)
    # the OTHER X channels are named honestly, so only text/table_cells are under test
    resp.add_warning("channel_unsupported", "markdown not produced", "markdown")
    resp.add_warning("channel_unsupported", "blocks not produced", "blocks")
    return resp


class _DropsXChannelsUnnamed(NullAdapter):
    """text and table_cells graded X and both requested, but no warning names either."""

    def __init__(self):
        super().__init__("colliding-x")
        self.descriptor.output.channels.text = X
        self.descriptor.output.channels.table_cells = X

    def normalize(self, job, ctx, req):
        return _bare_response(self)


class _DropsGradedChannelsUnnamed(NullAdapter):
    """text (N) and table_cells (D) requested, delivered empty, and no warning names either."""

    def __init__(self):
        super().__init__("colliding-d")
        self.descriptor.output.channels.table_cells = ChannelGrade.DERIVABLE

    def normalize(self, job, ctx, req):
        return _bare_response(self)


def test_c5_colliding_warning_word_does_not_count_as_naming_the_channel():
    report = check_adapter_conformance(
        _DropsXChannelsUnnamed(), [_cells_case()], raise_on_violation=False
    )
    c5 = [f.message for f in report.violations if f.check == "C5"]
    assert any("'text'" in m for m in c5), c5
    assert any("'table_cells'" in m for m in c5), c5


def test_c6_colliding_warning_word_does_not_count_as_naming_the_channel():
    report = check_adapter_conformance(
        _DropsGradedChannelsUnnamed(), [_cells_case()], raise_on_violation=False
    )
    c6 = [f.message for f in report.violations if f.check == "C6"]
    assert any("'text'" in m for m in c6), c6
    assert any("'table_cells'" in m for m in c6), c6


def test_warned_matches_the_field_attribute_and_whole_words_only():
    from openreading.testing.conformance import _warned

    keys = ("code", "message", "field")
    collisions = [dict(zip(keys, w, strict=True)) for w in _COLLIDING_WARNINGS]
    assert _warned(collisions, "text") is False
    assert _warned(collisions, "table_cells") is False
    # primary signal: the field attribute, bare or as one segment of a response path
    assert _warned([{"field": "text"}], "text") is True
    assert _warned([{"field": "document.text"}], "text") is True
    assert _warned([{"field": "pages[0].blocks"}], "blocks") is True
    assert _warned([{"field": "typed_fields"}], "typed_fields") is True
    assert _warned([{"field": "context"}], "text") is False
    # secondary signal: the channel name as whole words in code/message
    assert _warned([{"code": "table_cells_unsupported"}], "table_cells") is True
    assert _warned([{"message": "the table_cells channel is empty"}], "table_cells") is True
    assert _warned([{"message": "no notable table"}], "table_cells") is False


def _spec_descriptor(creds, config) -> AdapterDescriptor:
    return AdapterDescriptor(
        id="spec-probe",
        type=BackendType.HOSTED_API,
        protocol_version=1,
        provisioning=Provisioning(byo_mode=["api_key"], auth="api_key"),
        wait_modes=[WaitMode.INLINE],
        capabilities=Capabilities(),
        runtime=RuntimeProfile(),
        credentials_spec=creds,
        config_spec=config,
        signup_url="https://example.test/signup",
    )


def test_kit_catches_non_secret_field_declared_as_a_credential():
    """The the openreading.adapters runbook rule, mechanized: endpoints/regions/resource ids are non-secret, so
    they belong in config_spec (→ ctx.runtime), never in credentials_spec (→ ctx.credentials)."""
    from openreading.testing.conformance import _check_credential_spec

    found: list[tuple[str, str]] = []
    _check_credential_spec(
        _spec_descriptor(
            [
                CredentialField(key="api_key", env=["PROBE_API_KEY"]),
                CredentialField(key="region", secret=False, env=["PROBE_REGION"]),
            ],
            [],
        ),
        lambda cid, c, m: found.append((cid, m)),
    )
    assert [cid for cid, _ in found] == ["static"]
    assert "region" in found[0][1] and "config_spec" in found[0][1]


def test_kit_accepts_the_same_non_secret_field_in_config_spec():
    from openreading.testing.conformance import _check_credential_spec

    found: list[str] = []
    _check_credential_spec(
        _spec_descriptor(
            [CredentialField(key="api_key", env=["PROBE_API_KEY"])],
            [ConfigField(key="region", env=["PROBE_REGION"])],
        ),
        lambda cid, c, m: found.append(m),
    )
    assert found == []


def test_non_secret_credential_check_is_a_raising_violation():
    from openreading.testing.conformance import _is_violation

    assert _is_violation("static", set()) is True


def test_c3_unbalanced_code_fence_flagged_balanced_ok():
    from openreading.testing.conformance import _check_markdown_gfm

    bad: list[str] = []
    _check_markdown_gfm(
        {"document": {"markdown": "```python\nx = 1\n"}}, lambda cid, c, m: bad.append(cid), "c"
    )
    assert bad == ["C3"]
    ok: list[str] = []
    _check_markdown_gfm(
        {"document": {"markdown": "# t\n```\nx=1\n```"}}, lambda cid, c, m: ok.append(cid), "c"
    )
    assert ok == []


# --- _check_credential_spec: the v0.2 BYO declaration has teeth --------------------------


def _spec_findings(desc: AdapterDescriptor) -> list[tuple[str, str]]:
    """Run `_check_credential_spec` directly against a descriptor, collecting its findings."""
    from openreading.testing.conformance import _check_credential_spec

    found: list[tuple[str, str]] = []
    _check_credential_spec(desc, lambda check, case, msg: found.append((check, msg)))
    return found


def _byo_descriptor(
    *,
    backend_type: BackendType = BackendType.HOSTED_API,
    auth: str = "api_key",
    byo_mode: list[str] | None = None,
    credentials_spec: list[CredentialField] | None = None,
    config_spec: list[ConfigField] | None = None,
    signup_url: str | None = "https://example.invalid/signup",
) -> AdapterDescriptor:
    return AdapterDescriptor(
        id="byo",
        type=backend_type,
        protocol_version=1,
        provisioning=Provisioning(
            byo_mode=["api_key"] if byo_mode is None else byo_mode,
            auth=auth,
        ),
        wait_modes=[WaitMode.INLINE],
        capabilities=Capabilities(),
        runtime=RuntimeProfile(),
        adapter_impl="http",
        output=Output(channels=OutputChannels(text=N)),
        credentials_spec=credentials_spec or [],
        config_spec=config_spec or [],
        signup_url=signup_url,
    )


def test_credential_spec_is_silent_for_a_backend_that_needs_no_environment():
    desc = _byo_descriptor(
        backend_type=BackendType.OSS_LIBRARY, auth="none", byo_mode=["pip"], signup_url=None
    )
    assert _spec_findings(desc) == []


def test_credential_spec_flags_a_keyed_backend_that_declares_nothing():
    assert _spec_findings(_byo_descriptor()) == [
        (
            "static",
            "backend needs env credentials/config but declares neither "
            "credentials_spec nor config_spec",
        )
    ]


def test_credential_spec_flags_an_unauthenticated_container_backend_that_declares_nothing():
    """The env need is not only auth: a self-hosted endpoint/container URL must be declared too,
    or the broker has nothing to resolve and the readiness UI cannot ask for it."""
    desc = _byo_descriptor(
        backend_type=BackendType.SELF_HOSTED_MODEL,
        auth="none",
        byo_mode=["container"],
        signup_url=None,
    )
    assert [check for check, _ in _spec_findings(desc)] == ["static"]


def test_credential_spec_flags_an_empty_key_and_a_field_naming_no_env_var():
    desc = _byo_descriptor(
        credentials_spec=[CredentialField(key="", env=["ACME_API_KEY"])],
        config_spec=[ConfigField(key="endpoint", env=[])],
    )
    assert _spec_findings(desc) == [
        ("static", "spec field has an empty key"),
        ("static", "spec field 'endpoint' names no env var"),
    ]


def test_credential_spec_requires_a_signup_url_on_a_hosted_api():
    desc = _byo_descriptor(
        credentials_spec=[CredentialField(key="api_key", env=["ACME_API_KEY"])], signup_url=None
    )
    assert _spec_findings(desc) == [("static", "hosted_api must declare a signup_url")]


def test_credential_spec_is_silent_on_a_fully_declared_hosted_api():
    desc = _byo_descriptor(credentials_spec=[CredentialField(key="api_key", env=["ACME_API_KEY"])])
    assert _spec_findings(desc) == []


# --- _check_identity: the response names the backend that actually produced it ------------


def _identity_findings(resp: dict[str, Any], desc: AdapterDescriptor) -> list[tuple[str, str]]:
    from openreading.testing.conformance import _check_identity

    found: list[tuple[str, str]] = []
    _check_identity(resp, desc, lambda check, case, msg: found.append((check, msg)), "pdf")
    return found


def test_identity_flags_a_response_that_disagrees_with_the_descriptor():
    desc = NullAdapter().descriptor
    assert _identity_findings({"backend": {"id": "impostor", "type": desc.type.value}}, desc) == [
        ("identity", "response.backend.id='impostor' != descriptor 'null'")
    ]
    assert _identity_findings({"backend": {"id": desc.id, "type": "hosted_api"}}, desc) == [
        ("identity", "response.backend.type='hosted_api' != 'oss_library'")
    ]


def test_identity_is_silent_when_the_response_names_its_own_descriptor():
    desc = NullAdapter().descriptor
    assert _identity_findings({"backend": {"id": desc.id, "type": desc.type.value}}, desc) == []


# --- the two schema gates: an invalid descriptor, an invalid response ---------------------


class _SchemaInvalidDescriptor(NullAdapter):
    """Ships an `adapter_impl` outside the descriptor schema's enum. AdapterDescriptor does not
    re-validate on assignment, so a hand-edited or drifted descriptor reaches the kit intact —
    which is the whole reason the kit re-validates it against the vendored schema."""

    def __init__(self) -> None:
        super().__init__("bad-descriptor")
        self.descriptor.adapter_impl = "teleporter"


def test_kit_catches_a_descriptor_that_fails_the_descriptor_schema():
    with pytest.raises(ConformanceError, match="descriptor fails descriptor schema"):
        check_adapter_conformance(_SchemaInvalidDescriptor(), [_case()])


class _StaleSchemaVersion(NullAdapter):
    """Stamps a superseded `schema_version` on the envelope (the field is a schema `const`, so the
    stale stamp alone is fatal) and fabricates an X channel in the same response."""

    def __init__(self) -> None:
        super().__init__("stale-envelope")

    def normalize(self, job, ctx, req):
        from openreading.types import TypedField

        resp = super().normalize(job, ctx, req)
        resp.schema_version = "0.2"
        resp.typed_fields = {"ghost": TypedField(value="fabricated")}
        return resp


def test_kit_catches_a_response_that_fails_the_response_schema():
    with pytest.raises(ConformanceError, match="normalized response fails response schema"):
        check_adapter_conformance(_StaleSchemaVersion(), [_case()])


def test_a_schema_invalid_response_is_not_walked_any_further():
    """The channel walk reads a shape the schema already vouched for, so a schema failure ends the
    case: the C4 fabrication riding along in the same response is deliberately never reported."""
    report = check_adapter_conformance(_StaleSchemaVersion(), [_case()], raise_on_violation=False)
    assert [f.check for f in report.violations] == ["schema"]
    assert report.advisories == []


# --- C8: canonical geometry — the kit's first-named guarantee -----------------------------

_NATIVE_BBOX = {"x": 72.0, "y": 64.14, "w": 128.0, "h": 20.0, "unit": "pdf_point"}


def _bbox_findings(resp: dict[str, Any]) -> list[tuple[str, str]]:
    from openreading.testing.conformance import _check_bboxes

    found: list[tuple[str, str]] = []
    _check_bboxes(resp, lambda check, case, msg: found.append((check, msg)), "pdf")
    return found


def _one_page(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    return {"document": {"pages": [{"blocks": blocks}]}}


def test_c8_flags_a_bbox_outside_the_unit_square_and_a_cell_missing_its_native_original():
    findings = _bbox_findings(
        _one_page(
            [
                {
                    "type": "text",
                    "bbox": {
                        "x": 0.1,
                        "y": 0.2,
                        "w": 1.4,
                        "h": 0.1,
                        "page": 1,
                        "bbox_native": _NATIVE_BBOX,
                    },
                },
                {
                    "type": "table",
                    "table": {
                        "cells": [
                            {
                                "row": 0,
                                "col": 0,
                                "bbox": {"x": 0.1, "y": 0.2, "w": 0.1, "h": 0.1, "page": 1},
                            }
                        ]
                    },
                },
            ]
        )
    )
    assert [check for check, _ in findings] == ["C8", "C8"]
    assert findings[0][1] == "bbox.w=1.4 out of [0,1] at blocks[0]"
    assert findings[1][1].startswith("bbox missing bbox_native at blocks[1].table.cells[0]")


def test_c8_is_silent_on_a_canonical_bbox_carrying_its_native_original():
    assert (
        _bbox_findings(
            _one_page(
                [
                    {
                        "type": "text",
                        "bbox": {
                            "x": 0.1,
                            "y": 0.2,
                            "w": 0.3,
                            "h": 0.1,
                            "page": 1,
                            "bbox_native": _NATIVE_BBOX,
                        },
                    }
                ]
            )
        )
        == []
    )


# --- C11: live code, and permanently advisory ---------------------------------------------


def _coherence_findings(resp: dict[str, Any]) -> list[tuple[str, str]]:
    from openreading.testing.conformance import _check_text_blocks_coherence

    found: list[tuple[str, str]] = []
    _check_text_blocks_coherence(resp, lambda check, case, msg: found.append((check, msg)), "pdf")
    return found


def _text_and_spine(text: str | None, block_text: str | None) -> dict[str, Any]:
    doc: dict[str, Any] = {}
    if text is not None:
        doc["text"] = text
    if block_text is not None:
        doc["pages"] = [{"blocks": [{"type": "text", "text": block_text}]}]
    return {"document": doc}


def test_c11_flags_a_text_channel_that_diverges_from_the_block_spine():
    findings = _coherence_findings(
        _text_and_spine("alpha beta gamma delta", "zulu yankee xray whiskey")
    )
    assert [check for check, _ in findings] == ["C11"]
    assert "token similarity 0.00 < 0.5" in findings[0][1]


def test_c11_is_silent_when_the_spine_agrees_or_one_side_is_absent():
    assert _coherence_findings(_text_and_spine("alpha beta gamma", "alpha beta gamma")) == []
    assert _coherence_findings(_text_and_spine("alpha beta gamma", None)) == []
    assert _coherence_findings(_text_and_spine(None, "alpha beta gamma")) == []


class _TextDivergesFromBlocks(NullAdapter):
    """Grades blocks N and populates them, but document.text is unrelated prose — the shape a real
    backend reaches by filtering headers out of one channel and not the other."""

    def __init__(self) -> None:
        super().__init__("divergent-spine")
        self.descriptor.output.channels.blocks = N

    def normalize(self, job, ctx, req):
        resp = NormalizedResponse(
            status=Status(state="succeeded"),
            backend=BackendInfo(id=self.descriptor.id, type=self.descriptor.type),
            document=Document(
                text="alpha beta gamma delta epsilon",
                page_count=1,
                pages=[
                    Page(
                        page_number=1,
                        blocks=[Block(type=BlockType.TEXT, text="zulu yankee xray whiskey victor")],
                    )
                ],
            ),
        )
        resp.add_warning("channel_unsupported", "markdown not produced", "markdown")
        return resp


def test_c11_is_reachable_through_the_kit_and_never_raises():
    """C11 is live code an adapter can actually reach, not a dead branch — and it stays an advisory
    even when a caller explicitly names it in strict_checks."""
    for strict in ({"C1", "C6"}, {"C11"}):
        report = check_adapter_conformance(
            _TextDivergesFromBlocks(), [_case()], strict_checks=strict
        )
        assert report.violations == []
        assert [f.check for f in report.advisories] == ["C11"]


# --- Ledger T4a: R1/R2/R3 negative cases -------------------------------------------------------
#
# Phase C review (Finding 4): every existing R1/R2/R3 test (across all 15 adapters' own
# `adapter_factory=` call sites) only proves "a compliant adapter passes" — none prove the checks
# actually catch a non-compliant one. These three, following this file's own established
# deliberately-broken-adapter pattern, give each of R1/R2/R3 at least one genuine negative case.


class _CachesClient(NullAdapter):
    """R2 (AC-7) violation: caches a client-like object under `_active_client` during submit() —
    exactly the pattern all 8 T4a adapters used to have, and the one R2 exists to catch (the reviewer's
    own Finding 3 repro confirmed this shape fires; this pins it as a real regression test)."""

    def submit(self, req, ctx):
        job = super().submit(req, ctx)
        self._active_client = object()
        return job


def test_kit_catches_a_cached_client_after_submit_r2():
    with pytest.raises(ConformanceError, match="must not cache"):
        check_adapter_conformance(
            _CachesClient(), [_case()], adapter_factory=lambda: _CachesClient()
        )


class _BreaksJobRoundTrip(NullAdapter):
    """R1 (AC-5) violation: to_dict() emits a `state` value from_dict() can't parse back. The Job
    still survives a plain json.dumps (R3's post-submit check stays clean) — only the from_dict()
    half of the to_dict/json.dumps/json.loads/from_dict round trip R1's fresh-instance-resume
    check performs fails, exactly the shape a real resume would crash on."""

    def submit(self, req, ctx):
        job = super().submit(req, ctx)
        real_to_dict = job.to_dict

        def _broken_to_dict():
            d = real_to_dict()
            d["state"] = "not-a-real-state"
            return d

        job.to_dict = _broken_to_dict
        return job


def test_kit_catches_a_job_that_does_not_survive_the_round_trip_r1():
    with pytest.raises(ConformanceError, match="did not survive the to_dict/json/from_dict"):
        check_adapter_conformance(
            _BreaksJobRoundTrip(), [_case()], adapter_factory=lambda: _BreaksJobRoundTrip()
        )


class _FailedJobDoesNotSerialize(NullAdapter):
    """R3 (AC-6) violation: a FAILED-state job's to_dict() embeds a raw, non-JSON-serializable
    object. R3/AC-6's own contract is that `json.dumps(job.to_dict())` succeeds at every stage of
    the lifecycle a caller can observe, FAILED included (types/job.py's own docstring names FAILED
    explicitly) — this fixture never satisfies that. A permanently-broken to_dict() also trips R1's
    fresh-instance-resume check (it round-trips through the same to_dict()) — expected, and not
    asserted against below; the test only needs to confirm R3's own message fires."""

    def submit(self, req, ctx):
        job = self.new_job(WaitMode.INLINE, state=JobState.FAILED)
        real_to_dict = job.to_dict

        def _broken_to_dict():
            d = real_to_dict()
            d["_broken"] = {1, 2, 3}  # a set is not JSON-serializable
            return d

        job.to_dict = _broken_to_dict
        return job


def test_kit_catches_a_failed_job_that_does_not_serialize_r3():
    with pytest.raises(ConformanceError, match="is not JSON-serializable"):
        check_adapter_conformance(
            _FailedJobDoesNotSerialize(),
            [_case()],
            adapter_factory=lambda: _FailedJobDoesNotSerialize(),
        )
