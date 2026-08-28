"""Phase D (Manifest v0.6 §7) — the native-batch protocol: dispatch rule, native execution, and
M10 observational equivalence with the platform path. A fake native adapter proves the contract
without any real provider (the anthropic reference impl is tested separately, live-lane-gated)."""

from __future__ import annotations

import pytest

from openreading import run_batch, schemas
from openreading.adapters.base import NativeBatchAdapter
from openreading.adapters.registry import BUILTIN_ADAPTERS
from openreading.credentials import EnvCredentialBroker
from openreading.router.cost import apply_cost_report
from openreading.testing.sample_pdf import build_sample_pdf
from openreading.types import (
    BackendInfo,
    BackendType,
    Document,
    Health,
    Job,
    JobState,
    NormalizedResponse,
    RawResult,
    ResponseState,
    Status,
    WaitMode,
)
from openreading.types.batch import BatchItemError
from openreading.types.cost import CostReport, infra_only
from openreading.types.descriptor import BatchIntake, CredentialField
from openreading.types.errors import TerminalError
from tests.fakes import make_descriptor


class _FakeNative:
    """A minimal adapter implementing the required surface + the native-batch protocol. `native`
    toggles the descriptor grade; `fail_on` filenames come back as per-item errors.
    `submit_many_error`/`normalize_many_error` (BL-106) raise a plain exception from the named
    method — mirroring `tests/fakes.py`'s `ScriptedBackend.normalize_error` — to exercise
    `_run_native`'s crash-to-`TerminalError` guard; `required_env` (mirroring
    `ScriptedBackend.required_env`) attaches a secret credential field so a redaction regression
    can prove the resulting message stays scrubbed.

    `leak_secret_in_cost` (BL-108): when set, `report_cost` raises with this string embedded (a
    stand-in for a billing endpoint echoing a resolved secret back in its error body, mirroring
    tests/test_server.py's own `_LeakyMeterBackend` pattern) and the descriptor grows a
    `credentials_spec` secret field so `EnvCredentialBroker` actually resolves a matching value
    into `ctx.credentials` for `apply_cost_report`'s redaction to find."""

    def __init__(
        self,
        *,
        native="claimed",
        max_items=None,
        fail_on=None,
        submit_many_error=None,
        normalize_many_error=None,
        required_env=None,
        leak_secret_in_cost=None,
    ):
        base = make_descriptor("fake-native", [WaitMode.INLINE])
        update: dict = {
            "capabilities": base.capabilities.model_copy(update={"input_formats": ["pdf"]}),
            "batch": BatchIntake(native=native, max_items=max_items)
            if native is not None
            else None,
        }
        if required_env:
            update["credentials_spec"] = [
                CredentialField(key=e.lower(), required=True, secret=True, env=[e])
                for e in required_env
            ]
        elif leak_secret_in_cost is not None:
            update["credentials_spec"] = [
                CredentialField(
                    key="api_key", required=False, secret=True, env=["FAKE_NATIVE_API_KEY"]
                )
            ]
        self.descriptor = base.model_copy(update=update)
        self._fail_on = set(fail_on or [])
        self._submit_many_error = submit_many_error
        self._normalize_many_error = normalize_many_error
        self._leak_secret_in_cost = leak_secret_in_cost

    # --- required surface ---
    def capabilities(self):
        return self.descriptor.capabilities.model_dump(mode="json")

    def health(self):
        return Health(ready=True)

    def submit(self, req, ctx):
        job = Job(
            id="j", backend_id="fake-native", wait_mode=WaitMode.INLINE, state=JobState.SUCCEEDED
        )
        job.raw = RawResult(payload={})
        return job

    def poll(self, job, ctx):
        return job

    def resolve_webhook(self, event, job, ctx):
        return job

    def cancel(self, job, ctx):
        return job

    def normalize(self, job, ctx, req):
        return NormalizedResponse(
            status=Status(state=ResponseState.SUCCEEDED),
            backend=BackendInfo(id="fake-native", type=BackendType.HOSTED_API),
            document=Document(text="single"),
        )

    def report_cost(self, job) -> CostReport:
        if self._leak_secret_in_cost is not None:
            raise RuntimeError(f"billing endpoint rejected key {self._leak_secret_in_cost}")
        return infra_only("page", 1.0)

    # --- native batch protocol ---
    def submit_many(self, reqs, ctx) -> Job:
        if self._submit_many_error is not None:
            raise self._submit_many_error
        job = Job(
            id="jb", backend_id="fake-native", wait_mode=WaitMode.INLINE, state=JobState.SUCCEEDED
        )
        job.raw = RawResult(payload={"n": len(reqs)})
        return job

    def normalize_many(self, job, reqs, credentials=None):
        if self._normalize_many_error is not None:
            raise self._normalize_many_error
        out = []
        for i, req in enumerate(reqs):
            fn = req.document.filename or f"item{i}"
            if fn in self._fail_on:
                out.append(BatchItemError(code="native_item_error", message=f"boom {fn}"))
            else:
                resp = NormalizedResponse(
                    status=Status(state=ResponseState.SUCCEEDED),
                    backend=BackendInfo(id="fake-native", type=BackendType.HOSTED_API),
                    document=Document(text=f"native:{fn}"),
                )
                # BL-100: meter each succeeded item individually (report_cost/apply_cost_report),
                # matching the platform path's own per-item apply_cost_report call in run_request —
                # never once for the whole batch's job. BL-108: forward `credentials` through so a
                # report_cost() failure's warning is redacted here exactly like every other
                # apply_cost_report call site — this reference fixture is the exact file
                # the openreading.adapters runbook points a future native-batch adapter author to copy.
                out.append(apply_cost_report(self, job, resp, credentials))
        return out


def test_fake_native_satisfies_the_protocol():
    assert isinstance(_FakeNative(), NativeBatchAdapter)
    assert (
        isinstance(_FakeNative(native=None), NativeBatchAdapter) is not False
    )  # still has methods
    # a plain adapter without the methods is not a NativeBatchAdapter
    from tests.fakes import InlineFake

    assert not isinstance(InlineFake(), NativeBatchAdapter)


def _register(monkeypatch, adapter):
    monkeypatch.setitem(BUILTIN_ADAPTERS, "fake-native", lambda: adapter)


def _corpus(tmp_path, n=3):
    d = tmp_path / "c"
    d.mkdir()
    for i in range(n):
        (d / f"doc{i}.pdf").write_bytes(build_sample_pdf())
    return d


def test_dispatch_uses_native_when_declared(tmp_path, monkeypatch):
    _register(monkeypatch, _FakeNative(native="claimed"))
    env = run_batch([str(_corpus(tmp_path))], backend="fake-native")
    schemas.validate_batch_result(env)
    assert env["summary"]["succeeded"] == 3
    assert all(i["transport"] == "native" for i in env["items"])  # native path taken


def test_dispatch_falls_back_to_platform_without_native_grade(tmp_path, monkeypatch):
    _register(monkeypatch, _FakeNative(native=False))  # descriptor.batch.native falsy
    env = run_batch([str(_corpus(tmp_path))], backend="fake-native")
    assert all(i["transport"] == "platform" for i in env["items"])  # platform fan-out


def test_dispatch_falls_back_when_over_max_items(tmp_path, monkeypatch):
    _register(monkeypatch, _FakeNative(native="claimed", max_items=2))
    env = run_batch([str(_corpus(tmp_path, n=3))], backend="fake-native")  # 3 > max_items 2
    assert all(i["transport"] == "platform" for i in env["items"])


def test_native_maps_per_item_errors_and_preserves_order(tmp_path, monkeypatch):
    _register(monkeypatch, _FakeNative(native="claimed", fail_on={"doc1.pdf"}))
    env = run_batch([str(_corpus(tmp_path, n=3))], backend="fake-native")
    states = {i["source"]["relpath"]: i["state"] for i in env["items"]}
    assert states == {"doc0.pdf": "succeeded", "doc1.pdf": "failed", "doc2.pdf": "succeeded"}
    failed = next(i for i in env["items"] if i["state"] == "failed")
    assert failed["error"]["code"] == "native_item_error" and failed["transport"] == "native"
    assert env["status"]["state"] == "partial"
    assert [i["source"]["relpath"] for i in env["items"]] == ["doc0.pdf", "doc1.pdf", "doc2.pdf"]


def test_native_and_platform_are_observationally_equivalent(tmp_path, monkeypatch):
    # M10: same inputs → same envelope shape (states/sources/success count) whether native or
    # platform. Timing may legitimately differ; cost accounting (BL-100) must NOT — both transports
    # meter through the identical adapter.report_cost() per item, so usage/summary.cost_bases carry
    # identical values here (not just "allowed to differ", the old comment's unasserted claim).
    d = _corpus(tmp_path, n=3)
    _register(monkeypatch, _FakeNative(native="claimed"))
    native = run_batch([str(d)], backend="fake-native")
    _register(monkeypatch, _FakeNative(native=False))
    platform = run_batch([str(d)], backend="fake-native")

    def shape(env):
        return [(i["source"]["relpath"], i["state"]) for i in env["items"]]

    assert shape(native) == shape(platform)
    assert native["summary"]["succeeded"] == platform["summary"]["succeeded"] == 3
    assert native["status"]["state"] == platform["status"]["state"] == "succeeded"
    assert {i["transport"] for i in native["items"]} == {"native"}
    assert {i["transport"] for i in platform["items"]} == {"platform"}

    # BL-100: native must not silently drop cost accounting. _FakeNative.report_cost always
    # returns infra_only("page", 1.0) regardless of transport, so a correctly-metered native item
    # is indistinguishable from its platform counterpart: same cost_basis, same pages_processed,
    # no invented cost_usd (infra_only reports no dollar figure — merge_cost_report never invents
    # one, so the key is dropped by to_schema_dict's exclude_none rather than sent as null).
    for env in (native, platform):
        for item in env["items"]:
            usage = item["response"]["usage"]
            assert usage["cost_basis"] == "infra_only"
            assert usage["pages_processed"] == 1
            assert "cost_usd" not in usage
    assert native["summary"]["cost_bases"] == platform["summary"]["cost_bases"] == ["infra_only"]
    assert native["summary"]["pages_processed"] == platform["summary"]["pages_processed"] == 3


def test_native_dispatch_threads_credentials_so_cost_report_warning_is_redacted(
    tmp_path, monkeypatch
):
    # BL-108: _run_native previously called adapter.normalize_many(job, reqs) with no credentials
    # at all, so apply_cost_report's own redaction (BL-93) could never fire on the native-batch
    # path — a report_cost() failure that embeds a resolved secret came back unredacted, unlike
    # every other apply_cost_report call site. Exercised end-to-end through run_batch (not just a
    # direct normalize_many call) to prove ctx.credentials, resolved by the real
    # EnvCredentialBroker from FAKE_NATIVE_API_KEY, actually reaches the adapter.
    secret = "sk-fakenative-leak-0004-must-never-appear"  # noqa: S105 - test fixture, not a real key
    monkeypatch.setenv("FAKE_NATIVE_API_KEY", secret)
    _register(monkeypatch, _FakeNative(native="claimed", leak_secret_in_cost=secret))
    env = run_batch([str(_corpus(tmp_path, n=1))], backend="fake-native")
    schemas.validate_batch_result(env)
    item = env["items"][0]
    assert item["state"] == "succeeded"  # report_cost failing degrades, never fails the item
    warnings = item["response"]["warnings"]
    assert warnings and warnings[0]["code"] == "cost_unavailable"
    assert secret not in warnings[0]["message"]
    assert "***" in warnings[0]["message"]


def test_native_skips_are_still_honored(tmp_path, monkeypatch):
    d = tmp_path / "c"
    d.mkdir()
    (d / "ok.pdf").write_bytes(build_sample_pdf())
    (d / "no.docx").write_bytes(b"not a pdf")  # unsupported for fake-native (input_formats=[pdf])
    _register(monkeypatch, _FakeNative(native="claimed"))
    env = run_batch([str(d)], backend="fake-native")
    states = {i["source"]["relpath"]: i["state"] for i in env["items"]}
    assert states == {"no.docx": "skipped", "ok.pdf": "succeeded"}
    # the succeeded item went native; the skip never reached the adapter
    ok = next(i for i in env["items"] if i["state"] == "succeeded")
    assert ok["transport"] == "native"


# --- BL-106: _run_native's submit_many→normalize_many block had no crash guard -----------------


def test_run_native_adapter_error_passes_through_unchanged(tmp_path, monkeypatch):
    # The five _ADAPTER_ERRORS taxonomy types (raised by well-behaved adapter code) must keep their
    # own specific handling downstream, exactly like run_request's identical `except AdapterError:
    # raise` clause — proving the new guard doesn't accidentally re-wrap an already-well-formed
    # AdapterError into a generic TerminalError(str(e)), which would lose e.g. backend_code.
    boom = TerminalError("native submit rejected", backend_code="native_boom")
    _register(monkeypatch, _FakeNative(native="claimed", submit_many_error=boom))
    with pytest.raises(TerminalError) as exc:
        run_batch([str(_corpus(tmp_path))], backend="fake-native")
    assert exc.value is boom  # same instance, not reconstructed
    assert exc.value.backend_code == "native_boom"


def test_run_native_wraps_a_plain_submit_many_crash_as_terminal_error(tmp_path, monkeypatch):
    # _run_native's `with auth_hinted(...): submit_many → run_to_completion → normalize_many` block
    # had no try/except of any kind — the sixth BL-99-class call site. A plain, non-AdapterError
    # crash out of submit_many used to propagate straight out of run_batch(), discarding the entire
    # batch's results (not just the offending item's). Matches run_request's own post-BL-99
    # behavior: converted to TerminalError, never a bare KeyError/IndexError/ValueError/
    # AttributeError.
    _register(
        monkeypatch,
        _FakeNative(native="claimed", submit_many_error=ValueError("boom in submit_many")),
    )
    with pytest.raises(TerminalError) as exc:
        run_batch([str(_corpus(tmp_path))], backend="fake-native")
    assert "boom in submit_many" in str(exc.value)


def test_run_native_wraps_a_plain_normalize_many_crash_as_terminal_error(tmp_path, monkeypatch):
    # The concrete trigger BL-106 names: AnthropicClaudeAdapter.normalize_many's per-item
    # self.normalize(synth, req) call (adapter.py:430) is unguarded — a per-item mapping bug there
    # raises a plain exception, not an AdapterError. Pinned here against the fake so the guard is
    # proven independent of the live adapter's own fixtures.
    _register(
        monkeypatch,
        _FakeNative(native="claimed", normalize_many_error=IndexError("boom in normalize_many")),
    )
    with pytest.raises(TerminalError) as exc:
        run_batch([str(_corpus(tmp_path))], backend="fake-native")
    assert "boom in normalize_many" in str(exc.value)


def test_run_native_crash_redacts_a_secret_bearing_credential(tmp_path, monkeypatch):
    # auth_hinted was already widened (BL-99) to redact ctx.credentials' secret values out of any
    # plain-exception message, and _run_native already passes ctx.credentials into auth_hinted — so
    # this pins that redaction stays intact now that the crash is also converted into a structured
    # TerminalError, rather than merely proving redaction happens somewhere before an uncaught
    # crash.
    canary = "sk_CANARY_bl106_native_plain"
    crash = ValueError(f"malformed per-item mapping, saw key={canary}")
    adapter = _FakeNative(
        native="claimed", normalize_many_error=crash, required_env=["CANARY_BL106_NATIVE_KEY"]
    )
    _register(monkeypatch, adapter)
    broker = EnvCredentialBroker({"CANARY_BL106_NATIVE_KEY": canary})
    with pytest.raises(TerminalError) as exc:
        run_batch([str(_corpus(tmp_path))], backend="fake-native", broker=broker)
    assert canary not in str(exc.value)
    assert "***" in str(exc.value)


# --- BL-135: native-batch dispatch's own configurable deadline ---------------------------------
#
# Before this fix, `_run_native`'s `build_run_context` call had no way to receive anything but the
# generic, hardcoded `DEFAULT_DEADLINE_MS` (120s) — the one real adapter implementing this protocol
# (anthropic_claude) documents its own typical completion as "most <1h", so an ordinary,
# non-adversarial native-batch call was expected to hit that wall on essentially every invocation
# that wasn't trivially small, with no override anywhere. The wiring tests below spy on
# `api.build_run_context` (the one factory `_run_native` calls) to pin exactly what `deadline_ms`
# it receives, independent of any real polling; the two behavioral tests then prove the value
# actually governs the drive loop, via the same FakeClock + stuck-RUNNING-job pattern
# `tests/test_cli.py::test_parse_batch_native_deadline_exceeded_exits_3_clean` already established.


def _spy_on_build_run_context(monkeypatch, seen: dict):
    """Wrap api.build_run_context (the name `_run_native` resolves at call time) so `seen` records
    the `deadline_ms` every caller passed, while still delegating to the real implementation —
    never a stand-in that could hide a wiring bug in what happens AFTER the value is received."""
    import openreading.api as api_module

    real = api_module.build_run_context

    def spy(req, descriptor, *, broker=None, deadline_ms=None):
        seen["deadline_ms"] = deadline_ms
        return real(req, descriptor, broker=broker, deadline_ms=deadline_ms)

    monkeypatch.setattr(api_module, "build_run_context", spy)


def test_run_native_deadline_defaults_to_the_native_batch_constant_not_the_generic_one(
    tmp_path, monkeypatch
):
    from openreading.credentials import DEFAULT_NATIVE_BATCH_DEADLINE_MS

    seen: dict = {}
    _spy_on_build_run_context(monkeypatch, seen)
    _register(monkeypatch, _FakeNative(native="claimed"))
    run_batch([str(_corpus(tmp_path, n=1))], backend="fake-native")
    assert seen["deadline_ms"] == DEFAULT_NATIVE_BATCH_DEADLINE_MS


def test_run_batch_deadline_ms_override_reaches_native_dispatchs_build_run_context(
    tmp_path, monkeypatch
):
    seen: dict = {}
    _spy_on_build_run_context(monkeypatch, seen)
    _register(monkeypatch, _FakeNative(native="claimed"))
    run_batch([str(_corpus(tmp_path, n=1))], backend="fake-native", deadline_ms=42_000)
    assert seen["deadline_ms"] == 42_000


def test_run_native_deadline_ms_override_can_fail_faster_than_the_native_default(
    tmp_path, monkeypatch
):
    # A caller who wants to fail fast (rather than wait up to the native-batch default) can lower
    # the deadline below even the OLD generic 120s constant — proving the override genuinely
    # governs the drive loop, not merely a value recorded and then ignored.
    import openreading.api as api_module
    from openreading.router.clock import FakeClock
    from openreading.types.errors import RetryableError

    class _StuckRunningNative(_FakeNative):
        def submit_many(self, reqs, ctx) -> Job:
            return Job(
                id="jb-stuck",
                backend_id="fake-native",
                wait_mode=WaitMode.POLL,
                state=JobState.RUNNING,
            )

        def poll(self, job, ctx):
            return job  # never advances past RUNNING — only the deadline check ends the loop

    monkeypatch.setattr(api_module, "RealClock", FakeClock)
    _register(monkeypatch, _StuckRunningNative(native="claimed"))
    with pytest.raises(RetryableError) as exc:
        run_batch([str(_corpus(tmp_path, n=1))], backend="fake-native", deadline_ms=1_000)
    assert "deadline exceeded" in str(exc.value)


# --- BL-138: the applied-deadline site (api.py's `clock.now_ms() + (ctx.deadline_ms or ...)`) -----
#
# The test above proves the override reaches the drive loop at all (1_000, truthy, was never at
# risk from this bug). These two prove the specific boundary BL-138 fixes: `ctx.deadline_ms=0` is
# the single most natural "fail fast, no budget left" value a caller would pass deliberately, and
# is exactly the value Python's `or` treats as unset. `deadline_ms=-1_000` (already correctly
# applied today — truthy, so it never hit the buggy line — per the sprint-22 Noor x Trent 1:1) is
# pinned here as a permanent regression rather than a fact that lives only in a meeting record.


def test_run_native_deadline_ms_zero_fails_fast_not_the_native_default(tmp_path, monkeypatch):
    """`ctx.deadline_ms=0` must not be silently upgraded to DEFAULT_NATIVE_BATCH_DEADLINE_MS (1h)
    by `ctx.deadline_ms or DEFAULT_NATIVE_BATCH_DEADLINE_MS` truthiness (0 is falsy) at the applied
    -deadline call site — distinct from build_run_context's own already-correct `is not None`
    resolution, which is what `ctx.deadline_ms` itself reads regardless of this bug.

    Not literally zero poll() calls, unlike the negative-value test below: `await_result`'s deadline
    check is `clock.now_ms() > deadline_ms` (strict `>`, unchanged by this fix — out of scope). With
    a frozen FakeClock, the applied deadline (`clock.now_ms() + 0`) equals "now" at the instant it's
    computed, so the loop's very first check (before any poll()) sees `now == deadline`, not `now >
    deadline` — poll() legitimately fires once more before its own progress-guard backoff sleep
    advances the clock past the deadline for the *second* check to catch it. Empirically verified
    against the real, unmodified driver: exactly 2 poll() calls, not thousands (the bug would need
    ~3,600,000ms of virtual time at the loop's flat 500ms progress-guard cadence — ~7,200 polls —
    before expiring)."""
    import openreading.api as api_module
    from openreading.router.clock import FakeClock
    from openreading.types.errors import RetryableError

    class _StuckRunningNative(_FakeNative):
        def __init__(self):
            super().__init__(native="claimed")
            self.poll_calls = 0

        def submit_many(self, reqs, ctx) -> Job:
            return Job(
                id="jb-stuck-zero",
                backend_id="fake-native",
                wait_mode=WaitMode.POLL,
                state=JobState.RUNNING,
            )

        def poll(self, job, ctx):
            self.poll_calls += 1
            return job  # never advances past RUNNING — only the deadline check ends the loop

    monkeypatch.setattr(api_module, "RealClock", FakeClock)
    fake = _StuckRunningNative()
    _register(monkeypatch, fake)
    with pytest.raises(RetryableError) as exc:
        run_batch([str(_corpus(tmp_path, n=1))], backend="fake-native", deadline_ms=0)
    assert "deadline exceeded" in str(exc.value)
    # Bounded to a couple of iterations, not the thousands a truthiness-upgraded 1h budget would
    # take — the drive loop's own boundary mechanics (see docstring above), not an unbounded wait.
    assert fake.poll_calls <= 2


def test_run_native_deadline_ms_negative_fails_fast_with_zero_polls(tmp_path, monkeypatch):
    """A negative `ctx.deadline_ms` is truthy, so it already survived the buggy line unmolested
    before this fix (the fix is a semantic no-op for any truthy value). Pinned here per the
    sprint-22 Noor x Trent 1:1's live confirmation: `clock.now_ms() + negative_value` is already
    strictly in the past the instant it's computed, so the drive loop's very first deadline check —
    before either the WEBHOOK or POLL dispatch branch runs — catches it with poll() never called."""
    import openreading.api as api_module
    from openreading.router.clock import FakeClock
    from openreading.types.errors import RetryableError

    class _StuckRunningNative(_FakeNative):
        def __init__(self):
            super().__init__(native="claimed")
            self.poll_calls = 0

        def submit_many(self, reqs, ctx) -> Job:
            return Job(
                id="jb-stuck-negative",
                backend_id="fake-native",
                wait_mode=WaitMode.POLL,
                state=JobState.RUNNING,
            )

        def poll(self, job, ctx):
            self.poll_calls += 1
            return job  # never advances past RUNNING — only the deadline check ends the loop

    monkeypatch.setattr(api_module, "RealClock", FakeClock)
    fake = _StuckRunningNative()
    _register(monkeypatch, fake)
    with pytest.raises(RetryableError) as exc:
        run_batch([str(_corpus(tmp_path, n=1))], backend="fake-native", deadline_ms=-1_000)
    assert "deadline exceeded" in str(exc.value)
    assert fake.poll_calls == 0


def test_run_native_default_deadline_survives_past_the_old_120s_wall(tmp_path, monkeypatch):
    # BL-135's own regression: a native-batch job still legitimately RUNNING at ~130s of virtual
    # time — past the OLD hardcoded DEFAULT_DEADLINE_MS (120s), comfortably under the NEW
    # DEFAULT_NATIVE_BATCH_DEADLINE_MS (1h) — now succeeds with NO deadline_ms override at all.
    # Before this fix, this exact scenario raised "deadline exceeded" (RetryableError) on every
    # invocation that wasn't trivially fast, per the finding's own reproduction. A real 130s
    # wall-clock wait is avoided via FakeClock (router/clock.py's own injectable-clock double,
    # sleep() advances virtual time instead of blocking) — the deadline math is still crossed for
    # real, through the real, unmodified `_run_native` and driver.await_result.
    import openreading.api as api_module
    from openreading.router.clock import FakeClock

    class _SlowRunningNative(_FakeNative):
        def __init__(self):
            super().__init__(native="claimed")
            self._polls = 0

        def submit_many(self, reqs, ctx) -> Job:
            return Job(
                id="jb-slow",
                backend_id="fake-native",
                wait_mode=WaitMode.POLL,
                state=JobState.RUNNING,
            )

        def poll(self, job, ctx):
            self._polls += 1
            # driver.await_result's progress-guard backoff floor is 500ms/poll (no retry_after,
            # no next_poll_at set by this fake) — ~300 polls is ~150s of virtual time: past the
            # old 120s wall, nowhere near the new ~3600s one.
            if self._polls >= 300:
                job.state = JobState.SUCCEEDED
                job.raw = RawResult(payload={"n": 1})
            return job

    monkeypatch.setattr(api_module, "RealClock", FakeClock)
    _register(monkeypatch, _SlowRunningNative())
    env = run_batch([str(_corpus(tmp_path, n=1))], backend="fake-native")
    schemas.validate_batch_result(env)
    assert env["summary"]["succeeded"] == 1
    assert env["items"][0]["transport"] == "native"
