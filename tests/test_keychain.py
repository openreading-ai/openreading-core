"""The OS keychain behind the credential broker (`openreading.keychain`, `openreading.credentials`).

Every test runs against the in-memory fake from `tests/conftest.py` (`fake_keychain`), or with
the keychain switched off, which is the suite's default. Nothing here reaches a real keychain.
The laws under test are PS1 (no saved key, no change), PS2 (the keychain never beats the shell or
`.env`), PS3 (a saved key never enters `os.environ`), PS5 (only declared secrets), PS6 (never a
plaintext fallback), and the lookup timeout.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from openreading import api, credentials, keychain
from openreading.adapters.registry import make_adapter
from openreading.credentials import EnvCredentialBroker, keychain_environ, load_dotenv
from openreading.types.request import OpenReadingRequest

KEY = "sk-ant-keychain-canary-0001"


def _req(slug: str) -> OpenReadingRequest:
    return OpenReadingRequest.model_validate(
        {"document": {"path": "/x.pdf"}, "backend": {"id": slug}}
    )


def _api_key(broker: EnvCredentialBroker, slug: str = "anthropic-claude") -> object:
    desc = make_adapter(slug).descriptor
    return broker.resolve(desc, _req(slug)).values.get("api_key")


def _save(fake, name: str, value: str) -> None:
    fake.store[("openreading", name)] = value


# --- the module itself ---------------------------------------------------------------------------


def test_off_by_default_in_the_suite_reads_nothing_and_refuses_writes():
    assert os.environ["OPENREADING_KEYCHAIN"] == "off"
    assert keychain.get("ANTHROPIC_API_KEY") is None
    assert not keychain.available()
    with pytest.raises(keychain.KeychainUnavailable, match="OPENREADING_KEYCHAIN=off"):
        keychain.set("ANTHROPIC_API_KEY", KEY)


def test_set_get_delete_round_trip(fake_keychain):
    keychain.set("ANTHROPIC_API_KEY", KEY)
    assert fake_keychain.store[("openreading", "ANTHROPIC_API_KEY")] == KEY
    assert keychain.get("ANTHROPIC_API_KEY") == KEY
    assert keychain.delete("ANTHROPIC_API_KEY") is True
    assert keychain.get("ANTHROPIC_API_KEY") is None
    assert keychain.delete("ANTHROPIC_API_KEY") is False


def test_reads_are_cached_per_process(fake_keychain):
    _save(fake_keychain, "REDUCTO_API_KEY", "r1")
    assert keychain.get("REDUCTO_API_KEY") == "r1"
    assert keychain.get("REDUCTO_API_KEY") == "r1"
    assert fake_keychain.calls.count(("get", "REDUCTO_API_KEY")) == 1


def test_backend_name_falls_back_to_the_backend_name(fake_keychain):
    assert keychain.backend_name()


@pytest.mark.parametrize(
    ("module", "label"),
    [
        ("keyring.backends.macOS", "macOS Keychain"),
        ("keyring.backends.Windows", "Windows Credential Manager"),
        ("keyring.backends.SecretService", "Secret Service keyring"),
        ("keyring.backends.kwallet", "KWallet"),
    ],
)
def test_backend_name_uses_the_everyday_name(monkeypatch, fake_keychain, module, label):
    type(fake_keychain).__module__ = module
    try:
        assert keychain.backend_name() == label
    finally:
        type(fake_keychain).__module__ = __name__


def test_mapping_answers_only_for_allowed_names_and_is_read_only(fake_keychain):
    _save(fake_keychain, "REDUCTO_API_KEY", "r1")
    _save(fake_keychain, "OPENREADING_LEDGER", "/tmp/should-never-be-read")
    view = keychain.KeychainMapping(["REDUCTO_API_KEY"])
    assert view["REDUCTO_API_KEY"] == "r1"
    assert "OPENREADING_LEDGER" not in view
    with pytest.raises(KeyError):
        view["OPENREADING_LEDGER"]
    assert ("get", "OPENREADING_LEDGER") not in fake_keychain.calls
    assert list(view) == ["REDUCTO_API_KEY"] and len(view) == 1
    with pytest.raises(TypeError):
        view["REDUCTO_API_KEY"] = "x"
    with pytest.raises(TypeError):
        del view["REDUCTO_API_KEY"]


def test_a_keyring_error_on_read_is_nothing_saved(fake_keychain, monkeypatch):
    def boom(*_):
        raise RuntimeError("locked")

    monkeypatch.setattr(fake_keychain, "get_password", boom)
    assert keychain.get("REDUCTO_API_KEY") is None


def test_a_keyring_error_on_write_names_the_variable_never_the_value(fake_keychain, monkeypatch):
    def boom(service, name, value):
        raise RuntimeError(f"cannot store {value}")

    monkeypatch.setattr(fake_keychain, "set_password", boom)
    with pytest.raises(keychain.KeychainUnavailable) as caught:
        keychain.set("REDUCTO_API_KEY", KEY)
    assert "REDUCTO_API_KEY" in str(caught.value) and KEY not in str(caught.value)

    _save(fake_keychain, "PULSE_API_KEY", "p")
    monkeypatch.setattr(fake_keychain, "delete_password", boom)
    with pytest.raises(keychain.KeychainUnavailable, match="PULSE_API_KEY"):
        keychain.delete("PULSE_API_KEY")


# --- PS6: never a plaintext fallback ------------------------------------------------------------


def _refused_backends():
    import keyring.backends.fail
    import keyring.backends.null

    return [keyring.backends.fail.Keyring(), keyring.backends.null.Keyring()]


@pytest.mark.parametrize("index", [0, 1])
def test_fail_and_null_backends_are_refused(monkeypatch, fake_keychain, index):
    import keyring.core

    monkeypatch.setattr(keyring.core, "_keyring_backend", _refused_backends()[index])
    assert keychain.get("ANTHROPIC_API_KEY") is None
    assert not keychain.available()
    with pytest.raises(keychain.KeychainUnavailable, match="No keychain is available"):
        keychain.set("ANTHROPIC_API_KEY", KEY)


def test_a_plaintext_file_backend_is_refused(monkeypatch, fake_keychain):
    import keyring.core

    plaintext = type(fake_keychain)()
    type(plaintext).__module__ = "keyrings.alt.file"
    try:
        monkeypatch.setattr(keyring.core, "_keyring_backend", plaintext)
        with pytest.raises(keychain.KeychainUnavailable):
            keychain.set("ANTHROPIC_API_KEY", KEY)
        assert plaintext.store == {}
    finally:
        type(plaintext).__module__ = __name__


def test_a_low_priority_backend_is_refused(monkeypatch, fake_keychain):
    monkeypatch.setattr(type(fake_keychain), "priority", 0.5)
    assert not keychain.available()


def test_a_chain_is_unwrapped_to_its_best_acceptable_member(monkeypatch, fake_keychain):
    import keyring.core

    class _Chain:
        backends = [_refused_backends()[0], fake_keychain]

    _Chain.__module__ = "keyring.backends.chainer"
    monkeypatch.setattr(keyring.core, "_keyring_backend", _Chain())
    keychain.set("ANTHROPIC_API_KEY", KEY)
    assert fake_keychain.store[("openreading", "ANTHROPIC_API_KEY")] == KEY


def test_without_the_setup_extra_reads_are_empty_and_writes_name_the_extra(
    monkeypatch, fake_keychain
):
    _save(fake_keychain, "ANTHROPIC_API_KEY", KEY)
    monkeypatch.setitem(sys.modules, "keyring", None)
    assert keychain.get("ANTHROPIC_API_KEY") is None
    with pytest.raises(keychain.KeychainUnavailable, match=r"\[setup\] extra"):
        keychain.set("ANTHROPIC_API_KEY", KEY)


def test_get_keyring_raising_counts_as_no_keychain(monkeypatch, fake_keychain):
    import keyring

    def boom():
        raise RuntimeError("no dbus")

    monkeypatch.setattr(keyring, "get_keyring", boom)
    assert keychain.get("ANTHROPIC_API_KEY") is None
    assert not keychain.available()


# --- timeout --------------------------------------------------------------------------------------


class _Clock:
    """A monotonic clock the test moves by hand, installed as `keychain._clock`."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(keychain, "_clock", fake)
    return fake


def test_a_blocking_keychain_gives_up_and_pauses_reads_for_a_cooldown(
    monkeypatch, fake_keychain, clock
):
    monkeypatch.setattr(keychain, "TIMEOUT_S", 0.2)
    _save(fake_keychain, "ANTHROPIC_API_KEY", KEY)
    fake_keychain.block.set()
    started = time.monotonic()
    assert keychain.get("ANTHROPIC_API_KEY") is None
    assert time.monotonic() - started < 1.5
    calls = len(fake_keychain.calls)
    # One timeout pauses every read, so a second name costs nothing while the pause lasts.
    assert keychain.get("REDUCTO_API_KEY") is None
    assert len(fake_keychain.calls) == calls
    # The pause is not forever: once the keychain answers again, reads resume.
    fake_keychain.block.clear()
    clock.now += keychain.READ_COOLDOWN_S + 1
    assert keychain.get("ANTHROPIC_API_KEY") == KEY


def _wait_for_abandoned_reads(timeout: float = 5.0) -> None:
    import threading

    deadline = time.monotonic() + timeout
    while any(t.name == "openreading-keychain" and t.is_alive() for t in threading.enumerate()):
        assert time.monotonic() < deadline, "the abandoned read never finished"
        time.sleep(0.01)


def test_timeout_after_a_cached_hit_returns_the_cached_value(monkeypatch, fake_keychain, clock):
    """A `serve --keychain` that once read a key keeps it when a later refresh hangs."""
    monkeypatch.setattr(keychain, "TIMEOUT_S", 0.2)
    _save(fake_keychain, "ANTHROPIC_API_KEY", KEY)
    assert keychain.get("ANTHROPIC_API_KEY") == KEY
    clock.now += keychain.CACHE_TTL_S + 1
    fake_keychain.block.set()
    assert keychain.get("ANTHROPIC_API_KEY") == KEY  # the refresh timed out
    calls = len(fake_keychain.calls)
    assert keychain.get("ANTHROPIC_API_KEY") == KEY  # during the cooldown, no new read
    assert len(fake_keychain.calls) == calls
    assert keychain.get("REDUCTO_API_KEY") is None  # never known, so nothing to serve


def test_a_keyring_error_after_a_cached_hit_keeps_the_key_and_caches_nothing(
    monkeypatch, fake_keychain, clock
):
    """A locked keychain that raised once used to cache None over a held key, so a
    `serve --keychain` lost a working key until the TTL ended."""
    import keyring.errors

    _save(fake_keychain, "ANTHROPIC_API_KEY", KEY)
    assert keychain.get("ANTHROPIC_API_KEY") == KEY
    clock.now += keychain.CACHE_TTL_S + 1
    real = fake_keychain.get_password

    def locked(*_):
        raise keyring.errors.KeyringLocked("locked")

    monkeypatch.setattr(fake_keychain, "get_password", locked)
    assert keychain.get("ANTHROPIC_API_KEY") == KEY
    assert keychain.get("REDUCTO_API_KEY") is None  # never known, so nothing to serve
    # Recovered one second later: the next read asks again instead of serving a cached None.
    clock.now += 1
    monkeypatch.setattr(fake_keychain, "get_password", real)
    _save(fake_keychain, "ANTHROPIC_API_KEY", "sk-ant-rotated")
    _save(fake_keychain, "REDUCTO_API_KEY", "r1")
    assert keychain.get("ANTHROPIC_API_KEY") == "sk-ant-rotated"
    assert keychain.get("REDUCTO_API_KEY") == "r1"


def test_a_late_read_result_updates_the_cache(monkeypatch, fake_keychain, clock):
    monkeypatch.setattr(keychain, "TIMEOUT_S", 0.2)
    _save(fake_keychain, "ANTHROPIC_API_KEY", KEY)
    fake_keychain.block.set()
    assert keychain.get("ANTHROPIC_API_KEY") is None  # abandoned after the timeout
    fake_keychain.release.set()
    _wait_for_abandoned_reads()
    calls = len(fake_keychain.calls)
    assert keychain.get("ANTHROPIC_API_KEY") == KEY
    assert len(fake_keychain.calls) == calls  # answered from the cache the late read filled


def test_a_late_read_result_never_overwrites_a_newer_write(monkeypatch, fake_keychain, clock):
    monkeypatch.setattr(keychain, "TIMEOUT_S", 0.2)
    _save(fake_keychain, "ANTHROPIC_API_KEY", "old")
    fake_keychain.block.set()
    assert keychain.get("ANTHROPIC_API_KEY") is None
    keychain.set("ANTHROPIC_API_KEY", "new")
    # The abandoned read answers with what the store holds at release. Put the old value back,
    # so a late overwrite of the newer write would show.
    fake_keychain.store[("openreading", "ANTHROPIC_API_KEY")] = "old"
    fake_keychain.release.set()
    _wait_for_abandoned_reads()
    assert keychain.get("ANTHROPIC_API_KEY") == "new"


def test_writes_ignore_a_read_timeout_and_update_the_cache(monkeypatch, fake_keychain, clock):
    monkeypatch.setattr(keychain, "TIMEOUT_S", 0.2)
    _save(fake_keychain, "PULSE_API_KEY", "p")
    fake_keychain.block.set()
    assert keychain.get("ANTHROPIC_API_KEY") is None  # times out, reads pause
    keychain.set("REDUCTO_API_KEY", "r")
    assert fake_keychain.store[("openreading", "REDUCTO_API_KEY")] == "r"
    assert keychain.get("REDUCTO_API_KEY") == "r"  # answered from the cache, no read
    fake_keychain.block.clear()
    assert keychain.delete("PULSE_API_KEY") is True
    assert ("openreading", "PULSE_API_KEY") not in fake_keychain.store
    assert keychain.get("PULSE_API_KEY") is None


def test_cached_answers_expire_so_a_long_running_process_sees_later_changes(fake_keychain, clock):
    """`serve --keychain` runs for days, and `keys set` in another shell must reach it."""
    assert keychain.get("REDUCTO_API_KEY") is None
    _save(fake_keychain, "REDUCTO_API_KEY", "r1")
    assert keychain.get("REDUCTO_API_KEY") is None  # still the cached answer
    clock.now += keychain.CACHE_TTL_S + 1
    assert keychain.get("REDUCTO_API_KEY") == "r1"
    del fake_keychain.store[("openreading", "REDUCTO_API_KEY")]
    clock.now += keychain.CACHE_TTL_S + 1
    assert keychain.get("REDUCTO_API_KEY") is None
    assert "REDUCTO_API_KEY" not in list(keychain.KeychainMapping(["REDUCTO_API_KEY"]))


def test_a_write_that_never_returns_is_unavailable(monkeypatch, fake_keychain):
    monkeypatch.setattr(keychain, "WRITE_TIMEOUT_S", 0.2)

    def hang(*_):
        fake_keychain.release.wait(10)

    monkeypatch.setattr(fake_keychain, "set_password", hang)
    with pytest.raises(keychain.KeychainUnavailable, match="No keychain"):
        keychain.set("ANTHROPIC_API_KEY", KEY)
    _save(fake_keychain, "PULSE_API_KEY", "p")
    monkeypatch.setattr(fake_keychain, "delete_password", hang)
    with pytest.raises(keychain.KeychainUnavailable, match="No keychain"):
        keychain.delete("PULSE_API_KEY")


# --- the broker: PS1, PS2, PS3, PS5 ------------------------------------------------------------


def test_a_default_broker_never_reads_the_keychain_until_the_process_opts_in(fake_keychain):
    _save(fake_keychain, "ANTHROPIC_API_KEY", KEY)
    assert _api_key(EnvCredentialBroker()) is None
    assert fake_keychain.calls == []
    previous = credentials.use_keychain(True)
    try:
        assert credentials.keychain_in_use()
        assert _api_key(EnvCredentialBroker()) == KEY
    finally:
        credentials.use_keychain(previous)


@pytest.mark.parametrize("mode", ["off", "no-extra", "empty"])
def test_ps1_resolution_is_identical_with_nothing_usable_saved(monkeypatch, fake_keychain, mode):
    monkeypatch.setenv("REDUCTO_API_KEY", "from-env")
    if mode == "off":
        _save(fake_keychain, "ANTHROPIC_API_KEY", KEY)
        monkeypatch.setenv("OPENREADING_KEYCHAIN", "off")
    elif mode == "no-extra":
        _save(fake_keychain, "ANTHROPIC_API_KEY", KEY)
        monkeypatch.setitem(sys.modules, "keyring", None)
    plain = EnvCredentialBroker(os.environ)
    chained = EnvCredentialBroker(keychain_environ())
    for slug in ("anthropic-claude", "reducto", "aws-textract", "pymupdf", "qwen-vl"):
        desc = make_adapter(slug).descriptor
        assert chained.resolve(desc, _req(slug)) == plain.resolve(desc, _req(slug))
        assert chained.resolve_config(desc, _req(slug)) == plain.resolve_config(desc, _req(slug))


def test_ps2_env_beats_dotenv_beats_keychain(monkeypatch, fake_keychain, tmp_path):
    _save(fake_keychain, "ANTHROPIC_API_KEY", "from-keychain")
    _save(fake_keychain, "REDUCTO_API_KEY", "from-keychain")
    _save(fake_keychain, "PULSE_API_KEY", "from-keychain")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-shell")
    (tmp_path / ".env").write_text(
        "ANTHROPIC_API_KEY=from-dotenv\nREDUCTO_API_KEY=from-dotenv\n", encoding="utf-8"
    )
    load_dotenv(tmp_path / ".env")
    broker = EnvCredentialBroker(keychain_environ())
    assert _api_key(broker, "anthropic-claude") == "from-shell"
    assert _api_key(broker, "reducto") == "from-dotenv"
    assert _api_key(broker, "pulse") == "from-keychain"
    assert broker.var_source("ANTHROPIC_API_KEY") == "env"
    assert broker.var_source("REDUCTO_API_KEY") == ".env"
    assert broker.var_source("PULSE_API_KEY") == "keychain"
    assert broker.var_source("MISTRAL_API_KEY") == "missing"


@pytest.mark.parametrize(
    ("slug", "first", "second"),
    [
        ("llamaparse-fast", "LLAMA_CLOUD_API_KEY", "LLAMA_PARSE_API_KEY"),
        ("nuextract", "NUEXTRACT_API_KEY", "NUMIND_API_KEY"),
    ],
)
def test_ps2_an_exported_second_name_beats_a_saved_first_name(
    monkeypatch, fake_keychain, slug, first, second
):
    """Every environment name is tried before any keychain name, so an export always wins."""
    monkeypatch.delenv(first, raising=False)
    _save(fake_keychain, first, "from-keychain")
    monkeypatch.setenv(second, "from-shell")
    broker = EnvCredentialBroker(keychain_environ())
    assert _api_key(broker, slug) == "from-shell"
    assert broker.secret_sources(make_adapter(slug).descriptor)[0] == (second, "env")
    monkeypatch.delenv(second)
    assert _api_key(broker, slug) == "from-keychain"
    assert broker.secret_sources(make_adapter(slug).descriptor)[0] == (first, "keychain")


def test_the_slug_override_still_beats_a_saved_service_var(monkeypatch, fake_keychain):
    _save(fake_keychain, "REDUCTO_API_KEY", "from-keychain")
    monkeypatch.setenv("OPENREADING_REDUCTO_API_KEY", "override")
    broker = EnvCredentialBroker(keychain_environ())
    assert _api_key(broker, "reducto") == "override"
    assert broker.secret_sources(make_adapter("reducto").descriptor)[0] == (
        "OPENREADING_REDUCTO_API_KEY",
        "env",
    )


def test_a_blank_env_line_does_not_hide_a_saved_key(monkeypatch, fake_keychain):
    _save(fake_keychain, "ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    broker = EnvCredentialBroker(keychain_environ())
    assert _api_key(broker) == KEY
    assert broker.var_source("ANTHROPIC_API_KEY") == "keychain"
    # With nothing saved either, the blank still reads as blank, exactly as before.
    assert keychain_environ().get("MISTRAL_API_KEY") is None
    monkeypatch.setenv("MISTRAL_API_KEY", "")
    assert keychain_environ().get("MISTRAL_API_KEY") == ""


def test_ps3_a_saved_key_never_enters_os_environ_or_a_subprocess(monkeypatch, fake_keychain):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    _save(fake_keychain, "ANTHROPIC_API_KEY", KEY)
    broker = EnvCredentialBroker(keychain_environ())
    assert _api_key(broker) == KEY
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert KEY not in os.environ.values()
    child = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ.get('ANTHROPIC_API_KEY', ''))"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == ""


def test_ps5_knobs_and_slug_overrides_never_come_from_the_keychain(monkeypatch, fake_keychain):
    _save(fake_keychain, "OPENREADING_CREDENTIALS_REF_ALIASES", "EVIL")
    _save(fake_keychain, "OPENREADING_REDUCTO_API_KEY", "smuggled")
    _save(fake_keychain, "ANTHROPIC_MODEL", "smuggled-model")
    env = keychain_environ()
    assert env.get("OPENREADING_CREDENTIALS_REF_ALIASES") is None
    assert env.get("OPENREADING_REDUCTO_API_KEY") is None
    assert env.get("ANTHROPIC_MODEL") is None
    desc = make_adapter("anthropic-claude").descriptor
    assert "model" not in EnvCredentialBroker(env).resolve_config(desc, _req("anthropic-claude"))
    asked = {name for op, name in fake_keychain.calls}
    assert not asked & {"OPENREADING_CREDENTIALS_REF_ALIASES", "ANTHROPIC_MODEL"}


def test_declared_secret_envs_are_secret_service_vars_only():
    names = credentials._registered_secret_envs()
    assert {"ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY", "LLAMA_PARSE_API_KEY"} <= names
    assert "ANTHROPIC_MODEL" not in names and "AWS_REGION" not in names
    assert not any(n.startswith("OPENREADING_") for n in names)


def test_a_named_set_narrows_the_view(fake_keychain):
    _save(fake_keychain, "ANTHROPIC_API_KEY", KEY)
    assert keychain_environ(["REDUCTO_API_KEY"]).get("ANTHROPIC_API_KEY") is None


def test_dotenv_entries_report_line_numbers(tmp_path):
    path = tmp_path / "x.env"
    path.write_text("# c\n\nexport A=1\nB='two # kept' # dropped\nnovalue\nC=3 # note\n=x\n")
    assert credentials.dotenv_entries(path) == [
        (3, "A", "1"),
        (4, "B", "two # kept"),
        (6, "C", "3"),
    ]


def test_load_dotenv_records_which_names_it_set(monkeypatch, tmp_path):
    monkeypatch.setenv("ALREADY", "shell")
    (tmp_path / ".env").write_text("ALREADY=file\nFRESH_NAME_X=file\n", encoding="utf-8")
    assert load_dotenv(tmp_path / ".env") == 1
    assert credentials.dotenv_loaded_names() == {"FRESH_NAME_X"}
    # An explicit mapping is not the process environment, so nothing is recorded for it.
    load_dotenv(tmp_path / ".env", environ={})
    assert credentials.dotenv_loaded_names() == {"FRESH_NAME_X"}


# --- the Python API: opt-in only ----------------------------------------------------------------


def test_python_api_run_reads_the_keychain_only_with_keychain_true(monkeypatch, fake_keychain):
    _save(fake_keychain, "ANTHROPIC_API_KEY", KEY)
    seen: list[EnvCredentialBroker | None] = []

    def capture(*_args, broker=None, **_kw):
        seen.append(broker)
        raise RuntimeError("stop")

    monkeypatch.setattr(api, "run_request", capture)
    for kw in ({}, {"keychain": True}):
        with pytest.raises(RuntimeError):
            api.run(
                b"%PDF-1.4", "pymupdf", mime_type="application/pdf", config={"version": 1}, **kw
            )
    assert seen[0] is None  # the default broker, which is the environment alone
    assert fake_keychain.calls == []
    assert seen[1] is not None and _api_key(seen[1]) == KEY


def test_python_api_run_batch_reads_the_keychain_only_with_keychain_true(
    monkeypatch, fake_keychain
):
    import openreading.batch.sources as sources

    _save(fake_keychain, "ANTHROPIC_API_KEY", KEY)
    brokers: list[EnvCredentialBroker] = []

    def capture(_backend, _resolved, broker):
        brokers.append(broker)
        raise RuntimeError("stop")

    monkeypatch.setattr(sources, "resolve_intake", lambda *_a, **_k: [])
    monkeypatch.setattr(api, "_native_adapter", capture)
    for kw in ({}, {"keychain": True}):
        with pytest.raises(RuntimeError):
            api.run_batch(["x.pdf"], "pymupdf", config={"version": 1}, **kw)
    assert _api_key(brokers[0]) is None
    assert fake_keychain.calls == []
    assert _api_key(brokers[1]) == KEY


def test_python_api_resume_reads_the_keychain_only_with_keychain_true(
    monkeypatch, fake_keychain, tmp_path
):
    import types

    import openreading.strategies as strategies

    _save(fake_keychain, "ANTHROPIC_API_KEY", KEY)
    monkeypatch.setenv("OPENREADING_LEDGER", str(tmp_path / "ledger"))
    header = types.SimpleNamespace(strategy_name="main", pinned_eligible=["anthropic-claude"])
    monkeypatch.setattr(api, "read_header", lambda _root, _run_id: header)
    monkeypatch.setattr(api, "_request_from_header", lambda *_a: _req("anthropic-claude"))
    monkeypatch.setattr(
        strategies,
        "compile_strategy",
        lambda *_a, **_k: types.SimpleNamespace(
            config_hash="h", root=None, dispatchable=frozenset(), backend_allowlist=None
        ),
    )
    brokers: list[EnvCredentialBroker] = []

    def capture(_run_id, _req, _registry, broker, *_a, **_k):
        brokers.append(broker)
        raise RuntimeError("stop")

    monkeypatch.setattr(api, "_arm_ledger", capture)
    for kw in ({}, {"keychain": True}):
        with pytest.raises(RuntimeError):
            api.resume_run("7dbf6b71-adb5-4e90-9188-a184fdba9d05", **kw)
    assert _api_key(brokers[0]) is None
    assert fake_keychain.calls == []
    assert _api_key(brokers[1]) == KEY
