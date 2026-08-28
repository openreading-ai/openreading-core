"""`router.registry.Registry` — Ledger T4a (AC-8): registering an adapter whose descriptor declares
`protocol_version` below the router's own current floor is refused BY NAME at `register(...)` time,
never silently accepted and left to fail later the first time `poll()`/`cancel()` is called without
the `ctx` the v2 Protocol requires.
"""

from __future__ import annotations

import pytest

from openreading.router.registry import PROTOCOL_VERSION_FLOOR, Registry
from tests.fakes import ConfigurableBackend, make_backend


def test_registering_a_below_floor_adapter_is_refused_by_name():
    assert PROTOCOL_VERSION_FLOOR >= 1
    adapter = make_backend("stale-backend")
    adapter.descriptor = adapter.descriptor.model_copy(
        update={"protocol_version": PROTOCOL_VERSION_FLOOR - 1}
    )
    reg = Registry()
    with pytest.raises(ValueError, match="stale-backend") as exc:
        reg.register(adapter)
    assert str(PROTOCOL_VERSION_FLOOR - 1) in str(exc.value)
    assert "stale-backend" not in reg  # never partially registered


def test_registering_an_at_floor_adapter_succeeds():
    adapter = make_backend("current-backend")
    adapter.descriptor = adapter.descriptor.model_copy(
        update={"protocol_version": PROTOCOL_VERSION_FLOOR}
    )
    reg = Registry()
    reg.register(adapter)
    assert "current-backend" in reg


def test_make_adapter_also_refuses_a_below_floor_adapter_by_name(monkeypatch):
    # ben's F2: adapters.registry.make_adapter() is a SECOND adapter-construction entry point
    # (used directly at ~20 production call sites, including api.prepare_named_backend's common
    # named-backend path) that never goes through Registry.register() — a below-floor adapter
    # built there used to sail through unrefused, all the way to submit()/poll(), instead of being
    # refused by name at construction time as AC-8 promises. Fixed by having make_adapter() run
    # the same check.
    from openreading.adapters import registry as adapters_registry

    def _stale() -> ConfigurableBackend:
        adapter = make_backend("stale-backend")
        adapter.descriptor = adapter.descriptor.model_copy(
            update={"protocol_version": PROTOCOL_VERSION_FLOOR - 1}
        )
        return adapter

    monkeypatch.setitem(adapters_registry.BUILTIN_ADAPTERS, "stale-backend", _stale)
    with pytest.raises(ValueError, match="stale-backend") as exc:
        adapters_registry.make_adapter("stale-backend")
    assert str(PROTOCOL_VERSION_FLOOR - 1) in str(exc.value)
