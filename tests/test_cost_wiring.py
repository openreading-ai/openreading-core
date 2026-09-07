"""BL-8 — `report_cost()` reaches `response.usage`.

`report_cost` is one of the eight mandatory adapter methods and every adapter implements it, but
nothing in production ever called it: `usage.cost_usd` came back null for every real backend even
though response.v0.3 says "the router computes cost_usd via the pricing model". These tests pin
the wiring at the choke points the router owns (chain executor / strategy-engine leaf / the
directly-named backend run), the fill-only merge rule, and the degrade-don't-fail rule when an
adapter's meter raises. All offline: a captured Reducto fixture is the paid backend.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
import respx

from openreading import api, run_batch, schemas
from openreading.adapters.reducto import ReductoAdapter
from openreading.credentials import EnvCredentialBroker
from openreading.router.clock import FakeClock
from openreading.router.cost import merge_cost_report
from openreading.router.executor import execute_plan
from openreading.router.registry import Registry
from openreading.router.router import RoutePlan, RouterConfig
from openreading.strategies import StrategyConfig, compile_strategy, run_strategy
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.cost import CostReport, infra_only
from openreading.types.enums import BackendType, CostBasis, ResponseState
from openreading.types.request import OpenReadingRequest
from openreading.types.response import (
    BackendInfo,
    Document,
    NormalizedResponse,
    Status,
    Usage,
)
from openreading.types.runtime import RunContext
from tests.fakes import ConfigurableBackend, make_backend

_PARSE = json.loads((Path(__file__).parent / "fixtures" / "reducto" / "parse.json").read_text())
# The fixture bills 1.0 credit; ReductoAdapter.report_cost prices a credit at $0.015.
_EXPECTED_USD = 0.015
_REDUCTO_BROKER = EnvCredentialBroker({"REDUCTO_API_KEY": "test-key"})
_EMPTY_BROKER = EnvCredentialBroker({})


class FakeReductoClient:
    """Replays the captured parse fixture — a paid backend with no network."""

    def parse(self, document, options, is_async):
        return _PARSE


def _paid_adapter() -> ReductoAdapter:
    return ReductoAdapter(client=FakeReductoClient())


def _req(backend: str = None) -> OpenReadingRequest:
    return OpenReadingRequest.model_validate(
        {
            "document": {"url": "https://example.test/doc.pdf", "mime_type": "application/pdf"},
            "backend": {"id": backend},
        }
    )


def _blank_response() -> NormalizedResponse:
    return NormalizedResponse(
        status=Status(state=ResponseState.SUCCEEDED),
        backend=BackendInfo(id="x", type=BackendType.HOSTED_API),
        document=Document(text="t"),
    )


def _mock_reducto_http() -> None:
    respx.post("https://platform.reducto.ai/upload").mock(
        return_value=httpx.Response(200, json={"file_id": "reducto://abc.pdf"})
    )
    respx.post("https://platform.reducto.ai/parse").mock(
        return_value=httpx.Response(200, json=_PARSE)
    )


# --- the merge rule ---------------------------------------------------------------------


def test_merge_projects_every_mappable_field():
    resp = _blank_response()
    merge_cost_report(
        resp,
        CostReport(
            native_unit="page",
            native_quantity=3.0,
            cost_usd=0.42,
            basis=CostBasis.BILLED,
            billing_target="caller_account",
            duration_ms=1234,
        ),
    )
    assert resp.usage is not None
    assert resp.usage.cost_usd == 0.42
    assert resp.usage.cost_basis == "billed"
    assert resp.usage.duration_ms == 1234
    assert resp.usage.pages_processed == 3


def test_merge_fills_only_unset_fields():
    resp = _blank_response()
    resp.usage = Usage(cost_usd=9.99, pages_processed=7)
    merge_cost_report(
        resp,
        CostReport(
            native_unit="page",
            native_quantity=3.0,
            cost_usd=0.42,
            basis=CostBasis.BILLED,
            billing_target="caller_account",
        ),
    )
    assert resp.usage.cost_usd == 9.99  # what normalize() reported wins
    assert resp.usage.pages_processed == 7
    assert resp.usage.cost_basis == "billed"  # ...but an unset field is filled


def test_merge_records_infra_only_basis_without_inventing_a_price():
    resp = _blank_response()
    merge_cost_report(resp, infra_only("page", 2.0))
    assert resp.usage is not None
    assert resp.usage.cost_usd is None  # a local backend has no per-call price
    assert resp.usage.cost_basis == "infra_only"  # and says WHY the price is absent
    assert resp.usage.pages_processed == 2


def test_merge_never_rounds_a_fractional_page_count_into_pages_processed():
    resp = _blank_response()
    merge_cost_report(resp, infra_only("page", 2.5))
    assert resp.usage is not None and resp.usage.pages_processed is None


def test_merge_does_not_split_a_combined_token_count():
    resp = _blank_response()
    merge_cost_report(
        resp,
        CostReport(
            native_unit="token",
            native_quantity=900.0,
            cost_usd=0.01,
            basis=CostBasis.ESTIMATED,
            billing_target="caller_account",
        ),
    )
    assert resp.usage is not None
    assert resp.usage.input_tokens is None and resp.usage.output_tokens is None
    assert resp.usage.cost_usd == 0.01


def test_merge_projects_credits():
    resp = _blank_response()
    merge_cost_report(
        resp,
        CostReport(
            native_unit="credit",
            native_quantity=4.0,
            cost_usd=0.06,
            basis=CostBasis.BILLED,
            billing_target="caller_account",
        ),
    )
    assert resp.usage is not None and resp.usage.credits == 4.0


# --- choke point 1: the chain executor --------------------------------------------------


def test_executor_fills_cost_usd_from_report_cost():
    adapter = _paid_adapter()
    resp = execute_plan(RoutePlan(chosen=adapter), _req(), broker=_REDUCTO_BROKER)

    metered = adapter.report_cost(adapter.submit(_req(), RunContext()))
    assert resp.usage is not None
    assert resp.usage.cost_usd == metered.cost_usd == _EXPECTED_USD
    assert resp.usage.cost_basis == "billed"
    assert resp.usage.credits == 1.0
    schemas.validate_response(resp.to_schema_dict())


def test_executor_cost_survives_the_idempotency_cache():
    from openreading.router.executor import BoundedResultCache

    # the cache keys on document CONTENT identity (D-v3-3, BL-23) — a url-sourced _req() has none,
    # so this needs a bytes-carrying request to actually exercise a cache hit.
    req = OpenReadingRequest.model_validate(
        {
            "document": {
                "bytes_base64": base64.b64encode(build_sample_pdf()).decode(),
                "mime_type": "application/pdf",
            },
            "backend": {"id": None},
        }
    )
    cache = BoundedResultCache()
    plan = RoutePlan(chosen=_paid_adapter())
    execute_plan(plan, req, broker=_REDUCTO_BROKER, cache=cache, clock=FakeClock())
    replay = execute_plan(plan, req, broker=_REDUCTO_BROKER, cache=cache, clock=FakeClock())

    assert any(w.code == "idempotent_replay" for w in replay.warnings or [])
    assert replay.usage is not None and replay.usage.cost_usd == _EXPECTED_USD


class _RaisingMeter(ConfigurableBackend):
    def __init__(self) -> None:
        super().__init__(make_backend("broken-meter", local=True).descriptor)

    def report_cost(self, job):
        raise RuntimeError("meter exploded")


def test_a_raising_report_cost_degrades_instead_of_failing_the_run():
    resp = execute_plan(RoutePlan(chosen=_RaisingMeter()), _req(), broker=_EMPTY_BROKER)

    assert resp.status.state is ResponseState.SUCCEEDED
    assert resp.document.text  # the successful result is untouched
    assert resp.usage is None  # nothing to merge → absent, not fabricated
    assert any(w.code == "cost_unavailable" for w in resp.warnings or [])
    schemas.validate_response(resp.to_schema_dict())


# --- choke point 2: the strategy engine leaf --------------------------------------------


def test_strategy_engine_leaf_fills_cost_usd_from_report_cost():
    registry = Registry()
    registry.register(_paid_adapter())
    cfg = StrategyConfig.model_validate(
        {"version": 1, "strategies": {"s": {"steps": [{"backend": "reducto"}]}}}
    )
    req = _req("strategy:s")
    compiled = compile_strategy(req, "s", cfg, registry, RouterConfig())
    resp = run_strategy(
        compiled,
        req,
        registry=registry,
        broker=_REDUCTO_BROKER,
        clock=FakeClock(),
    ).response

    assert resp.usage is not None and resp.usage.cost_usd == _EXPECTED_USD
    assert resp.usage.cost_basis == "billed"
    schemas.validate_response(resp.to_schema_dict())


# --- choke point 3: the directly-named backend run --------------------------------------


@respx.mock
def test_named_backend_run_reports_cost(monkeypatch, tmp_path):
    monkeypatch.setenv("REDUCTO_API_KEY", "test-key")
    _mock_reducto_http()
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(build_sample_pdf())

    out = api.run(str(pdf), backend="reducto")

    schemas.validate_response(out)
    assert out["usage"]["cost_usd"] == _EXPECTED_USD
    assert out["usage"]["cost_basis"] == "billed"


@respx.mock
def test_batch_summary_cost_is_non_null_for_a_paid_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("REDUCTO_API_KEY", "test-key")
    _mock_reducto_http()
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for name in ("a.pdf", "b.pdf"):
        (corpus / name).write_bytes(build_sample_pdf())

    env = run_batch([str(corpus)], backend="reducto")

    schemas.validate_batch_result(env)
    assert env["summary"]["succeeded"] == 2
    assert env["summary"]["cost_usd"] == pytest.approx(2 * _EXPECTED_USD)
    assert env["summary"]["cost_bases"] == ["billed"]


# --- BL-108: a mechanical backstop against a future uncredentialed call site ----------------


def test_every_apply_cost_report_call_site_passes_credentials():
    # apply_cost_report's fourth `credentials` argument is the ONLY way its own redaction (BL-93)
    # can ever reach a report_cost() failure's warning message: report_cost() failures are caught
    # inside apply_cost_report itself and never re-raised, so no exception-based wrapper (not
    # auth_hinted, not any caller's own try/except) can protect this call after the fact. BL-108
    # happened because one call site (AnthropicClaudeAdapter.normalize_many, added by BL-100) was
    # written with only three arguments — syntactically valid, since `credentials` is optional for
    # backward compatibility, and silent on the happy path since a meter failure is rare. Rather
    # than wait a sprint for a by-hand sweep to notice a sixth such omission (e.g. a future
    # native-batch adapter's own normalize_many), walk every apply_cost_report(...) call site in
    # src/openreading/ via the AST and require at least 4 arguments (positional + keyword
    # combined) at every one.
    import ast
    from pathlib import Path

    src_root = Path(__file__).resolve().parent.parent / "src" / "openreading"
    offenders = []
    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (
                fn.id
                if isinstance(fn, ast.Name)
                else fn.attr
                if isinstance(fn, ast.Attribute)
                else None
            )
            if name != "apply_cost_report":
                continue
            total_args = len(node.args) + len(node.keywords)
            if total_args < 4:
                offenders.append(f"{path.relative_to(src_root)}:{node.lineno}")

    assert offenders == [], (
        "apply_cost_report call(s) with no `credentials` argument (report_cost() failures at "
        f"these sites can never be redacted): {offenders}"
    )
