"""Signals on the CLI: what a scheduler's stop signal does to a run, and what it leaves behind.

Ctrl-C has always been caught (`KeyboardInterrupt` → exit 6 → "resumable" + a run id, and the
ledger's in-flight step recorded `cancelled` by asyncio's own cancellation path). SIGTERM — the
signal systemd, Kubernetes, a cron timeout wrapper and a cancelled CI job all send — had no
handler at all, so the process died at 143 with nothing on either stream, no run id to resume
from, and an orphan `attempted` record that re-dispatches (and re-bills) on the next resume.

The fix is deliberately a mirror rather than a second code path: SIGTERM raises `KeyboardInterrupt`
so every already-tested consequence of Ctrl-C (the exit code, the message, the cancelled record)
happens identically. That also means these tests are the only place the two are pinned as equal.

An inherited `SIG_IGN` is honoured, not overridden — a parent that shielded this process from a
signal (`nohup`, a shell's asynchronous `&` job, a supervisor that masks) is stating a contract,
and a program that reinstalls a handler over it breaks the shield for everyone downstream.

Half of what is pinned here is about WHERE the interrupt is raised, not whether. An interrupt
raised inside asyncio's own loop bookkeeping strands the loop, and the `RuntimeError` that falls
out of the teardown replaces the interrupt — so a stop signal reports itself as a parse failure
at exit 1. `openreading.cli.app._terminate_as_interrupt` carries the mechanism; the two rules it
follows -- claim the stop at most once across BOTH signals, and hand it to `asyncio.Runner`'s own
SIGINT handler while one is installed -- each have a test below, because each one alone leaves the
other half of the race open. The first rule is per-stop, not per-signal-kind, and the tests say so
in both orderings: deduping SIGTERM against SIGTERM leaves the mixed pair reproducing the original
crash, since asyncio counts interrupts of its own and raises out of the second one.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from openreading.cli.app import main

_EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "john_smith_1000_2026_01.pdf"


def test_sigterm_handler_is_installed_for_the_duration_of_a_command(monkeypatch):
    seen: list[object] = []

    def _probe(args):
        seen.append(signal.getsignal(signal.SIGTERM))
        return 0

    before = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr("openreading.cli.app.cmd_backends", _probe)
    assert main(["backends"]) == 0
    assert seen and callable(seen[0]) and seen[0] is not before
    # and it is put back: a library caller's own disposition survives main()
    assert signal.getsignal(signal.SIGTERM) is before


def test_sigterm_handler_raises_keyboard_interrupt():
    # the mirror itself — SIGTERM must arrive at the same handler Ctrl-C does
    from openreading.cli.app import _terminate_as_interrupt

    with _terminate_as_interrupt():
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)


def _sigterm_inside_the_loop_startup_window(monkeypatch) -> list[bool]:
    """Arrange for one real SIGTERM to land inside asyncio's loop-startup window, and report
    whether it was actually delivered there.

    The window is CPython's, not ours: `BaseEventLoop.run_forever` calls `_run_forever_setup()`
    OUTSIDE its own `try`, and that setup marks the loop running (`_thread_id`) several statements
    before the `try` is entered. An exception raised in between therefore skips
    `_run_forever_cleanup()` altogether, and the loop stays `is_running()` for the rest of the
    process — so `asyncio.run`'s own teardown dies in `loop.close()` with `RuntimeError: Cannot
    close a running event loop`.

    `sys.set_asyncgen_hooks` is one of the calls inside that window, so hooking it delivers the
    signal at exactly the hostile instant instead of waiting to hit a sub-microsecond window by
    luck. The returned list is asserted on so the test can never pass vacuously: if a future
    CPython stops calling the hook there, the list stays empty and the test says so rather than
    quietly proving nothing.
    """
    delivered: list[bool] = []
    real_hooks = sys.set_asyncgen_hooks

    def _hooks(*args, **kwargs):
        if not delivered:
            delivered.append(True)
            signal.raise_signal(signal.SIGTERM)
        return real_hooks(*args, **kwargs)

    monkeypatch.setattr(sys, "set_asyncgen_hooks", _hooks)
    return delivered


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_sigterm_during_asyncio_loop_startup_still_exits_143(monkeypatch):
    """One interrupt, raised in the wrong frame, is enough: it corrupts the loop's own
    running/closed bookkeeping, and the `RuntimeError` that falls out of the teardown REPLACES the
    `KeyboardInterrupt`. Everything keyed on the interrupt is then bypassed — no exit 143, no exit
    6, no run id, no one-line message; the caller gets `[strategy:<name>] error: RuntimeError:
    Cannot close a running event loop` and exit 1, which reads like the document failed to parse
    rather than like a stop signal.

    This is the loop-STARTUP half of the window (the duplicate-signal test below covers the
    cleanup half, which is the shape the flake took in the field). A stop signal is the one input
    a scheduler always sends, so the exit code it produces has to be the same one every time.
    Pinning it here rather than in a timing sweep keeps the guard in the offline suite: the window
    is deterministic once you know where it is."""
    from openreading.cli.app import _terminate_as_interrupt

    delivered = _sigterm_inside_the_loop_startup_window(monkeypatch)

    async def _work():
        await asyncio.sleep(0)

    with pytest.raises(SystemExit) as exc, _terminate_as_interrupt():
        asyncio.run(_work())

    assert delivered, "the signal never reached the window — this test proved nothing"
    assert exc.value.code == 143


def test_a_forwarded_duplicate_sigterm_is_never_raised_into_the_unwinding():
    """The measured flake: `uv run` (and tini, and any supervisor whose `killpg` reaches both a
    wrapper and the process it wraps) forwards the stop signal, so the process is signalled twice
    for one stop. The first interrupt is already unwinding asyncio's event loop by the time the
    duplicate lands, and the frame it lands in is `_run_forever_cleanup` — which is where the loop
    would have been unmarked as running. Raising there strands the loop and turns the run's exit
    into `RuntimeError: Cannot close a running event loop` at exit 1.

    So the interrupt is raised at most once, and a duplicate returns quietly. Escalation is not
    lost: a supervisor that means it sends SIGKILL, which no handler can intercept."""
    from openreading.cli.app import _terminate_as_interrupt

    with _terminate_as_interrupt():
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)
        assert handler(signal.SIGTERM, None) is None  # the same stop, arriving again
        assert handler(signal.SIGTERM, None) is None


def test_a_ctrl_c_that_was_not_our_sigterm_passes_through_untouched():
    """Ledger law L1's zero-delta, at this seam: an interrupt that did NOT come from our SIGTERM
    handler is re-raised exactly as it arrived, so an unarmed Ctrl-C still ends in the ordinary
    `KeyboardInterrupt` (traceback, 130) it always did. Only a signal this handler saw is allowed
    to become the 143 exit."""
    from openreading.cli.app import _terminate_as_interrupt

    with pytest.raises(KeyboardInterrupt), _terminate_as_interrupt():
        raise KeyboardInterrupt


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_sigterm_delegates_to_asyncio_runners_own_sigint_handler():
    """Whoever holds SIGINT holds the interrupt-safe path — for a run, that is `asyncio.Runner`'s
    own handler, which cancels the walk's task instead of throwing through the loop's internals.
    Delegating there is both the safer raise and the more faithful mirror, since it is the exact
    handler Ctrl-C reaches.

    The delegation is pinned against the REAL handler rather than a stand-in, because the
    production check recognises that handler specifically (see the third-party test below) and a
    stand-in would let the recogniser rot without a failing test."""
    from openreading.cli.app import _terminate_as_interrupt

    seen: list[object] = []
    before_int = signal.getsignal(signal.SIGINT)

    async def _work():
        seen.append(signal.getsignal(signal.SIGINT))  # asyncio.Runner's, installed for this run
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        assert handler(signal.SIGTERM, None) is None  # delegated, not raised
        seen.append(signal.getsignal(signal.SIGINT))  # and SIGINT is spent from here on
        await asyncio.sleep(30)  # the delegation cancelled this task; never actually waits

    with pytest.raises(SystemExit) as exc, _terminate_as_interrupt():
        asyncio.run(_work())

    assert exc.value.code == 143
    assert len(seen) == 2, "the SIGTERM handler raised instead of delegating"
    assert seen[0] is not seen[1]  # SIGINT was taken over, not left pointing at asyncio's counter
    assert signal.getsignal(signal.SIGINT) is before_int  # and handed back on the way out


def test_sigterm_never_delegates_to_a_third_party_sigint_handler():
    """Delegation is restricted to a handler that is known to stop the process. A SIGINT handler
    that merely records the signal — an embedder, a test harness, a signal-aware library — returns
    without unwinding anything, so handing a supervisor's SIGTERM to it would make the stop a
    silent no-op and the process would keep running. Unrecognised means raise, which always stops.
    """
    from openreading.cli.app import _terminate_as_interrupt

    flag: list[int] = []
    before = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda signum, frame: flag.append(signum))
    try:
        with _terminate_as_interrupt():
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            with pytest.raises(KeyboardInterrupt):
                handler(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGINT, before)
    assert flag == []  # the stop was never handed to a handler that cannot stop anything


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_a_real_sigint_after_our_sigterm_is_never_raised_into_the_unwinding():
    """The mixed-signal half of rule one: SIGTERM, then a responder reaching for Ctrl-C.

    `asyncio.Runner._on_sigint` counts interrupts and, on every call after the first, does
    `raise KeyboardInterrupt()` straight out of the handler — into whatever frame the main thread
    is running, which after a stop has begun is the loop's own teardown. Delegating SIGTERM to
    that handler spends interrupt number one, so the responder's real SIGINT is number two and
    lands in exactly the window rule one exists to keep clear. Measured at roughly 1 run in 160.

    Deduping SIGTERM against SIGTERM does not cover this: the second signal never reaches our
    handler at all. The stop has to be taken over for BOTH signals, so SIGINT is neutralised at
    the moment the stop is claimed."""
    from openreading.cli.app import _terminate_as_interrupt

    seen: list[str] = []

    async def _work():
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)  # the supervisor's stop
        signal.raise_signal(signal.SIGINT)  # the responder's Ctrl-C, milliseconds later
        seen.append("survived")  # unreachable if the second signal raised
        await asyncio.sleep(30)

    with pytest.raises(SystemExit) as exc, _terminate_as_interrupt():
        asyncio.run(_work())

    assert seen == ["survived"], "the second stop signal raised into the frame it was aimed at"
    assert exc.value.code == 143  # still the supervisor's stop, reported as the supervisor's


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_a_sigterm_after_a_real_ctrl_c_is_never_raised_into_the_unwinding():
    """The same mechanism with the signals reversed: Ctrl-C, then a supervisor's SIGTERM.

    Here asyncio's counter is already at one when our handler runs, so delegating drives it to two
    and `_on_sigint` raises inside OUR handler — the same strand, reached from the other side. The
    handler therefore treats a `KeyboardInterrupt` coming back out of the delegation as proof that
    a stop was already in flight, swallows it, and hands ownership of the exit code back to the
    signal that really started the stop: a Ctrl-C on an unarmed run still ends at 130, not 143."""
    from openreading.cli.app import _terminate_as_interrupt

    seen: list[str] = []

    async def _work():
        signal.raise_signal(signal.SIGINT)  # asyncio cancels the task, interrupt count 1
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)  # the supervisor's stop, milliseconds later
        seen.append("survived")  # unreachable if the delegation raised through us
        await asyncio.sleep(30)

    with pytest.raises(KeyboardInterrupt), _terminate_as_interrupt():
        asyncio.run(_work())

    assert seen == ["survived"], "the delegation raised out of the SIGTERM handler"


def test_an_unknown_c_level_sigterm_handler_is_left_alone(monkeypatch):
    """`signal.getsignal` returns `None` when an unknown handler is in effect — one installed from
    C, by an extension module or an embedding host, before `main()` ran. The Python API cannot put
    such a handler back: `signal.signal(SIGTERM, None)` raises `TypeError`. Installing over it
    would therefore destroy it for the rest of the process, and the restore in our own `finally`
    would raise on the way out, replacing whatever was unwinding — inside the one function whose
    entire purpose is to keep an exception out of a teardown path.

    So an unknown handler is treated exactly like an inherited `SIG_IGN`: someone else owns this
    signal, and we leave it alone."""
    from openreading.cli.app import _terminate_as_interrupt

    installed: list[tuple[int, object]] = []

    def _fake_signal(signum, handler):
        # faithful to CPython: anything but a callable, SIG_DFL or SIG_IGN is a TypeError
        if not (callable(handler) or handler in (signal.SIG_DFL, signal.SIG_IGN)):
            raise TypeError(
                "signal handler must be signal.SIG_IGN, signal.SIG_DFL, or a callable object"
            )
        installed.append((signum, handler))

    monkeypatch.setattr(signal, "getsignal", lambda signum: None)
    monkeypatch.setattr(signal, "signal", _fake_signal)

    with _terminate_as_interrupt():
        pass

    assert installed == []  # nothing installed over a disposition we could not hand back


def test_sigterm_still_raises_when_sigint_is_ignored():
    """The delegation must never be able to swallow the signal. `nohup` and a shell's asynchronous
    `&` job both leave SIGINT ignored, and handing SIGTERM to an ignored SIGINT there would make a
    supervisor's stop a silent no-op — a worse failure than the race being fixed."""
    from openreading.cli.app import _terminate_as_interrupt

    before = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        with _terminate_as_interrupt():
            handler = signal.getsignal(signal.SIGTERM)
            assert callable(handler)
            with pytest.raises(KeyboardInterrupt):
                handler(signal.SIGTERM, None)
    finally:
        signal.signal(signal.SIGINT, before)


def test_inherited_sig_ign_is_not_overridden():
    before = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    try:
        from openreading.cli.app import _terminate_as_interrupt

        with _terminate_as_interrupt():
            assert signal.getsignal(signal.SIGTERM) is signal.SIG_IGN
    finally:
        signal.signal(signal.SIGTERM, before)


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_sigterm_mid_run_exits_6_resumable_with_clean_stdout(tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    body = _EXAMPLE.read_bytes()
    for i in range(150):
        (corpus / f"doc_{i:03d}.pdf").write_bytes(body)
    ledger = tmp_path / "ledger"
    ledger.mkdir()

    env = dict(os.environ, OPENREADING_LEDGER=str(ledger))
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "openreading.cli",
            "parse",
            str(corpus),
            "--backend",
            "pymupdf",
            "--jobs",
            "1",
            "--max-items",
            "200",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    # signal only once the run is demonstrably in flight (first progress line), never on a timer
    assert proc.stderr is not None
    deadline = time.monotonic() + 30
    for line in proc.stderr:
        if "succeeded" in line or "failed" in line:
            break
        if time.monotonic() > deadline:  # pragma: no cover - watchdog
            proc.kill()
            pytest.fail("run never produced a progress line")
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=60)

    assert proc.returncode == 6, f"expected the documented interrupted exit, got {proc.returncode}"
    assert "interrupted" in err  # a stop signal says so, on stderr
    assert out == ""  # and never half an envelope on stdout


@pytest.mark.skipif(os.name != "posix", reason="POSIX signals")
def test_sigterm_without_a_ledger_exits_143_with_one_line_and_no_traceback(tmp_path):
    # Unarmed there is nothing to resume, so the mirror stops at the exit code: SIGTERM must not
    # report itself as 130 (SIGINT) through Python's default KeyboardInterrupt unwind, and must not
    # spend a responder's first minute on a traceback for a signal their scheduler sent on purpose.
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    body = _EXAMPLE.read_bytes()
    for i in range(150):
        (corpus / f"doc_{i:03d}.pdf").write_bytes(body)

    env = {k: v for k, v in os.environ.items() if k != "OPENREADING_LEDGER"}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "openreading.cli",
            "parse",
            str(corpus),
            "--backend",
            "pymupdf",
            "--jobs",
            "1",
            "--max-items",
            "200",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        text=True,
    )
    assert proc.stderr is not None
    deadline = time.monotonic() + 30
    for line in proc.stderr:
        if "succeeded" in line or "failed" in line:
            break
        if time.monotonic() > deadline:  # pragma: no cover - watchdog
            proc.kill()
            pytest.fail("run never produced a progress line")
    proc.send_signal(signal.SIGTERM)
    out, err = proc.communicate(timeout=60)

    assert proc.returncode == 143  # 128 + SIGTERM, the code a supervisor checks for
    assert "SIGTERM" in err
    assert "Traceback" not in err
    assert "OPENREADING_LEDGER" in err  # says how to make the next one resumable
    assert out == ""


def test_serve_keeps_its_own_signal_handling(monkeypatch):
    """`serve` is the one command that must NOT get the SIGTERM mirror. uvicorn installs its own
    handlers for a graceful drain and re-raises the captured signal once it has restored whatever
    was there before, so a handler of ours would fire after a clean shutdown and print resume
    advice about a journal a server never arms. Staying out of the way leaves `serve` where it
    already was: drain, then die at 143 under the default disposition."""
    pytest.importorskip("uvicorn", reason="server extra not installed")
    seen: list[object] = []

    def _run(self, sockets=None):
        seen.append(signal.getsignal(signal.SIGTERM))

    monkeypatch.setattr("uvicorn.Server.run", _run)
    before = signal.getsignal(signal.SIGTERM)
    assert main(["serve", "--port", "0"]) == 0
    assert seen == [before]  # untouched for the whole life of the server
