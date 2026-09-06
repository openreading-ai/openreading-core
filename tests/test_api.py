"""Public one-call API + URL materialization (GOAL2 7.3). All offline: local backends run for
real; URL download uses an injected httpx MockTransport, so no default test performs network I/O."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

import openreading
from openreading import api
from openreading.adapters.registry import make_adapter
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types.errors import TerminalError

pytest.importorskip("fitz", reason="pymupdf not installed")


@pytest.fixture
def pdf_path(tmp_path):
    p = tmp_path / "doc.pdf"
    p.write_bytes(build_sample_pdf())
    return str(p)


def _policy(policy: dict) -> dict:
    """A policy the way a caller with no file on disk writes one: the file's own shape, inline."""
    return {"version": 1, "policy": policy}


def _policy_file(tmp_path, policy: dict) -> str:
    body = ", ".join(f"{k}: {json.dumps(v)}" for k, v in policy.items())
    p = tmp_path / "openreading.yaml"
    p.write_text(f"version: 1\npolicy: {{{body}}}\n")
    return str(p)


# --- config= is the one container (the file's shape, from a path or from memory) -----------------


def test_run_takes_the_same_policy_from_a_dict_and_from_a_file(pdf_path, tmp_path):
    """A dict passed to `config=` is the file, held in memory. Same shape, same validation, same
    result — otherwise a caller with no file on disk has a second grammar to learn."""
    policy = {"require_local": True}
    from_dict = openreading.run(pdf_path, backend="auto", config=_policy(policy))
    from_file = openreading.run(pdf_path, backend="auto", config=_policy_file(tmp_path, policy))
    assert from_dict["backend"]["id"] == from_file["backend"]["id"]
    assert from_dict["document"]["text"] == from_file["document"]["text"]


def test_route_takes_the_same_policy_from_a_dict_and_from_a_file(pdf_path, tmp_path):
    policy = {"require_local": True}
    from_dict = openreading.route(pdf_path, config=_policy(policy))
    from_file = openreading.route(pdf_path, config=_policy_file(tmp_path, policy))
    assert from_dict.chosen is not None and from_file.chosen is not None
    assert from_dict.chosen.descriptor.id == from_file.chosen.descriptor.id
    assert sorted(from_dict.dropped) == sorted(from_file.dropped)


def test_run_batch_takes_the_same_policy_from_a_dict_and_from_a_file(pdf_path, tmp_path):
    policy = {"require_local": True}
    from_dict = openreading.run_batch([pdf_path], backend="auto", config=_policy(policy))
    from_file = openreading.run_batch(
        [pdf_path], backend="auto", config=_policy_file(tmp_path, policy)
    )
    keys = ("total", "succeeded", "failed", "skipped")
    assert {k: from_dict["summary"][k] for k in keys} == {k: from_file["summary"][k] for k in keys}
    assert [i.get("response", {}).get("backend") for i in from_dict["items"]] == [
        i.get("response", {}).get("backend") for i in from_file["items"]
    ]


def test_a_dict_that_is_not_a_policy_is_refused_from_python_too(pdf_path):
    """A shape refused from a file is refused from Python, and the message says which it was."""
    from openreading.config import ConfigError

    with pytest.raises(ConfigError) as exc:
        openreading.run(pdf_path, backend="pymupdf", config=_policy({"require_locall": True}))
    assert "<dict>" in str(exc.value)
    assert "require_locall" in str(exc.value)


@pytest.mark.parametrize("call", ["run", "route", "run_batch"])
def test_the_policy_keyword_is_gone(pdf_path, call):
    """One container, not two. `policy=` was the second spelling and it is removed outright, so a
    caller who passes it learns that from Python rather than from a silently ignored constraint."""
    fn = getattr(openreading, call)
    source = [pdf_path] if call == "run_batch" else pdf_path
    with pytest.raises(TypeError, match="policy"):
        fn(source, policy={"require_local": True})


def test_run_named_local_backend_returns_schema_dict(pdf_path):
    result = openreading.run(pdf_path, backend="pymupdf")
    from openreading import schemas

    schemas.validate_response(result)
    assert result["backend"]["id"] == "pymupdf"
    assert "OpenReading Test Document" in result["document"]["text"]


def test_run_from_bytes(pdf_path):
    data = build_sample_pdf()
    result = openreading.run(data, backend="pymupdf")
    assert result["backend"]["id"] == "pymupdf"


def test_run_auto_routes_and_executes_local(pdf_path):
    # require_local → the router picks a local backend and the executor runs it
    result = openreading.run(pdf_path, backend="auto", config=_policy({"require_local": True}))
    assert make_adapter(result["backend"]["id"]).descriptor.compliance.runs_fully_local


def test_run_named_missing_credentials_raises_naming_vars(pdf_path, monkeypatch):
    for v in ("GCP_PROJECT_ID", "GOOGLE_CLOUD_PROJECT", "GCP_PROCESSOR_ID"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(TerminalError) as exc:
        openreading.run(pdf_path, backend="google-document-ai")
    assert exc.value.backend_code == "missing_credentials"
    assert "GCP_PROJECT_ID" in str(exc.value) and "cloud.google.com/document-ai" in str(exc.value)


def test_route_returns_plan(pdf_path):
    plan = openreading.route(pdf_path, config=_policy({"require_local": True}))
    assert plan.chosen is not None
    assert plan.chosen.descriptor.compliance.runs_fully_local


# NB: with the real registry the local tier is a guaranteed compliance floor, so a route plan is
# never empty for any policy — the PlanExhaustedError-on-empty-plan branch is defensive. The
# executor's exhaustion path (all backends fail at run time) is covered in test_executor.py.


# --- URL materialization (D-v2-13) -----------------------------------------------------


def _mock_transport(payload: bytes):
    return httpx.MockTransport(lambda request: httpx.Response(200, content=payload))


def _url_req(url="https://example.test/doc.pdf"):
    return api.build_request(url, "pymupdf")


@pytest.fixture
def resolves_public(monkeypatch):
    """`_assert_public_http_url` (H2) resolves the URL host for real before any transport runs.
    `example.test` is a reserved, deliberately non-resolving TLD (RFC 2606), so the offline
    URL-materialization tests below — which prove behavior through an injected transport, never a
    live network call — need a stand-in for "the host resolves to a public address" that never
    touches a resolver."""
    monkeypatch.setattr(
        "socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 80))]
    )


def test_url_materialized_to_bytes_for_non_url_backend(resolves_public):
    req = _url_req()
    assert req.document.url and req.document.bytes_base64 is None
    out = api.materialize_document(
        req, make_adapter("pymupdf").descriptor, transport=_mock_transport(b"%PDF-1.7 x")
    )
    assert out.document.url is None
    assert base64.b64decode(out.document.bytes_base64) == b"%PDF-1.7 x"


def test_url_passed_through_for_url_accepting_backend():
    url = "https://example.test/doc.pdf"
    req = api.build_request(url, "reducto")
    out = api.materialize_document(
        req, make_adapter("reducto").descriptor, transport=_mock_transport(b"should-not-fetch")
    )
    assert out.document.url == url and out.document.bytes_base64 is None  # untouched


def test_materialize_download_error_maps_to_terminal(resolves_public):
    req = _url_req()
    transport = httpx.MockTransport(lambda request: httpx.Response(404, text="nope"))
    with pytest.raises(TerminalError):
        api.materialize_document(req, make_adapter("pymupdf").descriptor, transport=transport)


def test_run_from_url_executes_local_backend(resolves_public):
    # end-to-end: URL → materialize (mock transport) → pymupdf runs for real
    url = "https://example.test/doc.pdf"
    transport = _mock_transport(build_sample_pdf())
    result = openreading.run(url, backend="pymupdf", transport=transport)
    assert result["backend"]["id"] == "pymupdf"
    assert "OpenReading Test Document" in result["document"]["text"]


# --- SSRF guard + streaming size cap for URL documents (H2) -----------------------------


def test_download_rejects_non_public_host(monkeypatch):
    monkeypatch.delenv("OPENREADING_ALLOW_PRIVATE_URLS", raising=False)
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("169.254.169.254", 80))],
    )
    # A transport, even though the guard is expected to reject before ever using it: without one,
    # a pre-fix/broken guard would fall through to a REAL connect against the address above, and
    # this test's own failure mode would then depend on this machine's network reachability
    # (fast local ConnectError, a long hang, or — on infra where that address is actually routed —
    # a real metadata-endpoint request) instead of on `_download`'s own behavior.
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"unreachable"))
    with pytest.raises(TerminalError) as e:
        api._download("http://metadata.internal/doc.pdf", transport=transport)
    assert e.value.backend_code == "url_not_public"


def test_download_rejects_file_scheme():
    with pytest.raises(TerminalError):
        api._download("file:///etc/hosts")


def test_download_streams_and_stops_at_limit(monkeypatch):
    # `content=` makes httpx auto-set Content-Length, so this always resolves at _download's
    # declared-length precheck and never reaches the per-chunk iter_bytes loop below it — see
    # test_download_streams_and_stops_at_limit_with_no_declared_length for that path.
    monkeypatch.setattr("openreading.api._MAX_DOWNLOAD_BYTES", 1024)
    monkeypatch.setattr(
        "socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 80))]
    )
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=b"x" * 4096))
    with pytest.raises(TerminalError) as e:
        api._download("http://example.com/big.pdf", transport=transport)
    assert e.value.backend_code == "doc_too_large"


class _UndeclaredLengthBody(httpx.SyncByteStream):
    """A response body with no Content-Length at all — the only way to reach `_download`'s
    per-chunk running-total loop instead of short-circuiting at its declared-length precheck.
    Two chunks, each under the cap on its own, so only the RUNNING total (not any single
    chunk's size) can be what trips the limit."""

    def __iter__(self):
        yield b"x" * 700
        yield b"x" * 700


def test_download_streams_and_stops_at_limit_with_no_declared_length(monkeypatch):
    monkeypatch.setattr("openreading.api._MAX_DOWNLOAD_BYTES", 1024)
    monkeypatch.setattr(
        "socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 80))]
    )

    def handler(request):
        response = httpx.Response(200, stream=_UndeclaredLengthBody())
        # Confirms the precheck this test exists to bypass truly has nothing to short-circuit on.
        assert "content-length" not in response.headers
        return response

    transport = httpx.MockTransport(handler)
    with pytest.raises(TerminalError) as e:
        api._download("http://example.com/big.pdf", transport=transport)
    assert e.value.backend_code == "doc_too_large"


def test_download_connects_to_the_vetted_address_not_a_second_resolution(monkeypatch):
    """DNS rebinding: the guard resolved the host and then httpx resolved it AGAIN at connect, so
    a name whose answer changed between the two reached an address the guard never approved —
    a cloud metadata endpoint among them. The connect is now pinned to the exact address the guard
    vetted, with the original host carried in the Host header and in SNI so virtual hosting and
    certificate verification still work."""
    monkeypatch.delenv("OPENREADING_ALLOW_PRIVATE_URLS", raising=False)
    monkeypatch.setattr(
        "socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))]
    )
    seen: dict[str, object] = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["host"] = request.headers.get("host")
        seen["sni"] = request.extensions.get("sni_hostname")
        return httpx.Response(200, content=b"ok")

    out = api._download("https://example.test/doc.pdf", transport=httpx.MockTransport(handler))

    assert out == b"ok"
    assert "93.184.216.34" in str(seen["url"])
    assert "example.test" not in str(seen["url"])
    assert seen["host"] == "example.test"
    assert seen["sni"] == "example.test"


def test_download_refuses_a_redirect_rather_than_returning_an_empty_document(monkeypatch):
    """Redirects are off (httpx's default), so a 3xx used to fall through the `>= 400` check and
    be read as a successful zero-byte document. A redirect is also the classic way to walk a
    vetted address to an unvetted one, so it is refused outright rather than followed."""
    monkeypatch.delenv("OPENREADING_ALLOW_PRIVATE_URLS", raising=False)
    monkeypatch.setattr(
        "socket.getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 80))]
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(302, headers={"location": "http://169.254.169.254/"})
    )
    with pytest.raises(TerminalError) as e:
        api._download("http://example.test/doc.pdf", transport=transport)
    assert e.value.backend_code == "url_not_public"


def test_download_allows_private_when_opted_in(monkeypatch):
    monkeypatch.setenv("OPENREADING_ALLOW_PRIVATE_URLS", "1")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"ok"))
    assert api._download("http://127.0.0.1:9/x.pdf", transport=transport) == b"ok"


def test_mime_inference_by_extension(tmp_path):
    ooxml = "application/vnd.openxmlformats-officedocument"
    for ext, expected in (
        (".png", "image/png"),
        (".pdf", "application/pdf"),
        (".xyz", "application/pdf"),
        (".docx", f"{ooxml}.wordprocessingml.document"),
        (".xlsx", f"{ooxml}.spreadsheetml.sheet"),
        (".pptx", f"{ooxml}.presentationml.presentation"),
    ):
        f = tmp_path / f"doc{ext}"
        f.write_bytes(b"x")
        assert api._document_dict(str(f), None)["mime_type"] == expected  # inferred from extension
    assert api._document_dict(b"raw", None)["mime_type"] == "application/pdf"  # bytes default
    assert (
        api._document_dict(b"raw", "image/tiff")["mime_type"] == "image/tiff"
    )  # explicit override


def test_build_request_url_keeps_mime_type():
    # L1: _document_dict's URL branch used to return {"url": s}, dropping the caller's explicit
    # mime_type entirely -- materialize_document's fallback then mis-typed every URL document as
    # application/pdf regardless of what the caller passed.
    req = api.build_request("https://example.com/scan.png", "auto", mime_type="image/png")
    assert req.document.mime_type == "image/png"


@pytest.mark.parametrize("ext", [".docx", ".xlsx", ".pptx"])
def test_route_office_document_reaches_a_backend(tmp_path, ext):
    # the reported break: through the convenience path an Office file's OOXML MIME derived the
    # format token "document"/"sheet"/"presentation" and every backend was dropped.
    doc = tmp_path / f"doc{ext}"
    doc.write_bytes(b"PK\x03\x04")
    plan = openreading.route(str(doc))
    assert plan.chosen is not None, f"{ext} dropped every backend: {plan.dropped}"
    declared = {f.split()[0].lower() for f in plan.chosen.descriptor.capabilities.input_formats}
    assert ext.lstrip(".") in declared


def test_run_named_backend_respects_compliance(pdf_path, monkeypatch):
    # a directly-named backend that violates the request's compliance is refused, not run.
    from openreading.types.errors import ComplianceRefused

    with pytest.raises(ComplianceRefused):
        openreading.run(pdf_path, backend="reducto", config=_policy({"require_local": True}))


def test_build_request_rejects_a_document_override(pdf_path):
    # BL-105: document/backend are derived from source=/backend=, the named parameters -- never
    # from the passthrough overrides bag. Before this guard, the override loop's ordinary
    # `body[k] = v` merge let a caller-supplied document= override silently win over the real
    # source, with no error of any kind -- the Python-API twin of the /v1/batch server bug this
    # same item fixes, reached from the one place shared by run(), platform fan-out, and native
    # dispatch.
    with pytest.raises(ValueError, match="document"):
        api.build_request(pdf_path, "pymupdf", document={"path": "/evil"})


def test_run_document_override_raises_never_silently_wins_over_source(pdf_path):
    # The identical guard, reached through run()'s own **request_overrides passthrough -- proving
    # the fix lands in the one place (build_request) shared by every caller, not just a direct
    # build_request() call.
    with pytest.raises(ValueError, match="document"):
        openreading.run(pdf_path, backend="pymupdf", document={"path": "/evil"})


def test_named_tier_gated_backend_needs_confirmation_and_warns(pdf_path, monkeypatch):
    # hipaa_baa='tier_gated' is a BAA on a higher plan, not one in force: require_baa refuses the
    # named backend until the operator confirms, and the run that follows says so out loud.
    from openreading.adapters.registry import BUILTIN_ADAPTERS
    from openreading.types.errors import ComplianceRefused
    from tests.fakes import make_backend

    monkeypatch.setitem(
        BUILTIN_ADAPTERS,
        "tiered",
        lambda: make_backend("tiered", hipaa_baa="tier_gated", trains="no", regions=["us"]),
    )
    with pytest.raises(ComplianceRefused) as exc:
        openreading.run(pdf_path, backend="tiered", config=_policy({"require_baa": True}))
    assert exc.value.constraint == "no_baa"

    result = openreading.run(
        pdf_path,
        backend="tiered",
        config=_policy({"require_baa": True, "baa_tier_confirmed": ["tiered"]}),
    )
    note = next(w for w in result["warnings"] if w["code"] == "baa_tier_confirmed")
    assert note["field"] == "tiered" and "tier_gated" in note["message"]


# --- deadline_ms propagation at the named-backend boundary (BL-153) --------------------


@pytest.mark.parametrize("deadline_ms", [1000, 0])
def test_prepare_named_backend_threads_deadline_ms_into_run_context(deadline_ms, monkeypatch):
    # ctx.deadline_ms is the field an adapter's own code actually reads (e.g.
    # TesseractAdapter.submit()'s subprocess timeout) — a caller's explicit deadline_ms (tight or
    # an explicit zero) must reach it at the prepare_named_backend boundary, the shared helper
    # behind both CLI `--backend` / `run_request`'s named-backend branch and the server's
    # `submit_job` handler — the single most direct way to dispatch a named adapter. Mirrors
    # test_executor.py's BL-146 regression test shape (`deadline_ms` in `(1000, 0)`, asserting
    # against the fake's recorded `.contexts[-1]`) for the third dispatch path BL-146 never
    # reached.
    from openreading.adapters.registry import BUILTIN_ADAPTERS
    from openreading.types.request import OpenReadingRequest
    from tests.fakes import ScriptedBackend

    fake = ScriptedBackend("scripted-deadline", local=True)
    monkeypatch.setitem(BUILTIN_ADAPTERS, "scripted-deadline", lambda: fake)
    req = OpenReadingRequest.model_validate(
        {"document": {"path": "/d.pdf"}, "backend": {"id": "scripted-deadline"}}
    )

    _adapter, _req, ctx = api.prepare_named_backend(
        req, "scripted-deadline", deadline_ms=deadline_ms
    )
    assert ctx.deadline_ms == deadline_ms


def test_prepare_named_backend_defaults_deadline_ms_when_not_supplied(monkeypatch):
    # BL-153 was pure plumbing; BL-169 wires `run()`'s own deadline_ms parameter (and `parse`'s
    # `--deadline` CLI flag) as a real caller into `run_request`'s named-backend branch. `submit_job`
    # (the server) still doesn't originate one. Either way, a caller that OMITS deadline_ms — the
    # common case, unaffected by BL-169 — must keep resolving to the same DEFAULT_DEADLINE_MS as
    # before: unchanged observed behavior is itself part of the acceptance criteria.
    from openreading.adapters.registry import BUILTIN_ADAPTERS
    from openreading.credentials import DEFAULT_DEADLINE_MS
    from openreading.types.request import OpenReadingRequest
    from tests.fakes import ScriptedBackend

    fake = ScriptedBackend("scripted-default", local=True)
    monkeypatch.setitem(BUILTIN_ADAPTERS, "scripted-default", lambda: fake)
    req = OpenReadingRequest.model_validate(
        {"document": {"path": "/d.pdf"}, "backend": {"id": "scripted-default"}}
    )

    _adapter, _req, ctx = api.prepare_named_backend(req, "scripted-default")
    assert ctx.deadline_ms == DEFAULT_DEADLINE_MS


def test_run_deadline_ms_reaches_a_directly_named_backends_real_execution(monkeypatch, tmp_path):
    # BL-169 closes BL-153's own "pure plumbing, not yet wired to any real caller" gap: `run()`'s
    # deadline_ms parameter must reach the ACTUAL driver deadline check for a directly-named
    # backend, not just RunContext.deadline_ms (already proven by the prepare_named_backend tests
    # above). A POLL-mode job that never finishes and an explicit deadline_ms=0 makes the two
    # outcomes distinguishable without any real waiting: the driver's own deadline check runs
    # before the first poll, so this raises immediately regardless of wall-clock time.
    from openreading.adapters.registry import BUILTIN_ADAPTERS
    from openreading.types.errors import RetryableError
    from tests.fakes import NeverFinishesFake

    fake = NeverFinishesFake()
    monkeypatch.setitem(BUILTIN_ADAPTERS, "never-finishes-fake", lambda: fake)
    pdf = tmp_path / "d.pdf"
    pdf.write_bytes(b"%PDF-1.7 fake")

    with pytest.raises(RetryableError, match="deadline exceeded"):
        api.run(str(pdf), backend="never-finishes-fake", deadline_ms=0)
