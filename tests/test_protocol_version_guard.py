"""Guard: no built-in adapter is left behind on protocol v1 after the Ledger T4b conversion.

Every entry in `BUILTIN_ADAPTERS` must declare `protocol_version` 2, and no allow-list may except
one (Ledger T4b §4.3, internal/eng-council/plans/sprint26-T4b-plan.md §4.3, design doc §13). An
allow-list would go stale the moment a new adapter lands, so this test reads the registry itself.
The protocol floor that `registry.register(...)` refuses to accept below has its own test at the
`router/registry.py` level.
"""

from __future__ import annotations

from openreading.adapters.registry import BUILTIN_ADAPTERS


def test_every_builtin_adapter_declares_protocol_version_2():
    # No hardcoded allow-list (Ledger T4b §4.3): every entry in BUILTIN_ADAPTERS, unconditionally.
    # That resolves the disclosed §5/§6 tension in internal/eng-council/FOUNDER-INBOX.md, since the
    # guard is now both meaningful (it checks something real) and green (the underlying condition
    # is really met, not merely scoped around the adapters that don't yet meet it).
    for slug, factory in BUILTIN_ADAPTERS.items():
        adapter = factory()
        assert adapter.descriptor.protocol_version == 2, (
            f"{slug} declares protocol_version={adapter.descriptor.protocol_version}, expected 2 "
            "— every built-in adapter must be on protocol v2 as of Ledger T4b"
        )
