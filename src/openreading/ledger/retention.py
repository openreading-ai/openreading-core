"""Retention ceiling computation + reaper (internal/design/ledger.md §9.4, plan §7).

Openreading's own retention ceiling is `min(max_retention_hours)` over the **hosted** descriptors
on a run's path (`runs_fully_local=True` descriptors excluded — their `0`/`None` states the vendor
holds nothing, not that openreading may hold nothing). A `None` `max_retention_hours` on a hosted
descriptor contributes no term and marks the run `retention_unverified`; a ZDR-flagged descriptor
forces the whole path to zero content. The default below the ceiling is an open founder decision
(plan §7, §18 item 4) — T1 ships a placeholder (the most conservative hosted ceiling observed among
built-in adapters, currently AWS Textract-family territory; `OPENREADING_LEDGER_RETENTION_HOURS`
overrides it) and logs the placeholder to FOUNDER-INBOX.md rather than presenting it as settled.

The reaper is an at-run-start sweep (Open Questions §9 item 2, recommendation (a) — no new CLI
surface): it scans every stamped run under the ledger root and, for one whose ceiling has passed,
calls `KeyStore.destroy` — the exact mechanism a manual shred uses — so a reaped run and a
manually-shredded one leave the journal in the identical `payload_expired` state. Nothing else
sweeps: expiry deletes nothing until the next run arms the ledger, so the operator owns the
schedule.

**The clock: `expires_epoch_ms` is an absolute UTC epoch, from `Clock.now_wall_ms()`, never
`now_ms()`.** The stamp is written by one process and read by another, possibly across a reboot,
so it is the one place in the ledger where wall time is the correct base and monotonic time is a
defect. It shipped as `now_ms()` — `time.monotonic()`, whose zero point is the boot — which read
as 1970 to any loader and, worse, put every pre-reboot stamp permanently in the future of a
reaper whose own clock had restarted near zero: a run armed on a machine 16 days into its uptime
recorded an expiry ~17 days out, and after a reboot nothing reaped it until the new boot session
itself reached 17 days. The failure was one-directional — a stamp is `arm + window`, so any `now`
that exceeds it has already covered at least the window in real time, and a run could be reaped
late but never early — so it over-retained rather than shredding early, which is why no test
caught it. Over-retention is still the compliance defect: it holds PHI past a window an operator
attested to. `tighten_retention` and `reap` must read the SAME base as the stamp; see the note at
`InlineExecutor.exec`'s `tighten_retention` call for what mixing them does.

**Arm-time vs. dispatch-time (Ledger T3 round-2, Findings 8 and 6).** A fresh run's
FIRST stamp (`_arm_ledger`, before anything has dispatched) uses the operator default alone — not
`compute_retention_ceiling_hours` over the whole registry-wide eligible set, which conflated
"eligible for this document type" with "on this run's actual path" and let an unrelated,
never-dispatched hosted backend's own strict limit collapse the ceiling for a run that never went
near it. `tighten_retention` (below) then narrows that stamp — never widens it — once a step
genuinely dispatches, from THAT backend's own descriptor alone, called from `InlineExecutor.exec`'s
live "ok" branch (the one place a real dispatch is known to have happened).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

DEFAULT_RETENTION_HOURS = 24.0  # placeholder (plan §7) — founder-overridable, see FOUNDER-INBOX.md


def compute_retention_ceiling_hours(
    descriptors: list[Any], *, default_hours: float = DEFAULT_RETENTION_HOURS
) -> tuple[float, bool, bool]:
    """Returns `(ceiling_hours, retention_unverified, zdr)` for a run whose path includes
    `descriptors` (each a `ComplianceProfile`-bearing `AdapterDescriptor`)."""
    hosted_hours: list[float] = []
    unverified = False
    zdr = False
    for desc in descriptors:
        profile = getattr(desc, "compliance", None)
        if profile is None or profile.runs_fully_local:
            continue
        if profile.zdr_flag is not None:
            zdr = True
        if profile.max_retention_hours is None:
            unverified = True
        else:
            hosted_hours.append(profile.max_retention_hours)
    ceiling = min(hosted_hours) if hosted_hours else default_hours
    return ceiling, unverified, zdr


def stamp_path(root: Path, run_id: str) -> Path:
    return root / "retention" / f"{run_id}.json"


def stamp_run(root: Path, run_id: str, *, expires_epoch_ms: int, zdr: bool) -> None:
    p = stamp_path(root, run_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"run_id": run_id, "expires_epoch_ms": expires_epoch_ms, "zdr": zdr}),
        encoding="utf-8",
    )


def tighten_retention(root: Path, run_id: str, descriptor: Any, *, now_epoch_ms: int) -> None:
    """Ledger T3 round-2 (Findings 8 and 6, "the retention ceiling" half of the
    fix — see `InlineExecutor._is_zdr_backend` for the ZDR/blob-suppression half): narrows, never
    widens, a run's already-stamped ceiling in response to `descriptor` ACTUALLY dispatching.

    `_arm_ledger` used to compute the fresh-run stamp from `compute_retention_ceiling_hours` over
    the run's WHOLE registry-wide eligible set (`Router.route`'s stage-1/2 survivors for the
    request's document type) — a backend merely eligible, never named by the compiled strategy nor
    dispatched, could collapse the ceiling to its own strict limit (e.g. `reducto`'s
    `max_retention_hours=0`) even when nothing on the run's actual path was ever that backend. That
    conflated "eligible" (the correct, appropriately-broad scope for Sanitizer arming, where
    over-inclusion is harmless) with "on the run's path" (AC-11's own phrase), where over-inclusion
    silently destroys an unrelated, already-successful run's own resumability.

    The fix: `_arm_ledger` now stamps a fresh run from the operator default alone (nothing has
    dispatched yet), and THIS function is called — from `InlineExecutor.exec`'s own live "ok"
    dispatch, the one place that knows a backend genuinely ran — once per step that actually
    completes, narrowing the stamp if (and only if) that ONE descriptor's own declared limit is
    stricter than what's already recorded. A backend that stays merely eligible never calls this at
    all, so it has zero effect on the stamp — matching AC-11's "on that run's path," not the
    registry-wide eligible set. Mirrors this tranche's own append-only-tightening pattern elsewhere
    (`strategies/trace.py`'s `Attempt.bind_gates`/`.recategorize`: update the field, never
    recompute it from scratch, and only ever move it in the safe direction).

    A silent no-op when: there is no stamp yet (unarmed, or a resumed run whose stamp was already
    reaped — nothing left to tighten either way); or `descriptor` contributes no term at all (a
    fully-local descriptor, or a hosted one with an unverified `max_retention_hours`) — the same two
    "no term" cases `compute_retention_ceiling_hours` already carves out for the exact same reason.
    `now_epoch_ms` (the dispatch's own completion time, not the run's original arm time) is used as
    the tightened window's start — always at or after arm time, so this can only ever be as-strict-
    or-stricter than computing the ideal answer up front would have been, never looser (the safety
    property this fix must not weaken)."""
    stamp_file = stamp_path(root, run_id)
    if not stamp_file.exists():
        return
    stamp = json.loads(stamp_file.read_text(encoding="utf-8"))
    hours, _unverified, zdr = compute_retention_ceiling_hours(
        [descriptor], default_hours=float("inf")
    )
    new_expires = stamp["expires_epoch_ms"]
    if hours != float("inf"):
        new_expires = min(new_expires, now_epoch_ms + int(hours * 3600_000))
    new_zdr = bool(stamp.get("zdr", False)) or zdr
    if new_expires == stamp["expires_epoch_ms"] and new_zdr == stamp.get("zdr", False):
        return  # nothing to tighten — avoid a needless rewrite
    stamp_run(root, run_id, expires_epoch_ms=new_expires, zdr=new_zdr)


def reap(root: Path, keys: Any, blobs_root: Path, *, now_epoch_ms: int) -> list[str]:
    """Destroys the key (and removes the blob directory) for every stamped run past its ceiling.
    The journal file itself is left in place — audit metadata survives erasure by design (§9.4)."""
    stamp_dir = root / "retention"
    if not stamp_dir.exists():
        return []
    reaped: list[str] = []
    for stamp_file in sorted(stamp_dir.glob("*.json")):
        stamp = json.loads(stamp_file.read_text(encoding="utf-8"))
        if stamp["expires_epoch_ms"] > now_epoch_ms:
            continue
        run_id = stamp["run_id"]
        keys.destroy(run_id)
        shutil.rmtree(blobs_root / run_id, ignore_errors=True)
        stamp_file.unlink(missing_ok=True)
        reaped.append(run_id)
    return reaped
