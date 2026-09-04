"""In-memory backend registry: descriptor.id maps to its adapter. The router reads descriptors
from here and never branches on backend type. Any other process model can supply its own
registry behind this same surface. Eligibility is always recomputed per request from the
current descriptors, never cached across a term change
(internal/research/openreading/routing_and_compliance.md §5.5).
"""

from __future__ import annotations

from collections.abc import Iterator

from openreading.adapters.base import BackendAdapter

# Ledger T4a (AC-8): the router's own adapter-contract floor. A descriptor declaring a
# `protocol_version` below this floor is refused BY NAME at register() time, never accepted
# and left to fail mid-run the first time poll()/cancel() is called without the `ctx` the v2
# Protocol requires. The floor is 1 rather than 2 on purpose. Every built-in adapter declares
# 2, and tests/test_protocol_version_guard.py holds the fleet to that. A third-party adapter
# still on v1 may register and run, with reduced resume guarantees, so this floor refuses only
# protocol_version < 1.
PROTOCOL_VERSION_FLOOR = 1


def check_protocol_version_floor(adapter: BackendAdapter) -> None:
    """Refuse, BY NAME, an adapter whose descriptor declares `protocol_version` below this
    router's current floor. Ledger T4a fix: this is the one place the AC-8 check lives, called
    both from `Registry.register()` below and from `adapters.registry.make_adapter()` — the
    latter is a second, direct adapter-construction entry point (~20 production call sites,
    including `api.prepare_named_backend`'s common named-backend path) that never goes through
    `Registry.register()`, so AC-8's own guarantee ("refused at registration time by name, not at
    call time") needs enforcing at both places an adapter instance comes into being, not just one.
    """
    bid = adapter.descriptor.id
    version = adapter.descriptor.protocol_version
    if version < PROTOCOL_VERSION_FLOOR:
        raise ValueError(
            f"backend {bid!r} declares protocol_version={version}, below this router's "
            f"current floor ({PROTOCOL_VERSION_FLOOR}); refusing to register"
        )


class Registry:
    def __init__(self) -> None:
        self._by_id: dict[str, BackendAdapter] = {}

    def register(self, adapter: BackendAdapter) -> BackendAdapter:
        bid = adapter.descriptor.id
        if bid in self._by_id:
            raise ValueError(f"backend id already registered: {bid!r}")
        check_protocol_version_floor(adapter)
        self._by_id[bid] = adapter
        return adapter

    def get(self, backend_id: str) -> BackendAdapter | None:
        return self._by_id.get(backend_id)

    def ids(self) -> list[str]:
        return list(self._by_id)

    def __len__(self) -> int:
        return len(self._by_id)

    def __iter__(self) -> Iterator[BackendAdapter]:
        return iter(self._by_id.values())

    def __contains__(self, backend_id: object) -> bool:
        return backend_id in self._by_id
