"""Stage-1 data-residency matching (routing_and_compliance.md §4.4).

Region codes are opaque identifiers, not a hierarchy: `eastus2` is a distinct Azure region from
`eastus`, and `us` is not a parent of `us-east-1`. Matching them by string prefix let one region
code silently satisfy a request for a different one, admitting an undeclared region into a
data-residency filter — so the match is exact (case-insensitive) and nothing else.
"""

from __future__ import annotations

import pytest

from openreading.adapters.registry import make_adapter
from openreading.router.compliance import _region_covered, evaluate, parse_retention_hours
from openreading.types.request import Compliance
from tests.fakes import make_backend

# the shipped azure-document-intelligence region list — the descriptor the bug was found against
_AZURE = ["eastus", "westus", "westeurope", "northeurope", "us", "eu"]


@pytest.mark.parametrize(
    "want, regions",
    [
        ("eu", ["us", "eu"]),
        ("eastus", _AZURE),
        ("europe-west2", ["us", "eu", "europe-west2"]),
        ("EU", ["us", "eu"]),  # request casing normalized
        ("eu", ["US", "EU"]),  # descriptor casing normalized
    ],
)
def test_exact_region_is_covered(want: str, regions: list[str]) -> None:
    assert _region_covered(want, regions)


@pytest.mark.parametrize(
    "want, regions",
    [
        ("eastus2", _AZURE),  # a real, distinct, UNDECLARED Azure region
        ("eastus2", ["eastus"]),
        ("eastus", ["eastus2"]),  # reverse direction
        ("us", ["us-east-1", "us-west-2"]),  # 'us' is not a parent of an AWS region code
        ("eu", ["europe-west2"]),
        ("eu", []),
    ],
)
def test_distinct_region_codes_never_cover_each_other(want: str, regions: list[str]) -> None:
    assert not _region_covered(want, regions)


def test_evaluate_drops_a_neighbouring_undeclared_region() -> None:
    desc = make_backend("azure-like", hipaa_baa="yes", regions=_AZURE).descriptor
    drop = evaluate(Compliance(data_region="eastus2"), desc)
    assert drop is not None
    assert (drop.stage, drop.code) == (1, "region_mismatch")
    assert evaluate(Compliance(data_region="eastus"), desc) is None


@pytest.mark.parametrize("value", ["zero", "zdr", "none", "0", "0h"])
def test_parse_retention_hours_zero_shorthand(value: str) -> None:
    """'zdr' is the module's own docstring example for the literal-shorthand form; every
    existing max_retention test only drives the numeric-with-optional-'h' branch instead."""
    assert parse_retention_hours(value) == 0.0


def test_a_service_backend_pointed_off_box_is_not_trusted_as_local(monkeypatch) -> None:
    """docling declares runs_fully_local=True as a static architectural claim, but the operator
    decides where DOCLING_SERVE_URL actually points. A remote host must drop the same way a
    hosted vendor would, not skip every stage-1 check the way a genuinely local backend does."""
    monkeypatch.setenv("DOCLING_SERVE_URL", "http://docling.example.internal:5001")
    desc = make_adapter("docling").descriptor
    drop = evaluate(Compliance(require_local=True), desc)
    assert drop is not None
    assert (drop.stage, drop.code) == (1, "not_local")


@pytest.mark.parametrize(
    "url", ["http://localhost:5001", "http://127.0.0.1:5001", "http://[::1]:5001"]
)
def test_a_service_backend_pointed_at_loopback_is_still_trusted_as_local(monkeypatch, url) -> None:
    monkeypatch.setenv("DOCLING_SERVE_URL", url)
    desc = make_adapter("docling").descriptor
    assert evaluate(Compliance(require_local=True), desc) is None


def test_an_unconfigured_service_backend_is_still_trusted_as_local(monkeypatch) -> None:
    """No endpoint set yet is not the same fact as a bad one; nothing has proven this points off
    box, so the static claim stands until an operator actually points it somewhere."""
    monkeypatch.delenv("DOCLING_SERVE_URL", raising=False)
    desc = make_adapter("docling").descriptor
    assert evaluate(Compliance(require_local=True), desc) is None


def test_a_service_backend_off_box_also_loses_the_baa_free_path(monkeypatch) -> None:
    """A backend wrongly trusted as local is treated as the BAA-free PHI path everywhere in this
    module, not only at require_local — docling's hipaa_baa is na_local (a fact that presumes the
    document never left the operator's own infrastructure), so once that presumption is false,
    require_baa must fall through to the same 'no BAA in force' drop a hosted vendor would get."""
    monkeypatch.setenv("DOCLING_SERVE_URL", "http://docling.example.internal:5001")
    desc = make_adapter("docling").descriptor
    drop = evaluate(Compliance(require_baa=True), desc)
    assert drop is not None
    assert (drop.stage, drop.code) == (1, "no_baa")


def test_an_in_process_library_has_no_endpoint_to_misconfigure(monkeypatch) -> None:
    """pymupdf declares no config_spec endpoint field at all, so nothing in the environment can
    make the static runs_fully_local claim dishonest for it."""
    monkeypatch.setenv("DOCLING_SERVE_URL", "http://docling.example.internal:5001")  # unrelated
    desc = make_adapter("pymupdf").descriptor
    assert evaluate(Compliance(require_local=True), desc) is None


@pytest.mark.parametrize(
    "constraint,code", [("require_local", "not_local"), ("require_baa", "no_baa")]
)
def test_remote_alias_cannot_use_local_compliance_exemption(monkeypatch, constraint, code):
    """An approved credential alias selects configuration as well as credentials."""
    from openreading.credentials import build_run_context
    from openreading.router import Registry, Router
    from openreading.types.errors import ComplianceRefused
    from openreading.types.request import OpenReadingRequest

    monkeypatch.setenv("OPENREADING_CREDENTIALS_REF_ALIASES", "REMOTE")
    monkeypatch.setenv("REMOTE_ENDPOINT", "https://offbox.example")
    monkeypatch.setenv("DOCLING_SERVE_URL", "http://localhost:5001")
    adapter = make_adapter("docling")
    req = OpenReadingRequest.model_validate(
        {
            "document": {"bytes_base64": "eA=="},
            "backend": {"id": "docling", "credentials_ref": "env:REMOTE"},
            "compliance": {constraint: True},
        }
    )
    assert (
        build_run_context(req, adapter.descriptor).runtime["endpoint"] == "https://offbox.example"
    )
    registry = Registry()
    registry.register(adapter)
    router = Router(registry)
    plan = router.route(req)
    assert plan.chosen is None
    assert plan.dropped["docling"].code == code
    with pytest.raises(ComplianceRefused):
        router.check_eligible(req, "docling")


@pytest.mark.parametrize(
    "endpoint,refused", [("https://offbox.example", True), ("http://localhost:5001", False)]
)
def test_strategy_compliance_uses_injected_broker(monkeypatch, endpoint, refused):
    """Compilation checks the endpoint the execution broker selects for an approved alias."""
    from openreading.credentials import EnvCredentialBroker
    from openreading.router import Registry
    from openreading.strategies.model import StrategyConfig
    from openreading.strategies.prune import compile_strategy
    from openreading.types.errors import ComplianceRefused
    from openreading.types.request import OpenReadingRequest

    monkeypatch.setenv("DOCLING_SERVE_URL", "http://localhost:5001")
    broker = EnvCredentialBroker(
        {
            "OPENREADING_CREDENTIALS_REF_ALIASES": "REMOTE",
            "REMOTE_ENDPOINT": endpoint,
        }
    )
    req = OpenReadingRequest.model_validate(
        {
            "document": {"bytes_base64": "eA=="},
            "backend": {"id": "strategy:demo", "credentials_ref": "env:REMOTE"},
            "compliance": {"require_local": True},
        }
    )
    registry = Registry()
    registry.register(make_adapter("docling"))
    config = StrategyConfig.model_validate(
        {"version": 1, "strategies": {"demo": {"backend": "docling"}}}
    )
    if refused:
        with pytest.raises(ComplianceRefused):
            compile_strategy(req, "demo", config, registry, broker=broker)
    else:
        assert compile_strategy(req, "demo", config, registry, broker=broker).eligible == [
            "docling"
        ]


@pytest.mark.parametrize("backend", ["docling", "auto", "strategy:demo"])
def test_execution_refuses_remote_alias_before_dispatch(monkeypatch, backend):
    from openreading import api
    from openreading.credentials import EnvCredentialBroker
    from openreading.router import Registry
    from openreading.strategies.model import StrategyConfig
    from openreading.types.errors import ComplianceRefused
    from openreading.types.request import OpenReadingRequest

    broker = EnvCredentialBroker(
        {
            "OPENREADING_CREDENTIALS_REF_ALIASES": "REMOTE",
            "REMOTE_ENDPOINT": "https://offbox.example",
        }
    )
    monkeypatch.setenv("DOCLING_SERVE_URL", "http://localhost:5001")
    registry = Registry()
    registry.register(make_adapter("docling"))
    monkeypatch.setattr(api, "build_registry", lambda: registry)
    req = OpenReadingRequest.model_validate(
        {
            "document": {"bytes_base64": "eA=="},
            "backend": {"id": backend, "credentials_ref": "env:REMOTE"},
            "compliance": {"require_local": True},
        }
    )
    config = StrategyConfig.model_validate(
        {"version": 1, "strategies": {"demo": {"backend": "docling"}}}
    )
    with pytest.raises(ComplianceRefused):
        api.run_request(req, broker=broker, strategy_config=config)
