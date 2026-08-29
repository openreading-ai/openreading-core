"""In-memory backend registry: descriptor.id → adapter. The router reads descriptors from
here and never branches on backend type. A production deployment swaps in a shared/dynamic
registry behind the same surface; eligibility is always recomputed per request from current
descriptor state (internal/research/openreading/routing_and_compliance.md §5.5 — never cache
eligibility across term changes).
"""

from __future__ import annotations

from collections.abc import Iterator

from openreading.adapters.base import BackendAdapter

# Ledger T4a (AC-8): the router's own current adapter-contract floor. A descriptor declaring
# `protocol_version` below this is refused BY NAME at register() time — never silently accepted
# and left to fail later, mid-run, the first time poll()/cancel() is called without the `ctx` the
# v2 Protocol requires. `1` (not `2`): T4a converts only 8 of the 13 built-in adapters to v2 this
# milestone (internal/design/ledger.md §13's own "first zombie" guard, T4b finishes the rest) — the
# floor only needs to reject a version that predates the CURRENT adapter contract's own minimum
# (v1, the pre-Ledger shape), not force every adapter to already be v2.
#
# Review F3 (Phase C round 1): this floor is deliberately NOT the same number as the built-in fleet's
# own `==2` discipline bar, enforced separately by tests/test_protocol_version_guard.py, not by
# this floor — a third-party adapter that hasn't migrated to v2 can still register and run (with
# reduced resume guarantees); this floor only refuses protocol_version < 1.
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
