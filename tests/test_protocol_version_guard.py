"""Ledger T4b §4.3 — the "first zombie" guard, WIDENED to all 13, unscoped, per the design doc's
own §13 (the reading T4a's own scoped version deliberately deferred until this milestone actually
converted the remaining 5 — internal/eng-council/plans/sprint26-T4b-plan.md §4.3). T4a's own scoped
version (naming exactly its 8 converted adapters, plus a companion assertion that the other 5 still
declared v1) is replaced outright, not kept alongside this one — the whole point of T4b's own
completion is that the distinction it drew no longer needs drawing.
`registry.register(...)`'s own floor-refusal is a separate, `router/registry.py`-level test.
"""

from __future__ import annotations

from openreading.adapters.registry import BUILTIN_ADAPTERS


def test_every_builtin_adapter_declares_protocol_version_2():
    # No hardcoded allow-list (Ledger T4b §4.3): every entry in BUILTIN_ADAPTERS, unconditionally —
    # this is what actually resolves the disclosed §5/§6 tension in FOUNDER-INBOX.md, since the
    # guard is now both meaningful (it checks something real) and green (the underlying condition
    # is really met, not merely scoped around the adapters that don't yet meet it).
    for slug, factory in BUILTIN_ADAPTERS.items():
        adapter = factory()
        assert adapter.descriptor.protocol_version == 2, (
            f"{slug} declares protocol_version={adapter.descriptor.protocol_version}, expected 2 "
            "— every built-in adapter must be on protocol v2 as of Ledger T4b"
        )
