"""Reducto adapter — fault injection for the branches the happy-path fixtures never reach,
centred on `_map_error`: the one function deciding RetryableError vs TerminalError, and therefore
whether the router retries this backend, falls back, or gives up for good. Every branch is pinned
— each code in `_RETRYABLE_CODES` (parametrized off the set itself, so a code added to it is
covered rather than silently untested), the "429"-anywhere fallback, an already-typed error
returned unchanged ahead of any code lookup, and an unmapped exception falling through to
TerminalError — plus the submit/poll callers that reach it, the intake and credential guards, and
the degenerate wire shapes normalize has to survive. All offline via an injected fake client."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openreading.adapters.reducto import ReductoAdapter
from openreading.adapters.reducto.adapter import _RETRYABLE_CODES
from openreading.router.clock import FakeClock
from openreading.router.driver import run_to_completion
from openreading.types import BlockType, JobState
from openreading.types.errors import RetryableError, TerminalError
from openreading.types.request import OpenReadingRequest
from openreading.types.runtime import RunContext

FIX = Path(__file__).parent / "fixtures" / "reducto"
_BBOX = {"left": 0.1, "top": 0.1, "width": 0.2, "height": 0.05, "page": 1}


def _fixture(name: str) -> dict:
    return json.loads((FIX / f"{name}.json").read_text())


def _req(op: str = "parse", **over) -> OpenReadingRequest:
    body = {
        "document": {"url": "https://example.test/doc.pdf", "mime_type": "application/pdf"},
        "backend": {"id": "reducto", "type": "hosted_api", "operation": op},
    }
    body.update(over)
    return OpenReadingRequest.model_validate(body)


def _async_req(**over) -> OpenReadingRequest:
    return _req(**{"async": {"mode": "async"}}, **over)


class _ScriptedClient:
    """parse/extract return their scripted value (raising it when it is an Exception); get_job
    walks a scripted list, holding on the last entry. Records the document arg so the intake
    tests can assert what actually reached the wire."""

    def __init__(self, *, parse=None, extract=None, jobs=None, cancel_effect=None) -> None:
        self._parse = parse
        self._extract = extract
        self._jobs = list(jobs or [_fixture("parse")])
        self._i = 0
        self.last_document: dict | None = None
        self._cancel_effect = cancel_effect
        self.cancel_calls: list[str] = []

    @staticmethod
    def _emit(value):
        if isinstance(value, Exception):
            raise value
        return value

    def parse(self, document, options, is_async):
        self.last_document = document
        if self._parse is not None:
            return self._emit(self._parse)
        return {"job_id": "job_parse_1"} if is_async else _fixture("parse")

    def extract(self, document, schema, is_async):
        self.last_document = document
        return self._emit(self._extract if self._extract is not None else _fixture("extract"))

    def get_job(self, job_id):
        item = self._jobs[min(self._i, len(self._jobs) - 1)]
        self._i += 1
        return self._emit(item)

    def verify_webhook(self, headers, body):
        return True

    def cancel_job(self, job_id):
        self.cancel_calls.append(job_id)
        if self._cancel_effect is not None:
            raise self._cancel_effect
        return {}


class _Coded(Exception):
    """A provider exception carrying Reducto's own error code, the way the SDK raises it."""

    def __init__(self, code, message="reducto said no") -> None:
        super().__init__(message)
        self.code = code


# ---- _map_error: the retry-vs-give-up decision --------------------------------------------------


@pytest.mark.parametrize("code", sorted(_RETRYABLE_CODES))
def test_every_declared_retryable_code_maps_to_retryable(code):
    # Parametrized off _RETRYABLE_CODES itself: a code added to the set is exercised here
    # automatically instead of shipping as an untested retry decision.
    mapped = ReductoAdapter()._map_error(_Coded(code))
    assert isinstance(mapped, RetryableError)
    assert mapped.backend_code == code


@pytest.mark.parametrize("code", ["429", "http_429", "Error429TooManyRequests", 429])
def test_429_anywhere_in_the_code_maps_to_retryable(code):
    mapped = ReductoAdapter()._map_error(_Coded(code))
    assert isinstance(mapped, RetryableError)
    assert mapped.backend_code == str(code)  # a non-string code is coerced, never carried raw


@pytest.mark.parametrize("code", ["3000", "unsupported_file_type", 500])
def test_code_outside_the_retryable_set_maps_to_terminal(code):
    mapped = ReductoAdapter()._map_error(_Coded(code))
    assert isinstance(mapped, TerminalError)
    assert mapped.backend_code == str(code)


def test_unmapped_exception_falls_through_to_terminal():
    mapped = ReductoAdapter()._map_error(ValueError("boom"))
    assert isinstance(mapped, TerminalError)
    assert mapped.backend_code == "ValueError"  # no code attribute → the exception type name
    assert str(mapped) == "boom"


@pytest.mark.parametrize(
    "err",
    [
        RetryableError("slow down", backend_code="1000"),
        TerminalError("bad document", backend_code="unsupported_input"),
    ],
    ids=["retryable", "terminal"],
)
def test_already_typed_errors_pass_through_unchanged(err):
    assert ReductoAdapter()._map_error(err) is err


def test_a_terminal_error_carrying_a_retryable_code_stays_terminal():
    # The isinstance passthrough runs BEFORE the code lookup, so a classification the adapter (or
    # the shared HTTP layer) already made is never re-litigated into a retry loop.
    err = TerminalError("permanently rejected", backend_code="1000")
    mapped = ReductoAdapter()._map_error(err)
    assert mapped is err and not isinstance(mapped, RetryableError)


def test_backend_code_attribute_wins_over_a_bare_code_attribute():
    class _Both(Exception):
        backend_code = "2000"
        code = "3000"

    mapped = ReductoAdapter()._map_error(_Both("both attributes set"))
    assert isinstance(mapped, RetryableError) and mapped.backend_code == "2000"


# ---- the callers that reach _map_error ----------------------------------------------------------


def test_submit_reraises_taxonomy_errors_unchanged():
    boom = TerminalError("no key", backend_code="auth_rejected")
    adapter = ReductoAdapter(client=_ScriptedClient(parse=boom))
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value is boom


def test_submit_maps_an_unexpected_error_to_terminal():
    adapter = ReductoAdapter(client=_ScriptedClient(parse=ValueError("boom")))
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(), RunContext())
    assert exc.value.backend_code == "ValueError"


def test_submit_extract_maps_a_provider_code():
    adapter = ReductoAdapter(client=_ScriptedClient(extract=_Coded("2000", "excessive polling")))
    with pytest.raises(RetryableError) as exc:
        adapter.submit(_req(op="extract"), RunContext())
    assert exc.value.backend_code == "2000"


def test_poll_maps_a_provider_code_to_retryable():
    adapter = ReductoAdapter(client=_ScriptedClient(jobs=[_Coded("1000", "slow down")]))
    job = adapter.submit(_async_req(), RunContext())
    with pytest.raises(RetryableError) as exc:
        adapter.poll(job, RunContext())
    assert exc.value.backend_code == "1000"


def test_poll_leaves_an_already_typed_error_alone():
    # poll() has no re-raise guard of its own the way submit() does — every exception goes through
    # _map_error, so its isinstance passthrough is the only thing keeping this one terminal.
    boom = TerminalError("job vanished", backend_code="not_found")
    adapter = ReductoAdapter(client=_ScriptedClient(jobs=[boom]))
    job = adapter.submit(_async_req(), RunContext())
    with pytest.raises(TerminalError) as exc:
        adapter.poll(job, RunContext())
    assert exc.value is boom


# ---- poll / webhook lifecycle -------------------------------------------------------------------


def test_poll_failed_status_is_terminal():
    adapter = ReductoAdapter(client=_ScriptedClient(jobs=[{"status": "failed"}]))
    job = adapter.submit(_async_req(), RunContext())
    with pytest.raises(TerminalError) as exc:
        adapter.poll(job, RunContext())
    assert exc.value.backend_code == "job_failed"


def test_poll_pending_reschedules_then_succeeds():
    adapter = ReductoAdapter(
        client=_ScriptedClient(jobs=[{"status": "pending"}, _fixture("parse")])
    )
    job = adapter.submit(_async_req(), RunContext())
    clock = FakeClock()
    job = run_to_completion(
        adapter, job, ctx=RunContext(), deadline_ms=clock.now_ms() + 120_000, clock=clock
    )
    assert job.state is JobState.SUCCEEDED


def test_cancel_calls_the_vendor_and_marks_the_job_cancelled():
    client = _ScriptedClient(jobs=[{"status": "pending"}])
    adapter = ReductoAdapter(client=client)
    job = adapter.submit(_async_req(), RunContext())
    job_id = job.backend_job_id
    out = adapter.cancel(job, RunContext())
    assert client.cancel_calls == [job_id]
    assert out.state is JobState.CANCELLED


def test_cancel_unexpected_error_is_mapped_and_raised():
    # Not silently swallowed by the adapter itself — mapped and raised; engine.py's own
    # `_cancel_loser_job` is the layer that treats this call as best-effort, not this adapter.
    client = _ScriptedClient(jobs=[{"status": "pending"}], cancel_effect=RuntimeError("500"))
    adapter = ReductoAdapter(client=client)
    job = adapter.submit(_async_req(), RunContext())
    with pytest.raises(TerminalError):
        adapter.cancel(job, RunContext())
    assert client.cancel_calls == [job.backend_job_id]


def test_cancel_a_retryable_client_error_stays_retryable():
    # _map_error passes an already-typed RetryableError through unchanged — a genuinely
    # retryable failure (e.g. a real 5xx via error_for_status) must not be re-wrapped into a
    # TerminalError.
    client = _ScriptedClient(
        jobs=[{"status": "pending"}],
        cancel_effect=RetryableError("upstream 503", backend_code="http_503"),
    )
    adapter = ReductoAdapter(client=client)
    job = adapter.submit(_async_req(), RunContext())
    with pytest.raises(RetryableError):
        adapter.cancel(job, RunContext())


def test_cancel_on_an_already_terminal_job_does_not_call_the_vendor():
    client = _ScriptedClient(jobs=[{"status": "pending"}])
    adapter = ReductoAdapter(client=client)
    job = adapter.submit(_async_req(), RunContext())
    job.state = JobState.SUCCEEDED
    adapter.cancel(job, RunContext())
    assert client.cancel_calls == []


def test_cancel_on_a_fresh_instance_reaches_the_vendor():
    # Ledger T4a (AC-5/AC-7, closing BL-164 review (trent, Medium)'s own flagged gap): cancel()
    # now takes ctx: RunContext and always builds its client via self._get_client(ctx) — a fresh
    # instance that never called submit() itself (e.g. a job reconstructed by the Ledger T3 resume
    # path) no longer silently marks CANCELLED with zero vendor contact. Proven here with a
    # SEPARATE fake client on the fresh instance (never the submitting adapter's own client, and
    # no other shared Python-level state) — the fresh instance reaches ITS OWN vendor client, not
    # some client the submitting instance happened to leave bound.
    real_client = _ScriptedClient(jobs=[{"status": "pending"}])
    submitting_adapter = ReductoAdapter(client=real_client)
    job = submitting_adapter.submit(_async_req(), RunContext())
    job_id = job.backend_job_id

    fresh_client = _ScriptedClient(jobs=[{"status": "pending"}])
    fresh_adapter = ReductoAdapter(client=fresh_client)  # a genuinely separate instance
    out = fresh_adapter.cancel(job, RunContext())
    assert out.state is JobState.CANCELLED
    assert fresh_client.cancel_calls == [job_id]  # the fresh instance's OWN client was contacted
    assert real_client.cancel_calls == []  # the submitting instance's client was never touched


def test_webhook_for_another_job_is_ignored():
    adapter = ReductoAdapter(client=_ScriptedClient())
    req = _req(**{"async": {"mode": "async", "webhook_url": "https://me.test/hook"}})
    job = adapter.submit(req, RunContext())
    out = adapter.resolve_webhook(
        {"job_id": "not-ours", "data": _fixture("parse")}, job, RunContext()
    )
    assert out.state is JobState.RUNNING and out.raw is None


def test_webhook_idless_event_against_idless_job_is_ignored():
    # BL-81: BL-70 (sprint 12) added an adapter-level guard, independent of the dispatcher's own
    # (server/app.py), so an id-less event can never match an id-less job. Without it,
    # `eid in (job.webhook_token, job.backend_job_id)` is `None in (None, None)` — True by
    # construction — the moment a vendor create-task 2xx response omits its own id field (leaving
    # both `backend_job_id` and `webhook_token` None). That guard had zero direct regression
    # coverage: the dispatcher's own `jid is not None` check (server/app.py) makes this branch
    # structurally unreachable through any server-level test, so it needs its own adapter-level
    # test to protect it against a future regression reached a different way.
    adapter = ReductoAdapter(client=_ScriptedClient())
    req = _req(**{"async": {"mode": "async", "webhook_url": "https://me.test/hook"}})
    job = adapter.submit(req, RunContext())
    job.backend_job_id = None  # simulate a create-task 2xx that omitted its own id field
    job.webhook_token = None
    out = adapter.resolve_webhook({}, job, RunContext())  # event carries no job_id key at all
    assert out.state is JobState.RUNNING  # id-less event must not be treated as a match


# ---- intake, credentials, readiness -------------------------------------------------------------


def test_file_id_input_is_sent_as_a_reducto_handle():
    client = _ScriptedClient()
    doc = {"file_id": "reducto://abc.pdf", "mime_type": "application/pdf"}
    ReductoAdapter(client=client).submit(_req(document=doc), RunContext())
    assert client.last_document == {"file_id": "reducto://abc.pdf"}


def test_bytes_input_defaults_the_filename():
    client = _ScriptedClient()
    doc = {"bytes_base64": "ZmFrZQ==", "mime_type": "application/pdf"}
    ReductoAdapter(client=client).submit(_req(document=doc), RunContext())
    assert client.last_document == {"base64": "ZmFrZQ==", "filename": "document.pdf"}


def test_local_path_input_is_unsupported():
    adapter = ReductoAdapter(client=_ScriptedClient())
    doc = {"path": "/local.pdf", "mime_type": "application/pdf"}
    with pytest.raises(TerminalError) as exc:
        adapter.submit(_req(document=doc), RunContext())
    assert exc.value.backend_code == "unsupported_input"


def test_submit_without_credentials_is_terminal():
    with pytest.raises(TerminalError) as exc:
        ReductoAdapter().submit(_req(), RunContext())
    assert exc.value.backend_code == "no_credentials"


def test_health_reports_the_missing_extra_when_httpx_is_absent(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _no_httpx(name, *args, **kwargs):
        if name == "httpx":
            raise ImportError("No module named 'httpx'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_httpx)
    health = ReductoAdapter().health()
    assert not health.ready and any("httpx" in dep for dep in health.missing_deps)


# ---- normalize over degenerate wire shapes ------------------------------------------------------


def _normalize_blocks(blocks: list[dict]):
    raw = {
        "result": {"type": "full", "chunks": [{"content": "chunk", "blocks": blocks}]},
        "usage": {"num_pages": 1},
    }
    adapter = ReductoAdapter(client=_ScriptedClient(parse=raw))
    req = _req()
    ctx = RunContext()
    return adapter.normalize(adapter.submit(req, ctx), ctx, req)


def _jsonbbox_table(payload) -> dict:
    return {"type": "Table", "content": json.dumps(payload), "bbox": _BBOX}


def test_block_without_geometry_normalizes_to_no_bbox():
    blk = (
        _normalize_blocks([{"type": "Text", "content": "no geometry here"}])
        .document.pages[0]
        .blocks[0]
    )
    assert blk.bbox is None and blk.text == "no geometry here"


def test_html_in_a_non_table_block_fills_the_html_channel_and_plain_text():
    blocks = [{"type": "Text", "content": "<p>Paid <b>$10</b></p>", "bbox": _BBOX}]
    blk = _normalize_blocks(blocks).document.pages[0].blocks[0]
    assert blk.html == "<p>Paid <b>$10</b></p>"
    assert blk.text and "<" not in blk.text and "$10" in blk.text


@pytest.mark.parametrize(
    "content",
    ['[[{"text": "unterminated"', "(table intentionally omitted)", None],
    ids=["malformed-jsonbbox", "prose", "missing"],
)
def test_table_block_with_no_recoverable_grid_gets_no_table(content):
    blk = (
        _normalize_blocks([{"type": "Table", "content": content, "bbox": _BBOX}])
        .document.pages[0]
        .blocks[0]
    )
    assert blk.type is BlockType.TABLE and blk.table is None  # never a fabricated empty grid


def test_bare_rows_jsonbbox_without_the_table_wrapper_is_accepted():
    # The live shape is [[row, …]] — a list of tables; a single table's rows also arrive unwrapped.
    rows = [[{"text": "Date"}, {"text": "Amount"}], [{"text": "05/01"}, {"text": "100"}]]
    table = _normalize_blocks([_jsonbbox_table(rows)]).document.pages[0].blocks[0].table
    assert table is not None and table.rows == [["Date", "Amount"], ["05/01", "100"]]
    assert all(cell.bbox is None for cell in table.cells)  # no cell geometry → none invented


@pytest.mark.parametrize(
    "payload",
    [[], [{"text": "loose cell"}], ["not-a-row"]],
    ids=["empty", "dict-first", "scalar-first"],
)
def test_unrecognised_jsonbbox_shapes_produce_no_table(payload):
    assert _normalize_blocks([_jsonbbox_table(payload)]).document.pages[0].blocks[0].table is None


def test_jsonbbox_scalar_cells_are_stringified_into_the_grid():
    # Some tables come back as bare values per cell rather than {text, bbox} objects.
    table = (
        _normalize_blocks([_jsonbbox_table([[["Date", "Amount"], ["05/01", 100]]])])
        .document.pages[0]
        .blocks[0]
        .table
    )
    assert table is not None and table.rows == [["Date", "Amount"], ["05/01", "100"]]


@pytest.mark.parametrize(
    ("confidence", "expected"),
    [(True, None), (0.42, 0.42), ("nonsense", None)],
    ids=["bool-guard", "numeric", "unknown-enum"],
)
def test_block_confidence_never_fabricates_a_score(confidence, expected):
    # bool is an int subclass: without the guard `"confidence": true` would land as a perfect 1.0.
    blocks = [{"type": "Text", "content": "hi", "confidence": confidence, "bbox": _BBOX}]
    assert _normalize_blocks(blocks).document.pages[0].blocks[0].confidence == expected


def _normalize_extract(raw: dict):
    adapter = ReductoAdapter(client=_ScriptedClient(extract=raw))
    req = _req(op="extract")
    ctx = RunContext()
    return adapter.normalize(adapter.submit(req, ctx), ctx, req)


def test_extract_non_dict_data_yields_no_typed_fields():
    resp = _normalize_extract({"result": {"data": ["not", "a", "dict"]}})
    assert resp.typed_fields is None  # nothing invented from an unusable payload


def test_extract_flat_result_skips_the_sibling_citations_key():
    # With no `data` wrapper the result IS the field map and `citations` sits alongside the fields;
    # it must not be emitted as a typed field of its own.
    resp = _normalize_extract(
        {
            "result": {
                "invoice_total": "128.50",
                "citations": {
                    "invoice_total": [{"page": 1, "content": "$128.50", "confidence": 0.8}]
                },
            }
        }
    )
    assert set(resp.typed_fields) == {"invoice_total"}
    citation = resp.typed_fields["invoice_total"].citations[0]
    assert citation.confidence == 0.8  # numeric confidence carried through
    assert citation.bbox is None  # no geometry on the citation → no bbox invented
