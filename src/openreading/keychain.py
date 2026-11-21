"""The OS keychain as the last place a backend key is looked up, and the one place OpenReading
ever writes a key.

You get a key that works from every directory and every session. `openreading keys set SLUG`
saves it once, and the credential broker (`openreading.credentials`) finds it after the process
environment and `.env` have had their turn. The keychain is the store your operating system
already ships: the macOS Keychain, the Windows Credential Manager, or a Secret Service such as
GNOME Keyring on Linux. Access goes through the `keyring` package, which the `[setup]` extra
installs.

The contract
------------
- Every entry uses service `openreading` (`SERVICE`), with the variable name as the account. For
  example, service `openreading`, account `ANTHROPIC_API_KEY`.
- `KeychainMapping` is the only read path the broker uses. It answers for the names it is given,
  which are the secret variables backends declare (`credentials.declared_secret_envs`), and for
  nothing else. An operator knob such as `OPENREADING_LEDGER` is never looked up, so a setting
  cannot hide in a store nobody reviews.
- Reading never raises and never prints. A missing `[setup]` extra, a refused backend, a locked
  or unreachable keychain, any `keyring` error and a timeout all count as "nothing saved". A CLI
  call on a server without a keychain therefore stays quiet.
- Writing raises `KeychainUnavailable` with a sentence the CLI prints as-is and exits 2 on.
- Read answers, a value or "nothing saved", are cached in this process's memory for
  `CACHE_TTL_S` seconds (60), so one run asks the keychain once per variable. The cache never
  enters `os.environ`. An answer that old is asked again, so a long-running `serve --keychain` or
  `mcp --keychain` sees a later `keys set` or `keys unset` within a minute. `set` and `delete`
  update the cache at once.

Why some backends are refused
-----------------------------
`keyring` falls back to a "fail" or null backend when no keychain runs, which is the normal state
of a headless Linux server. The `keyrings.alt` package adds file backends that keep the key in
clear text or behind a fixed key. Core refuses all of them (`_acceptable`). A "saved securely"
message over a key written to disk in clear text is the failure this avoids. A chained backend
is unwrapped to its best acceptable member, because the chainer writes to the next member when
one fails, and the next member can be a plaintext file.

Why every call has a timeout
----------------------------
On Linux without a desktop session, `keyring` can block while it waits for a Secret Service that
never answers. Each read runs in a daemon thread and is abandoned after `TIMEOUT_S` seconds (2).
A timeout pauses every read for `READ_COOLDOWN_S` seconds (60), so one lookup over many names
costs at most one timeout. The pause ends on its own, because a locked keychain that made one
read wait can answer the next minute. During a timeout or a pause, a name read before keeps its
last answer, even an expired one, so a `serve --keychain` that held a key does not lose it to one
slow refresh. An abandoned read still records its answer when it arrives, unless a `set` or
`delete` for that name came first. A write ignores the pause and waits up to
`WRITE_TIMEOUT_S` seconds (60), which leaves you time to answer an unlock prompt.

Environment variables this module reads
---------------------------------------
`OPENREADING_KEYCHAIN`: set to `off` and every read returns nothing, while every write raises
`KeychainUnavailable`. Any other value, or unset, leaves the keychain on. The offline test suite
and `make verify` set it, so a developer's real keys never reach a test run.

Relation to its siblings: `openreading.credentials` decides when the keychain is consulted and in
what order. `openreading.cli.keys` is the only writer, and `openreading setup`
(`openreading.cli.setup`) saves through it.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterable, Iterator, MutableMapping
from typing import Any

SERVICE = "openreading"
ENV_SWITCH = "OPENREADING_KEYCHAIN"
TIMEOUT_S = 2.0
WRITE_TIMEOUT_S = 60.0
READ_COOLDOWN_S = 60.0
CACHE_TTL_S = 60.0

# Module prefixes of `keyring` backends that keep a key outside an OS keychain, or nowhere.
_REFUSED_MODULES = ("keyrings.alt", "keyring.backends.fail", "keyring.backends.null")

_UNAVAILABLE_MESSAGE = (
    "No keychain is available on this machine. Use an environment variable or .env instead."
)

# Process-wide state. A read timeout sets `_reads_paused_until`, and `_cache` maps a name to its
# answer and the clock reading after which that answer is asked again. An expired entry stays, so
# a refresh that times out can still serve it. `_generation` counts the answers recorded per
# name, so a read that finishes late can tell whether a newer answer arrived while it waited.
_lock = threading.Lock()
_reads_paused_until = 0.0
_cache: dict[str, tuple[str | None, float]] = {}
_generation: dict[str, int] = {}
_clock = time.monotonic


class KeychainUnavailable(RuntimeError):
    """A write could not reach a usable keychain. `str(exc)` is the sentence to show."""


class _Timeout(Exception):
    pass


def disabled() -> bool:
    """True when `OPENREADING_KEYCHAIN=off` switches the keychain off for this process."""
    return os.environ.get(ENV_SWITCH, "").strip().lower() == "off"


def _acceptable(backend: Any) -> bool:
    """A backend that stores in an OS keychain. Refused: fail, null and `keyrings.alt` file
    backends, and anything whose priority is below 1 (keyring's own "not a real store" signal)."""
    if type(backend).__module__.startswith(_REFUSED_MODULES):
        return False
    try:
        return float(backend.priority) >= 1
    except Exception:
        return False


def _backend() -> Any | None:
    """The keyring backend to use, or None when there is none worth using. Tests replace the
    backend through `keyring.set_keyring`, which this reads back."""
    try:
        import keyring
    except ImportError:
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:
        return None
    members = getattr(backend, "backends", None)
    if members is not None and type(backend).__module__ == "keyring.backends.chainer":
        return next((m for m in members if _acceptable(m)), None)
    return backend if _acceptable(backend) else None


def _call(fn: Callable[[], Any], timeout: float) -> Any:
    """Run `fn` in a daemon thread and give up after `timeout` seconds. A daemon thread cannot
    keep the process alive, so an abandoned call costs nothing at exit."""
    box: dict[str, Any] = {}

    def work() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 - re-raised in the caller's thread
            box["error"] = e

    worker = threading.Thread(target=work, name="openreading-keychain", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise _Timeout
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _reads_paused() -> bool:
    return _clock() < _reads_paused_until


def _remember(name: str, value: str | None, *, since: int | None = None) -> None:
    """Cache `value` for `name`. With `since`, only when no answer was recorded after that
    generation, so a late read never replaces a newer write."""
    with _lock:
        current = _generation.get(name, 0)
        if since is not None and current != since:
            return
        _generation[name] = current + 1
        _cache[name] = (value, _clock() + CACHE_TTL_S)


def _refresh(name: str) -> str | None:
    """Ask the keychain for `name` and cache the answer. Raises `_Timeout` after `TIMEOUT_S`.

    The abandoned worker keeps running, and its answer still reaches the cache when it arrives.
    A locked keychain that answers after the timeout therefore fills the cache for the next read
    instead of wasting the wait."""
    with _lock:
        since = _generation.get(name, 0)

    def lookup() -> str | None:
        try:
            backend = _backend()
            value = None if backend is None else backend.get_password(SERVICE, name)
        except Exception:
            value = None
        value = value if isinstance(value, str) and value else None
        _remember(name, value, since=since)
        return value

    return _call(lookup, TIMEOUT_S)


def _read(name: str, *, timeout: float) -> str | None:
    """Ask the keychain for `name`. Raises `_Timeout` when it does not answer in `timeout`."""

    def lookup() -> str | None:
        backend = _backend()
        return None if backend is None else backend.get_password(SERVICE, name)

    try:
        value = _call(lookup, timeout)
    except _Timeout:
        raise
    except Exception:
        return None
    return value if isinstance(value, str) and value else None


def get(name: str) -> str | None:
    """The saved value of `name`, or None. Never raises, never prints."""
    global _reads_paused_until
    if disabled():
        return None
    with _lock:
        hit = _cache.get(name)
        if hit is not None and _clock() < hit[1]:
            return hit[0]
    # The last answer, even an expired one. A refresh that cannot reach the keychain serves it
    # rather than nothing, because a long-running server that lost a key it held a minute ago
    # fails every request until the keychain answers again.
    last = hit[0] if hit is not None else None
    if _reads_paused():
        return last
    try:
        return _refresh(name)
    except _Timeout:
        with _lock:
            _reads_paused_until = _clock() + READ_COOLDOWN_S
        return last


def _writable_backend() -> Any:
    if disabled():
        raise KeychainUnavailable(
            f"The keychain is off for this process ({ENV_SWITCH}=off). Unset {ENV_SWITCH} to "
            "save a key, or use an environment variable or .env instead."
        )
    try:
        import keyring  # noqa: F401
    except ImportError:
        raise KeychainUnavailable(
            "Saving a key needs the [setup] extra. Install it with "
            "pip install 'openreading[setup]', or use an environment variable or .env instead."
        ) from None
    try:
        backend = _call(_backend, TIMEOUT_S)
    except Exception:
        backend = None
    if backend is None:
        raise KeychainUnavailable(_UNAVAILABLE_MESSAGE)
    return backend


def available() -> bool:
    """True when a write would reach an acceptable keychain. Asks the backend, never a value."""
    try:
        _writable_backend()
    except KeychainUnavailable:
        return False
    return True


def backend_name() -> str:
    """The keychain's everyday name, for a line such as "Saved to the macOS Keychain"."""
    backend = _writable_backend()
    module = type(backend).__module__
    if module.startswith("keyring.backends.macOS"):
        return "macOS Keychain"
    if module.startswith("keyring.backends.Windows"):
        return "Windows Credential Manager"
    if module.startswith(("keyring.backends.SecretService", "keyring.backends.libsecret")):
        return "Secret Service keyring"
    if module.startswith("keyring.backends.kwallet"):
        return "KWallet"
    return str(getattr(backend, "name", type(backend).__name__))


# `set` shadows the builtin inside this module on purpose, so a caller writes `keychain.set(...)`.
# Nothing in this module calls the builtin.
def set(name: str, value: str) -> None:  # noqa: A001 - mirrors keyring's own verb
    """Save `value` under `name`. Raises `KeychainUnavailable` when nothing acceptable is there,
    and never writes anywhere else."""
    backend = _writable_backend()
    try:
        _call(lambda: backend.set_password(SERVICE, name, value), WRITE_TIMEOUT_S)
    except _Timeout:
        raise KeychainUnavailable(_UNAVAILABLE_MESSAGE) from None
    except KeychainUnavailable:
        raise
    except Exception as e:
        # The keyring error class names the failure. Its message can echo arguments, so it is
        # dropped rather than risk printing the value.
        raise KeychainUnavailable(
            f"The keychain refused to save {name} ({type(e).__name__})."
        ) from None
    _remember(name, value)


def delete(name: str) -> bool:
    """Remove `name`. True when an entry was there to remove."""
    backend = _writable_backend()
    try:
        present = _read(name, timeout=WRITE_TIMEOUT_S) is not None
    except _Timeout:
        raise KeychainUnavailable(_UNAVAILABLE_MESSAGE) from None
    if not present:
        _remember(name, None)
        return False
    try:
        _call(lambda: backend.delete_password(SERVICE, name), WRITE_TIMEOUT_S)
    except _Timeout:
        raise KeychainUnavailable(_UNAVAILABLE_MESSAGE) from None
    except Exception as e:
        raise KeychainUnavailable(
            f"The keychain refused to remove {name} ({type(e).__name__})."
        ) from None
    _remember(name, None)
    return True


def _reset() -> None:
    """Forget the cache and a read pause. The test fixture calls this between tests."""
    global _reads_paused_until
    with _lock:
        _reads_paused_until = 0.0
        _cache.clear()
        _generation.clear()


class KeychainMapping(MutableMapping[str, str]):
    """A read-only view of the keychain over a fixed set of names, for `ChainMap`.

    It answers only for names in `allowed` and looks each one up on first use. Iteration yields
    only names already found, because a keychain cannot be enumerated and this view never asks it
    to be. `allowed` may be a callable, so the set of declared secrets is computed on the first
    lookup rather than when a broker is built. Writing through the view raises `TypeError`: a key
    reaches the keychain only through `set`.
    """

    def __init__(self, allowed: Iterable[str] | Callable[[], Iterable[str]]) -> None:
        self._source = allowed
        self._allowed: frozenset[str] | None = None

    def _names(self) -> frozenset[str]:
        if self._allowed is None:
            source = self._source
            self._allowed = frozenset(source() if callable(source) else source)
        return self._allowed

    def __getitem__(self, name: str) -> str:
        if name not in self._names():
            raise KeyError(name)
        value = get(name)
        if value is None:
            raise KeyError(name)
        return value

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._names() and get(name) is not None

    def __iter__(self) -> Iterator[str]:
        now = _clock()
        with _lock:
            found = [n for n, (v, until) in _cache.items() if v is not None and now < until]
        return iter([n for n in found if n in self._names()])

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def __setitem__(self, name: str, value: str) -> None:
        raise TypeError("the keychain view is read-only")

    def __delitem__(self, name: str) -> None:
        raise TypeError("the keychain view is read-only")
