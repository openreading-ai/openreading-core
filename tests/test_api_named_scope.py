"""The shared execution boundary refuses named backends before resolving credentials."""

import pytest

from openreading.api import build_request, run_request
from openreading.credentials import EnvCredentialBroker
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.errors import ScopeRefused
from openreading.types.request import OpenReadingRequest


class NoCredentialAccess(EnvCredentialBroker):
    def resolve(self, descriptor, request):
        raise AssertionError("A denied backend reached credential resolution")


@pytest.mark.parametrize("backend", ["pymupdf", "unregistered-backend"])
@pytest.mark.parametrize("allowed", [frozenset(), frozenset({"docling_local"})])
def test_named_backend_is_denied_before_lookup_credentials_or_execution(backend, allowed):
    request = OpenReadingRequest.model_validate(
        {"document": {"bytes_base64": "bm90"}, "backend": {"id": backend}}
    )
    with pytest.raises(ScopeRefused) as failure:
        run_request(request, broker=NoCredentialAccess(), backend_allowlist=allowed)
    assert failure.value.backend_code == backend
    assert failure.value.constraint == "backend_allowlist"


@pytest.mark.parametrize("allowed", [None, frozenset({"pymupdf"})])
def test_allowed_or_unscoped_named_backend_still_executes(allowed):
    request = build_request(build_sample_pdf(), "pymupdf")
    result = run_request(request, backend_allowlist=allowed)
    assert result["backend"]["id"] == "pymupdf"
    assert result["document"]["pages"]


@pytest.mark.parametrize("call", ["run", "run_batch"])
@pytest.mark.parametrize("backend", ["reducto", "not-registered"])
def test_public_named_scope_precedes_input_config_and_credential_access(monkeypatch, call, backend):
    from openreading import api
    from openreading.batch import sources

    def forbidden(*args, **kwargs):
        raise AssertionError("Denied public call accessed setup or input")

    monkeypatch.setattr(api, "make_adapter", forbidden)
    monkeypatch.setattr(api, "load_config_file", forbidden)
    monkeypatch.setattr(api, "load_dotenv", forbidden)
    monkeypatch.setattr(sources, "resolve_intake", forbidden)
    source = ["missing.pdf"] if call == "run_batch" else "missing.pdf"
    with pytest.raises(ScopeRefused) as error:
        getattr(api, call)(
            source, backend=backend, env_file="secret.env", backend_allowlist=frozenset()
        )
    assert error.value.backend_code == backend
    assert error.value.constraint == "backend_allowlist"


@pytest.mark.parametrize("backend", ["pymupdf", None, "strategy:local", "strategy:none"])
@pytest.mark.parametrize("allowed", [None, frozenset({"pymupdf"})])
def test_public_single_scope_preserves_permitted_paths(backend, allowed, monkeypatch):
    from openreading import api, schemas

    monkeypatch.delenv("OPENREADING_LEDGER", raising=False)
    config = {
        "version": 1,
        "policy": {"backends": ["pymupdf"]},
        "strategies": {"local": {"backend": "pymupdf"}},
    }
    result = api.run(build_sample_pdf(), backend=backend, config=config, backend_allowlist=allowed)
    schemas.validate_response(result)
    assert result["backend"]["id"] == "pymupdf"
    assert "OpenReading Test Document" in result["document"]["text"]


@pytest.mark.parametrize("backend", [None, "strategy:local", "strategy:none"])
def test_public_single_empty_scope_refuses_routed_and_strategy_execution(backend, monkeypatch):
    from openreading import api

    monkeypatch.delenv("OPENREADING_LEDGER", raising=False)
    config = {"version": 1, "strategies": {"local": {"backend": "pymupdf"}}}
    with pytest.raises(ScopeRefused):
        api.run(
            build_sample_pdf(),
            backend=backend,
            config=config,
            broker=NoCredentialAccess(),
            backend_allowlist=frozenset(),
        )


@pytest.mark.parametrize("strategy", [None, "local"])
def test_public_batch_propagates_scope_to_each_platform_item(tmp_path, monkeypatch, strategy):
    from openreading import api, schemas

    monkeypatch.delenv("OPENREADING_LEDGER", raising=False)
    paths = []
    for name in ("a.pdf", "b.pdf"):
        path = tmp_path / name
        path.write_bytes(build_sample_pdf())
        paths.append(str(path))
    config = {"version": 1, "strategies": {"local": {"backend": "pymupdf"}}}
    accepted = api.run_batch(
        paths, strategy=strategy, config=config, jobs=2, backend_allowlist=frozenset({"pymupdf"})
    )
    schemas.validate_batch_result(accepted)
    assert accepted["summary"]["succeeded"] == 2
    assert all(item["response"]["backend"]["id"] == "pymupdf" for item in accepted["items"])
    denied = api.run_batch(
        paths,
        strategy=strategy,
        config=config,
        jobs=2,
        broker=NoCredentialAccess(),
        backend_allowlist=frozenset(),
    )
    schemas.validate_batch_result(denied)
    assert denied["summary"]["failed"] == 2
    # The existing batch contract retains ScopeRefused.backend_code, not an HTTP category.
    assert all(item["error"]["code"] == "pymupdf" for item in denied["items"])
    assert all("scoped" in item["error"]["message"] for item in denied["items"])
