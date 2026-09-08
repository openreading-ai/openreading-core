"""`RouterConfig`: the deployment-level settings the router reads.

One setting today: `backends`, the default backend ids in preference order.

This module used to be the compliance filter, stage 1 of a three-stage router. It read a
twelve-field `ComplianceProfile` off every descriptor, 180 vendor claims across fifteen adapters,
and dropped backends against a caller's `require_baa`, `no_train_on_data`, `data_region`,
`require_local` and `max_retention`.

None of that could be true. Every field was a claim about a company this project does not control,
published on a page that changes without notice, with nothing here able to detect drift and no way
for a reader to tell a fact verified last week from one copied at import time. A wrong entry did
not fail loudly: it routed a document to a backend the operator believed was excluded, and the run
succeeded.

The replacement is the caller's own conclusion. An operator who cares about compliance knows which
vendors they hold agreements with, which regions their contracts cover, and what their auditors
accepted. `backends: [aws-textract, pymupdf]` states that default, and core honors its order.

The rule that replaces "compliance is never relaxed by fallback", and that a reader two years from
now needs more than the diff: **core holds no fact it cannot verify. A constraint core cannot
check is a constraint core must not appear to enforce.**

Default-chain semantics
-----------------------
`policy.backends` supplies the chain only when a request names no backend. An empty list resolves
to no backend, while an absent list falls back to `pymupdf`. A named backend runs directly. The
server's API-key scope is a separate caller boundary that can narrow either form.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RouterConfig:
    """Deployment settings for one run.

    `backends` is `None` when no default chain was supplied. A tuple preserves the configured
    order, and an empty tuple resolves to no backend.
    """

    backends: tuple[str, ...] | None = None


@dataclass(frozen=True)
class DropReason:
    """Why a backend is not in a chain.

    Only one producer remains, `strategies.prune`'s scope drop: a backend the caller's own
    allow-list excluded. Stage 1 used to produce nine of these from vendor claims, and stage 2 two
    more from `input_formats` and the capability table. Those are gone, so a `DropReason` now
    always names something the caller said rather than something core believed about a vendor.
    """

    stage: int
    code: str
    detail: str = ""
    meta: dict = field(default_factory=dict)
