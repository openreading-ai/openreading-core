"""EnvCredentialBroker + build_run_context + .env loader (GOAL2 milestone 6.2). All offline: the
broker is fed an explicit environ dict, so no test touches the real process env or the network.
Covers precedence, the credentials_ref `env:` scheme, config resolution, and secret redaction."""

from __future__ import annotations

import base64

import pytest

from openreading import api
from openreading import credentials as cred
from openreading.adapters.registry import make_adapter
from openreading.credentials import (
    DEFAULT_DEADLINE_MS,
    EnvCredentialBroker,
    build_run_context,
    load_dotenv,
    redact,
    secret_values,
)
from openreading.evals.dataset import EvalCase
from openreading.evals.runner import run_case
from openreading.readiness import auth_hinted
from openreading.router.clock import FakeClock
from openreading.router.executor import execute_plan
from openreading.router.router import RoutePlan, RouterConfig
from openreading.strategies import StrategyConfig, compile_strategy, run_strategy
from openreading.strategies.calibrate import calibrate_strategy
from openreading.types.errors import TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import ResolvedCredentials, RunContext
from tests.fakes import ScriptedBackend, make_backend, scripted_registry


def _req(backend_id: str, **backend_extra) -> OpenReadingRequest:
    body = {
        "document": {"path": "/doc.pdf", "mime_type": "application/pdf"},
        "backend": {"id": backend_id, **backend_extra},
    }
    return OpenReadingRequest.model_validate(body)


def _desc(slug: str):
    return make_adapter(slug).descriptor


# --- precedence ------------------------------------------------------------------------


def test_resolves_service_native_env():
    broker = EnvCredentialBroker({"REDUCTO_API_KEY": "sk_native"})
    creds = broker.resolve(_desc("reducto"), _req("reducto"))
    assert creds.values["api_key"] == "sk_native"
    assert creds.source == "env"


def test_openreading_prefixed_env_wins_over_native():
    broker = EnvCredentialBroker(
        {"REDUCTO_API_KEY": "sk_native", "OPENREADING_REDUCTO_API_KEY": "sk_override"}
    )
    creds = broker.resolve(_desc("reducto"), _req("reducto"))
    assert creds.values["api_key"] == "sk_override"


def test_credentials_ref_env_scheme_wins_over_env_vars():
    # the alias must be on the operator's allow-list (BL-162) — see the alias-focused tests below
    # for the refusal path this precedence test is no longer exercising.
    broker = EnvCredentialBroker(
        {
            "REDUCTO_API_KEY": "sk_native",
            "MYVAULT_API_KEY": "sk_ref",
            "OPENREADING_CREDENTIALS_REF_ALIASES": "MYVAULT",
        }
    )
    creds = broker.resolve(_desc("reducto"), _req("reducto", credentials_ref="env:MYVAULT"))
    assert creds.values["api_key"] == "sk_ref"


# --- BL-162: credentials_ref is an allow-listed alias, never a free-form env-var prefix -------


def test_credentials_ref_alias_not_on_allow_list_is_refused():
    # unset OPENREADING_CREDENTIALS_REF_ALIASES = no alias accepted, fails closed. Before BL-162,
    # ANY alias resolved as an env-var prefix — this is the exact exfiltration primitive: an
    # unauthenticated caller naming an arbitrary prefix like `ANTHROPIC_API` reads
    # `ANTHROPIC_API_KEY` for a field keyed `key` (BL-162, chain step 3).
    broker = EnvCredentialBroker({"ANTHROPIC_API_KEY": "sk-ant-should-never-leak"})
    with pytest.raises(TerminalError) as exc:
        broker.resolve(
            _desc("azure-document-intelligence"),
            _req("azure-document-intelligence", credentials_ref="env:ANTHROPIC_API"),
        )
    assert exc.value.backend_code == "credentials_ref_alias_not_allowed"
    assert "ANTHROPIC_API" in str(exc.value)


def test_credentials_ref_refusal_does_not_disclose_the_allow_list():
    # BL-162 review (bruce, Medium): this message reaches an unauthenticated caller verbatim
    # (server auth is off by default) — naming the operator's configured aliases here would hand
    # out internal vault/prefix naming for free, the same class of unauthenticated exposure BL-162
    # exists to close. Only the CALLER'S OWN submitted alias (already known to them) may appear.
    broker = EnvCredentialBroker(
        {"OPENREADING_CREDENTIALS_REF_ALIASES": "SECRET_INTERNAL_VAULT_NAME,PROD_AWS_VAULT"}
    )
    with pytest.raises(TerminalError) as exc:
        broker.resolve(_desc("reducto"), _req("reducto", credentials_ref="env:UNLISTED"))
    msg = str(exc.value)
    assert "SECRET_INTERNAL_VAULT_NAME" not in msg
    assert "PROD_AWS_VAULT" not in msg
    assert "UNLISTED" in msg  # the caller's own value may still appear


def test_credentials_ref_alias_allow_list_accepts_only_listed_aliases():
    broker = EnvCredentialBroker(
        {"OPENREADING_CREDENTIALS_REF_ALIASES": "MYVAULT,OTHERVAULT", "MYVAULT_API_KEY": "sk_ok"}
    )
    creds = broker.resolve(_desc("reducto"), _req("reducto", credentials_ref="env:MYVAULT"))
    assert creds.values["api_key"] == "sk_ok"
    with pytest.raises(TerminalError) as exc:
        broker.resolve(_desc("reducto"), _req("reducto", credentials_ref="env:UNLISTED"))
    assert exc.value.backend_code == "credentials_ref_alias_not_allowed"


def test_full_exfiltration_chain_refused_at_first_gate():
    """BL-162 end-to-end: the exact chain from the vulnerability report — an unauthenticated
    caller names an arbitrary credentials_ref alias to read a foreign env var (here
    ANTHROPIC_API_KEY, which this backend's descriptor never declares) as azure-document-
    intelligence's `key`, AND a request-controlled `backend.runtime.endpoint` to redirect the
    authenticated call. build_run_context (what every surface — CLI, server, Python API — calls)
    must refuse before either secret is resolved or the client is ever built, at the first gate:
    the credentials_ref alias check in resolve(), which runs before resolve_config()."""
    env = {"ANTHROPIC_API_KEY": "sk-ant-should-never-leak"}  # the operator's real, unrelated key
    req = _req(
        "azure-document-intelligence",
        credentials_ref="env:ANTHROPIC_API",
        runtime={"endpoint": "https://attacker.example.com"},
    )
    with pytest.raises(TerminalError) as exc:
        build_run_context(
            req, _desc("azure-document-intelligence"), broker=EnvCredentialBroker(env)
        )
    assert exc.value.backend_code == "credentials_ref_alias_not_allowed"
    assert "sk-ant-should-never-leak" not in str(exc.value)


def test_unsupported_credentials_ref_raises():
    broker = EnvCredentialBroker({})
    try:
        broker.resolve(_desc("reducto"), _req("reducto", credentials_ref="vault:prod/reducto"))
    except TerminalError as e:
        assert e.backend_code == "unsupported_credentials_ref"
    else:
        raise AssertionError("expected TerminalError for a non-env credentials_ref")


def test_raw_secret_in_credentials_ref_never_honored():
    # a raw key value (no scheme) is rejected, not treated as the secret
    broker = EnvCredentialBroker({})
    try:
        broker.resolve(_desc("reducto"), _req("reducto", credentials_ref="sk_rawsecret"))
    except TerminalError as e:
        assert e.backend_code == "unsupported_credentials_ref"
    else:
        raise AssertionError("raw secret in credentials_ref must be rejected")


# --- ambient chain preserved -----------------------------------------------------------


def test_ambient_backend_resolves_empty_credentials():
    # textract with nothing in env → the broker resolves nothing → RunContext.credentials is None,
    # so the adapter's boto3 default chain takes over exactly as before.
    ctx = build_run_context(
        _req("aws-textract"), _desc("aws-textract"), broker=EnvCredentialBroker({})
    )
    assert ctx.credentials is None


def test_textract_resolves_full_aws_chain_when_present():
    env = {
        "AWS_ACCESS_KEY_ID": "AKIA...",
        "AWS_SECRET_ACCESS_KEY": "secret",
        "AWS_DEFAULT_REGION": "eu-west-1",
    }
    broker = EnvCredentialBroker(env)
    creds = broker.resolve(_desc("aws-textract"), _req("aws-textract"))
    assert creds.values["aws_access_key_id"] == "AKIA..."
    assert "aws_session_token" not in creds.values  # optional + absent
    # region is non-secret config, so it resolves into ctx.runtime, not ctx.credentials
    assert "region" not in creds.values
    runtime = broker.resolve_config(_desc("aws-textract"), _req("aws-textract"))
    assert runtime["region"] == "eu-west-1"  # via AWS_DEFAULT_REGION alias


# --- config resolution (ctx.runtime) ---------------------------------------------------


def test_config_resolves_endpoint_from_env():
    broker = EnvCredentialBroker({"DOCLING_SERVE_URL": "http://box:5001"})
    runtime = broker.resolve_config(_desc("docling"), _req("docling"))
    assert runtime["endpoint"] == "http://box:5001"


def test_request_runtime_endpoint_is_refused_not_honored():
    # BL-162 part 2: backend.runtime.endpoint is operator configuration only. Before this, a
    # request-supplied endpoint always won over the environment (D7 violation: router/deployment
    # configuration must never come from the request body) — a caller could redirect a hosted
    # backend's authenticated call, key included, to a host of their choosing.
    broker = EnvCredentialBroker({"DOCLING_SERVE_URL": "http://env:5001"})
    req = _req("docling", runtime={"endpoint": "http://attacker.example.com"})
    with pytest.raises(TerminalError) as exc:
        broker.resolve_config(_desc("docling"), req)
    assert exc.value.backend_code == "endpoint_not_request_configurable"


def test_request_runtime_other_fields_still_pass_through():
    # only `endpoint` is refused — mode/image/device/system_deps_ok control local execution, not
    # a network destination, and stay request-settable.
    broker = EnvCredentialBroker({})
    req = _req("docling", runtime={"device": "cpu", "system_deps_ok": True})
    runtime = broker.resolve_config(_desc("docling"), req)
    assert runtime["device"] == "cpu"
    assert runtime["system_deps_ok"] is True


def test_qwen_vl_request_runtime_endpoint_is_also_refused():
    # BL-162's "Watch for": qwen_vl reads ctx.runtime["endpoint"] directly (adapter.py:412-414)
    # the same way azure-document-intelligence does — the fix lives in the shared
    # resolve_config() mechanism, not per-adapter, so it must close this one too.
    broker = EnvCredentialBroker({"QWEN_VL_ENDPOINT": "http://gpu:8000/v1"})
    req = _req("qwen-vl", runtime={"endpoint": "http://attacker.example.com"})
    with pytest.raises(TerminalError) as exc:
        broker.resolve_config(_desc("qwen-vl"), req)
    assert exc.value.backend_code == "endpoint_not_request_configurable"


def test_qwen_config_and_credentials_split():
    env = {"QWEN_VL_ENDPOINT": "http://gpu:8000/v1", "QWEN_VL_API_KEY": "tok"}
    ctx = build_run_context(_req("qwen-vl"), _desc("qwen-vl"), broker=EnvCredentialBroker(env))
    assert ctx.runtime["endpoint"] == "http://gpu:8000/v1"  # config → runtime
    assert ctx.credentials.values["api_key"] == "tok"  # secret → credentials


# --- build_run_context wiring ----------------------------------------------------------


def test_build_run_context_fills_budget_compliance_idempotency():
    body = {
        "document": {"path": "/d.pdf", "mime_type": "application/pdf"},
        "backend": {"id": "reducto"},
        "compliance": {"require_baa": True},
        "idempotency_key": "idem-123",
    }
    req = OpenReadingRequest.model_validate(body)
    ctx = build_run_context(
        req, _desc("reducto"), broker=EnvCredentialBroker({"REDUCTO_API_KEY": "k"})
    )
    assert ctx.deadline_ms == DEFAULT_DEADLINE_MS
    assert ctx.compliance["require_baa"] is True  # full compliance block passed through
    assert ctx.idempotency_key == "idem-123"


# --- BL-166: a caller-omitted idempotency_key defaults to content_key(...) ------------


def test_build_run_context_seeds_default_idempotency_key_when_omitted():
    # BL-166 review (trent): reducto declares idempotency_supported=False (no vendor mechanism) —
    # pinned explicitly so this test's point (a key is generated regardless of vendor support;
    # forwarding it is the ADAPTER's own job to skip, not build_run_context's) doesn't silently
    # stop being tested if reducto's flag ever flips.
    assert _desc("reducto").idempotency_supported is False
    body = {
        "document": {"bytes_base64": "aGVsbG8=", "mime_type": "application/pdf"},
        "backend": {"id": "reducto"},
    }
    req = OpenReadingRequest.model_validate(body)
    ctx = build_run_context(
        req, _desc("reducto"), broker=EnvCredentialBroker({"REDUCTO_API_KEY": "k"})
    )
    assert ctx.idempotency_key is not None
    assert ctx.idempotency_key.startswith("om_")


def test_default_idempotency_key_is_deterministic_across_two_runs():
    body = {
        "document": {"bytes_base64": "aGVsbG8=", "mime_type": "application/pdf"},
        "backend": {"id": "reducto"},
    }
    req = OpenReadingRequest.model_validate(body)
    broker = EnvCredentialBroker({"REDUCTO_API_KEY": "k"})
    key1 = build_run_context(req, _desc("reducto"), broker=broker).idempotency_key
    key2 = build_run_context(req, _desc("reducto"), broker=broker).idempotency_key
    assert key1 == key2


def test_default_idempotency_key_differs_by_backend_and_by_content():
    broker = EnvCredentialBroker(
        {"REDUCTO_API_KEY": "k", "ANTHROPIC_API_KEY": "k", "CHUNKR_API_KEY": "k"}
    )

    def _key(content_b64: str, backend_id: str) -> str | None:
        req = OpenReadingRequest.model_validate(
            {
                "document": {"bytes_base64": content_b64, "mime_type": "application/pdf"},
                "backend": {"id": backend_id},
            }
        )
        return build_run_context(req, _desc(backend_id), broker=broker).idempotency_key

    a = _key("aGVsbG8=", "reducto")
    b = _key("d29ybGQ=", "reducto")  # different content
    c = _key("aGVsbG8=", "chunkr")  # same content, different backend
    assert len({a, b, c}) == 3


def test_no_default_idempotency_key_for_a_locator_only_document():
    # BL-166: a URL/file_id document (or an unreadable path) has no stable content identity — the
    # same conservative rule the executor's own result cache already applies. The caller must
    # supply idempotency_key explicitly for retry safety on that document.
    body = {
        "document": {"url": "https://example.com/doc.pdf", "mime_type": "application/pdf"},
        "backend": {"id": "reducto"},
    }
    req = OpenReadingRequest.model_validate(body)
    ctx = build_run_context(
        req, _desc("reducto"), broker=EnvCredentialBroker({"REDUCTO_API_KEY": "k"})
    )
    assert ctx.idempotency_key is None


_URL_SOURCED_NO_DEFAULT_KEY_ADAPTERS = (
    "azure-document-intelligence",
    "chunkr",
    "pulse",
    "reducto",
    "open-ocr",
)


def test_no_default_idempotency_key_for_url_sourced_document_across_named_adapters():
    # BL-166 round 2 (trent, following ben's High + FOUNDER-INBOX 2026-08-22): pins, for every
    # adapter the disclosure names by name, both halves of the interaction together — it still
    # accepts a URL AND a URL-sourced request to it still gets no default key — so a future
    # change to document_identity, or to any of these adapters' accepts_url flag, can't silently
    # drift this specific, already-documented interaction without a test noticing.
    broker = EnvCredentialBroker(
        {
            "AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT": "https://x.cognitiveservices.azure.com",
            "AZURE_DOCUMENT_INTELLIGENCE_KEY": "k",
            "CHUNKR_API_KEY": "k",
            "PULSE_API_KEY": "k",
            "REDUCTO_API_KEY": "k",
            "OPENOCR_API_KEY": "k",
        }
    )
    for backend_id in _URL_SOURCED_NO_DEFAULT_KEY_ADAPTERS:
        desc = _desc(backend_id)
        assert desc.accepts_url is True, f"{backend_id}: disclosure assumes accepts_url is True"
        req = OpenReadingRequest.model_validate(
            {
                "document": {"url": "https://example.com/doc.pdf", "mime_type": "application/pdf"},
                "backend": {"id": backend_id},
            }
        )
        ctx = build_run_context(req, desc, broker=broker)
        assert ctx.idempotency_key is None, f"{backend_id}: disclosure claims no default key here"


def test_explicit_idempotency_key_is_never_overridden_by_the_default():
    body = {
        "document": {"bytes_base64": "aGVsbG8=", "mime_type": "application/pdf"},
        "backend": {"id": "reducto"},
        "idempotency_key": "caller-supplied",
    }
    req = OpenReadingRequest.model_validate(body)
    ctx = build_run_context(
        req, _desc("reducto"), broker=EnvCredentialBroker({"REDUCTO_API_KEY": "k"})
    )
    assert ctx.idempotency_key == "caller-supplied"


def test_build_run_context_threads_explicit_deadline_ms():
    # BL-146 (credentials.py's own half): a caller with its own already-resolved budget can pass
    # deadline_ms explicitly and have it reach ctx.deadline_ms verbatim — including an explicit
    # zero, which must NOT fall back to DEFAULT_DEADLINE_MS (truthiness would silently discard it).
    broker = EnvCredentialBroker({"REDUCTO_API_KEY": "k"})
    for deadline_ms in (1000, 0):
        ctx = build_run_context(
            _req("reducto"), _desc("reducto"), broker=broker, deadline_ms=deadline_ms
        )
        assert ctx.deadline_ms == deadline_ms


# --- .env loader -----------------------------------------------------------------------


def test_load_dotenv_sets_without_override(tmp_path):
    envfile = tmp_path / ".env"
    envfile.write_text(
        "# a comment\n"
        "REDUCTO_API_KEY=sk_fromfile\n"
        'export CHUNKR_API_KEY="ck_quoted"\n'
        "PRESET=fromfile\n"
        "\n"
    )
    env = {"PRESET": "already_set"}
    n = load_dotenv(envfile, environ=env)
    assert n == 2  # PRESET not overridden, so 2 of 3 set
    assert env["REDUCTO_API_KEY"] == "sk_fromfile"
    assert env["CHUNKR_API_KEY"] == "ck_quoted"  # quotes stripped
    assert env["PRESET"] == "already_set"  # never overridden


def test_load_dotenv_missing_file_is_noop(tmp_path):
    assert load_dotenv(tmp_path / "nope.env", environ={}) == 0


def test_load_dotenv_strips_inline_comments(tmp_path):
    # THE live bug: `NUEXTRACT_BASE_URL=   # optional: on-prem…` parsed the comment as the VALUE —
    # a truthy scheme-less string that beat the adapter's default base URL and broke every request
    # ("Request URL is missing an 'http://' or 'https://' protocol"). An unquoted `#` at the start
    # of the value or preceded by whitespace begins a comment (python-dotenv semantics).
    envfile = tmp_path / ".env"
    envfile.write_text(
        "NUEXTRACT_BASE_URL=                     # optional: on-prem/enterprise deployment\n"
        "CHUNKR_BASE_URL= # optional: self-hosted container\n"
        "WITH_VALUE=https://example.test # prod endpoint\n"
        "HASH_NO_SPACE=abc#not-a-comment\n"  # no whitespace before # → part of the value
        'QUOTED_HASH="kept # inside"\n'  # quoted → # is data
        "QUOTED_THEN_COMMENT='v' # trailing\n"  # comment after the closing quote
        "BARE_HASH=#immediate\n"  # value IS a comment → empty
    )
    env: dict[str, str] = {}
    load_dotenv(envfile, environ=env)
    assert env["NUEXTRACT_BASE_URL"] == ""  # falsy → the adapter default base URL applies
    assert env["CHUNKR_BASE_URL"] == ""
    assert env["WITH_VALUE"] == "https://example.test"
    assert env["HASH_NO_SPACE"] == "abc#not-a-comment"
    assert env["QUOTED_HASH"] == "kept # inside"
    assert env["QUOTED_THEN_COMMENT"] == "v"
    assert env["BARE_HASH"] == ""


# --- redaction canary ------------------------------------------------------------------


def test_secret_never_leaks_in_repr_or_source():
    canary = "sk_CANARY_9f3a_do_not_log"
    broker = EnvCredentialBroker({"REDUCTO_API_KEY": canary})
    ctx = build_run_context(_req("reducto"), _desc("reducto"), broker=broker)
    # the credential bag's repr shows key NAMES, never values
    assert canary not in repr(ctx.credentials)
    assert canary not in repr(ctx)  # RunContext repr delegates to ResolvedCredentials repr
    assert "api_key" in repr(ctx.credentials)
    assert ctx.credentials.source == "env"  # source carries no secret value
    assert canary not in str(ctx.credentials.source)


def test_redact_scrubs_secret_but_keeps_nonsecret():
    canary = "sk_CANARY_9f3a"
    desc = _desc("azure-document-intelligence")
    creds = ResolvedCredentials(
        values={"endpoint": "https://r.cognitiveservices.azure.com", "key": canary}
    )
    secrets = secret_values(desc, creds)
    assert secrets == {canary}  # endpoint is secret=False → excluded
    msg = f"POST https://r.cognitiveservices.azure.com with key={canary}"
    scrubbed = redact(msg, secrets)
    assert canary not in scrubbed
    assert "r.cognitiveservices.azure.com" in scrubbed  # non-secret endpoint survives


def test_module_exports_broker_and_factory():
    # smoke: the public names the CLI/server/executor import exist
    assert hasattr(cred, "EnvCredentialBroker") and hasattr(cred, "build_run_context")


# --- BL-37: redaction wired into the executor/API failure paths ------------------------
#
# Before this item, redact()/secret_values() were correct and unit-tested (above) but never
# called anywhere outside their own test — attach_auth_hint's auth_rejected rewrite was the only
# thing standing between a raw provider body and the CLI/API output. These reproduce the item's
# own canary-secret repro end-to-end through the real router.executor / api.run_request paths.


def test_execute_plan_redacts_secret_from_a_failing_backend_error():
    # A backend fails carrying a resolved secret verbatim in its raw message (a leaky provider
    # body, an echoed key on a non-401/403 status, …). execute_plan's per-attempt except block must
    # scrub it before falling back — not only attach_auth_hint's narrower auth_rejected rewrite.
    canary = "sk_CANARY_executor_9f3a"
    exc = TerminalError(
        f"upstream said: request rejected, key={canary}", backend_code="server_error"
    )
    bad = ScriptedBackend("bad", required_env=["CANARY_EXEC_KEY"], error=exc)
    plan = RoutePlan(chosen=bad, fallbacks=[make_backend("good", local=True)])
    broker = EnvCredentialBroker({"CANARY_EXEC_KEY": canary})

    resp = execute_plan(plan, _req("auto"), broker=broker)

    assert resp.backend.id == "good"  # fell back past the failing backend
    # the SAME exception instance execute_plan caught is scrubbed in place — str(exc) is exactly
    # what a caller (trail, log, re-raise) would render.
    assert canary not in exc.message
    assert canary not in str(exc)
    assert "***" in exc.message
    assert exc.args == (exc.message,)


def test_execute_plan_auth_rejected_hint_is_not_further_mangled():
    # No regression: an auth_rejected failure still gets EXACTLY attach_auth_hint's key-free
    # sentence. The new redact() step runs after it and is a no-op here, because the hint text
    # never contains a secret value to begin with.
    canary = "sk_CANARY_hint_1a2b"
    exc = TerminalError(f"401 unauthorized, saw key {canary}", backend_code="auth_rejected")
    bad = ScriptedBackend("bad", required_env=["CANARY_HINT_KEY"], error=exc)
    plan = RoutePlan(chosen=bad, fallbacks=[make_backend("good", local=True)])
    broker = EnvCredentialBroker({"CANARY_HINT_KEY": canary})

    execute_plan(plan, _req("auto"), broker=broker)

    assert exc.message == "key was found but rejected by bad — check CANARY_HINT_KEY"
    assert canary not in exc.message


def test_auth_hinted_redacts_a_resolved_secret_when_credentials_are_given():
    # Direct coverage of the new optional `credentials` parameter — the mechanism api.py's and
    # server/app.py's direct-adapter call sites now pass their RunContext.credentials through.
    canary = "sk_CANARY_direct_7d21"
    desc = _desc("azure-document-intelligence")
    creds = ResolvedCredentials(values={"key": canary})
    exc = TerminalError(f"400 bad request, saw key={canary}", backend_code="bad_request")
    try:
        with auth_hinted(desc, creds):
            raise exc
    except TerminalError as caught:
        assert caught is exc
        assert canary not in str(caught)
        assert "***" in caught.message
    else:
        raise AssertionError("expected TerminalError to propagate")


def test_auth_hinted_with_no_credentials_argument_is_unchanged():
    # Every pre-existing caller that never passes `credentials` (evals, strategies) keeps behaving
    # exactly as before — the new parameter is a strict opt-in, never a required one.
    exc = TerminalError("plain failure, no secret", backend_code="bad_request")
    try:
        with auth_hinted(_desc("reducto")):
            raise exc
    except TerminalError as caught:
        assert caught.message == "plain failure, no secret"
    else:
        raise AssertionError("expected TerminalError to propagate")


def test_run_request_named_backend_redacts_secret_end_to_end(monkeypatch):
    # The item's own canary repro, driven through the real api.run_request() named-backend path:
    # `with auth_hinted(adapter.descriptor, ctx.credentials)` at api.py wraps adapter.submit() the
    # same way for every named-backend run. str(exc) here is exactly what cmd_parse's single-backend
    # render path prints and what the HTTP API's error envelope carries as `message`.
    canary = "sk_CANARY_apipath_5c6d"
    exc = TerminalError(
        f"provider rejected the request; echoed key={canary}", backend_code="bad_request"
    )
    bad = ScriptedBackend("bad", required_env=["CANARY_API_KEY"], error=exc)
    monkeypatch.setattr(api, "make_adapter", lambda slug: bad)
    broker = EnvCredentialBroker({"CANARY_API_KEY": canary})

    try:
        api.run_request(_req("bad"), broker=broker)
    except TerminalError as caught:
        assert caught is exc
        assert canary not in str(caught)
        assert canary not in caught.message
        assert "***" in caught.message
    else:
        raise AssertionError("expected TerminalError to propagate")


# --- BL-99: auth_hinted / execute_plan / run_request never caught a PLAIN, non-AdapterError -----
# crash out of normalize() at all (KeyError/IndexError/ValueError/AttributeError — an ordinary
# adapter bug, not one of the five _ADAPTER_ERRORS taxonomy types). Every test above this banner
# uses a TerminalError (an AdapterError subclass) raised from submit() — the one substitution none
# of them makes. These are the same three scenarios, with that one substitution, each asserting
# BOTH properties BL-99 requires at once: the crash is converted into a structured outcome (never
# an uncaught crash) AND its message is redacted before anything downstream can render it.


def test_auth_hinted_redacts_a_plain_non_adapter_exception():
    # auth_hinted's ORIGINAL except AdapterError clause is blind to anything else — a plain
    # ValueError sails through untouched. The new, second except Exception clause must redact it
    # too, without calling attach_auth_hint (an AdapterError-only operation that reads
    # exc.backend_code, which a plain exception doesn't have).
    canary = "sk_CANARY_plain_9f21"
    desc = _desc("azure-document-intelligence")
    creds = ResolvedCredentials(values={"key": canary})
    exc = ValueError(f"malformed response body, saw key={canary}")
    try:
        with auth_hinted(desc, creds):
            raise exc
    except ValueError as caught:
        assert caught is exc
        assert canary not in str(caught)
        assert "***" in str(caught)
    else:
        raise AssertionError("expected ValueError to propagate")


def test_execute_plan_redacts_a_plain_normalize_crash_and_falls_back():
    # execute_plan has never routed through auth_hinted (it predates it, BL-37) — its redaction has
    # always been a hand-copied inline sequence scoped to `except _TAXONOMY` only. A plain
    # ValueError out of normalize() (not one of the four taxonomy types) used to propagate straight
    # out of execute_plan uncaught — a healthy second backend was never even contacted. This proves
    # both post-fix properties: the chain still falls back cleanly (structured, not crashed) AND the
    # SAME exception instance execute_plan caught is scrubbed in place, mirroring
    # test_execute_plan_redacts_secret_from_a_failing_backend_error above but for normalize(), not
    # submit(), and a plain exception, not a TerminalError.
    canary = "sk_CANARY_execplan_plain_9f3b"
    exc = ValueError(f"malformed page structure, saw key={canary}")
    bad = ScriptedBackend("bad", required_env=["CANARY_EXECPLAN_PLAIN_KEY"], normalize_error=exc)
    plan = RoutePlan(chosen=bad, fallbacks=[make_backend("good", local=True)])
    broker = EnvCredentialBroker({"CANARY_EXECPLAN_PLAIN_KEY": canary})

    resp = execute_plan(plan, _req("auto"), broker=broker)

    assert resp.backend.id == "good"  # fell back past the crashing backend, not an uncaught crash
    assert canary not in str(exc)
    assert "***" in str(exc)


def test_run_request_named_backend_redacts_a_plain_normalize_crash(monkeypatch):
    # run_request's named-backend branch had no try/except of any kind around its
    # `with auth_hinted(...)` block — a plain ValueError out of normalize() propagated straight past
    # every caller's typed except clauses (server's _ADAPTER_ERRORS catch, the CLI's own
    # (TerminalError, ComplianceRefused) catch) to a bare, undocumented crash. Converting it into a
    # TerminalError gives it the identical structured handling those callers already give BL-85's
    # three async-job sinks; auth_hinted's own widening (proven directly above) has already redacted
    # the message by the time it reaches here.
    canary = "sk_CANARY_apipath_plain_9f22"
    crash = ValueError(f"malformed output; echoed key={canary}")
    bad = ScriptedBackend("bad", required_env=["CANARY_API_PLAIN_KEY"], normalize_error=crash)
    monkeypatch.setattr(api, "make_adapter", lambda slug: bad)
    broker = EnvCredentialBroker({"CANARY_API_PLAIN_KEY": canary})

    with pytest.raises(TerminalError) as exc:
        api.run_request(_req("bad"), broker=broker)
    assert canary not in str(exc.value)
    assert "***" in str(exc.value)


# --- BL-47: auth_hinted's 3 previously-excluded call sites -----------------------------


def _sample_document() -> dict:
    return {"bytes_base64": base64.b64encode(b"%PDF-1.4 fake").decode(), "mime_type": "text/plain"}


def test_strategy_engine_leaf_redacts_leaked_secret_from_attempt_detail():
    # strategies/engine.py:1551 (_execute_leaf_sync) — the universal leaf-execution boundary,
    # never wired by BL-37. Driven through a real (minimal) strategy run, not auth_hinted directly.
    canary = "sk_CANARY_bl47_engine"
    reg = scripted_registry(
        ScriptedBackend(
            "leaky",
            cost_low=0.01,
            required_env=["OPENREADING_TEST_BL47_ENGINE_KEY"],
            error=TerminalError(f"upstream echoed key={canary}", backend_code="server"),
        ),
        ScriptedBackend("safe", local=True, text="clean fallback text " * 5),
    )
    broker = EnvCredentialBroker({"OPENREADING_TEST_BL47_ENGINE_KEY": canary})
    req = OpenReadingRequest.model_validate(
        {"document": _sample_document(), "backend": {"id": "strategy:s"}}
    )
    cfg = StrategyConfig.model_validate(
        {"version": 1, "strategies": {"s": {"steps": ["leaky", "safe"]}}}
    )
    compiled = compile_strategy(req, "s", cfg, reg, RouterConfig())
    result = run_strategy(compiled, req, registry=reg, broker=broker, clock=FakeClock())

    # the failed leaky attempt fell back to `safe`; its detail (persisted into Attempt.detail and
    # serialized by Trace.orchestration()) must carry the redaction, not the raw secret.
    detail = next(a["detail"] for a in result.orchestration["attempts"] if a["backend"] == "leaky")
    assert canary not in detail
    assert "***" in detail


def test_strategy_engine_leaf_redacts_a_plain_normalize_crash():
    # BL-99: the same _execute_leaf_sync boundary as immediately above, but the one substitution
    # BL-47's own test (and BL-85's/BL-93's eight tests) never makes — a plain, non-AdapterError
    # exception (not a TerminalError) out of normalize(). Pre-BL-99, _run_leaf's enclosing
    # `except (TerminalError, RetryableError, UnsupportedFeatureError, ComplianceRefused)` clause
    # does not match a plain ValueError at all, so it propagated straight out of the cascade
    # uncaught — the second, healthy backend was never reached.
    canary = "sk_CANARY_bl99_leaf_plain"
    reg = scripted_registry(
        ScriptedBackend(
            "leaky",
            cost_low=0.01,
            required_env=["OPENREADING_TEST_BL99_LEAF_PLAIN_KEY"],
            normalize_error=ValueError(f"malformed page structure, saw key={canary}"),
        ),
        ScriptedBackend("safe", local=True, text="clean fallback text " * 5),
    )
    broker = EnvCredentialBroker({"OPENREADING_TEST_BL99_LEAF_PLAIN_KEY": canary})
    req = OpenReadingRequest.model_validate(
        {"document": _sample_document(), "backend": {"id": "strategy:s"}}
    )
    cfg = StrategyConfig.model_validate(
        {"version": 1, "strategies": {"s": {"steps": ["leaky", "safe"]}}}
    )
    compiled = compile_strategy(req, "s", cfg, reg, RouterConfig())
    result = run_strategy(compiled, req, registry=reg, broker=broker, clock=FakeClock())

    assert result.response.backend.id == "safe"  # fell back cleanly, not an uncaught crash
    detail = next(a["detail"] for a in result.orchestration["attempts"] if a["backend"] == "leaky")
    assert canary not in detail
    assert "***" in detail


def test_calibrate_strategy_redacts_leaked_secret(tmp_path, monkeypatch):
    # strategies/calibrate.py:235 (calibrate_strategy) — the documented `openreading calibrate`
    # CLI command's rung-1 execution. calibrate_strategy has no broker override, so the canary is
    # injected via the real env var the broker's service-native lookup reads (still fully offline).
    canary = "sk_CANARY_bl47_calibrate"
    monkeypatch.setenv("OPENREADING_TEST_BL47_CALIBRATE_KEY", canary)
    reg = scripted_registry(
        ScriptedBackend(
            "cheap",
            cost_low=0.01,
            required_env=["OPENREADING_TEST_BL47_CALIBRATE_KEY"],
            error=TerminalError(f"upstream echoed key={canary}", backend_code="server"),
        )
    )
    case_dir = tmp_path / "case_00"
    case_dir.mkdir()
    (case_dir / "case.json").write_text('{"name": "c0", "input": {"builtin_sample": true}}')
    cfg = StrategyConfig.model_validate({"version": 1, "strategies": {"s": {"steps": ["cheap"]}}})

    with pytest.raises(TerminalError) as exc:
        calibrate_strategy(str(tmp_path), cfg, "s", reg)
    assert canary not in str(exc.value)
    assert "***" in str(exc.value)


def test_run_case_redacts_leaked_secret():
    # evals/runner.py:67 (run_case) — the eval harness's own submit boundary.
    canary = "sk_CANARY_bl47_runner"
    backend = ScriptedBackend(
        "cheap",
        cost_low=0.01,
        required_env=["OPENREADING_TEST_BL47_RUNNER_KEY"],
        error=TerminalError(f"upstream echoed key={canary}", backend_code="server"),
    )
    case = EvalCase(
        name="c0",
        request_body={"document": _sample_document(), "backend": {"id": "cheap"}},
        expected={},
    )
    ctx = RunContext(
        credentials=ResolvedCredentials(values={"openreading_test_bl47_runner_key": canary})
    )

    result = run_case(backend, case, ctx)
    assert result.error is not None
    assert canary not in result.error
    assert "***" in result.error
