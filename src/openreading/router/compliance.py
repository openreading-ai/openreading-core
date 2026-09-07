"""`RouterConfig`: the deployment-level settings the router reads.

One setting today: `backends`, the allow-list of backend ids in preference order.

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
accepted. `backends: [aws-textract, pymupdf]` states that, and core honours it exactly, forever,
with no table to rot. `design/compliance-removal.md` carries the full argument.

The rule that replaces "compliance is never relaxed by fallback", and that a reader two years from
now needs more than the diff: **core holds no fact it cannot verify. A constraint core cannot
check is a constraint core must not appear to enforce.**

Allow-list semantics
--------------------
Every source intersects and none widens: the file's `policy.backends`, the caller's argument, and
the server's API-key scope. An EMPTY list permits nothing and refuses with `scope_denied`, which
is the fail-closed direction the compliance keys used to hold. An absent list is not an empty one,
and means no restriction from that source.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RouterConfig:
    """Deployment settings for one run.

    `backends` is `None` for "no restriction from this source" and a tuple, possibly empty, for a
    stated allow-list. The distinction matters: an empty tuple permits nothing.
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
