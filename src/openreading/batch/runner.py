"""The platform runner (Manifest v0.6, invariants M6–M9). Pure orchestration over a
`run_one(source, idempotency_key) -> response.v0.3 dict` seam — production wires it to api.run
(route → materialize → submit → run_to_completion → normalize → validate, unchanged); the runner
adds only the batch layer: per-item isolation, bounded concurrency, honest aggregation, progress,
and the skip warning. It never touches the single-document contract."""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Literal

from openreading.batch.sources import ResolvedSource
from openreading.types.batch import (
    MAX_BATCH_JOBS,
    BatchItem,
    BatchItemError,
    BatchRequestEcho,
    BatchResult,
    BatchStatus,
    BatchSummary,
    BatchWarning,
)

RunOne = Callable[[ResolvedSource, str | None], dict[str, Any]]
OnProgress = Callable[[int, int, BatchItem], None]


class JobsLimitError(Exception):
    """`jobs` above the max-jobs ceiling (bound_jobs, BL-84). Mirrors `batch.sources
    .SourceLimitError`'s own shape — a coded, catchable exception naming the count and the limit,
    not a bare crash after a batch has already run to completion."""


def bound_jobs(jobs: int, *, max_jobs: int = MAX_BATCH_JOBS) -> int:
    """The ONE shared floor/ceiling bounds-check for `jobs` (BL-84). Called explicitly by all
    three surfaces — server, CLI, Python API — at the point each first receives the value, BEFORE
    that surface's own `BatchRequestEcho` is constructed, so the echo always reports the corrected
    value rather than a surface-local unclamped variable (otherwise a silent provenance bug
    against the field's one stated purpose). Deliberately NOT called from inside `run_batch`
    itself: a check buried there would run too late to fix what the echo already reported.

    Floor (`jobs <= 0`): clamp to 1 — matches the server's own pre-existing `max(1, ...)`
    precedent (BL-78). No plausible legitimate caller intent exists for a non-positive worker
    count, so silently coercing it up is strictly safer than running the input as given.

    Ceiling (`jobs > max_jobs`): reject — raise `JobsLimitError` naming the count and the limit.
    A ceiling violation is worth surfacing loudly: silently capping it would either mask a
    probing attacker or quietly under-serve a caller who meant a bigger pool. `max_jobs` is the
    CLI's `--max-jobs` / Python API's `max_jobs=` escape hatch (mirrors `resolve_intake`'s own
    `max_items` "sane default + override" shape); the HTTP surface never passes a non-default
    value here — the request body is exactly the untrusted-input boundary this item is about.
    """
    if jobs <= 0:
        return 1
    if jobs > max_jobs:
        raise JobsLimitError(
            f"jobs={jobs} is over the max-jobs limit ({max_jobs}); "
            f"raise it with --max-jobs / max_jobs= if this is intended"
        )
    return jobs


def item_idempotency_key(base: str | None, sha256: str | None) -> str | None:
    """Per-item idempotency key: ``f"{base}:{sha256[:16]}"`` when both are present, else None (no
    base ⇒ none fabricated; no sha ⇒ e.g. a URL item gets none — the doc defines only the
    sha-derived form)."""
    if not base or not sha256:
        return None
    return f"{base}:{sha256[:16]}"


def batch_state(succeeded: int, failed: int) -> Literal["succeeded", "partial", "failed"]:
    """M5 status rule: succeeded = >=1 succeeded and 0 failed; partial = some of each; failed =
    0 succeeded (all failed, or empty, so nothing was produced)."""
    if succeeded and not failed:
        return "succeeded"
    if succeeded and failed:
        return "partial"
    return "failed"


def _error_code(exc: Exception) -> str:
    return getattr(exc, "backend_code", None) or type(exc).__name__


def _run_item(src: ResolvedSource, run_one: RunOne, base_key: str | None) -> BatchItem:
    """Execute one source in isolation (M6): a raised exception becomes a `failed`
    item carrying the error, never aborting the batch."""
    idem = item_idempotency_key(base_key, src.ref.sha256)
    try:
        resp = run_one(src, idem)
    except Exception as e:  # noqa: BLE001 — per-item isolation is the whole point
        return BatchItem(
            source=src.ref,
            state="failed",
            error=BatchItemError(code=_error_code(e), message=str(e)),
        )
    return BatchItem(source=src.ref, state="succeeded", response=resp, transport="platform")


def _summarize(items: list[BatchItem], duration_ms: int) -> BatchSummary:
    succeeded = [i for i in items if i.state == "succeeded"]
    pages: list[int] = []
    backends: dict[str, int] = {}
    for it in succeeded:
        usage = (it.response or {}).get("usage") or {}
        p = usage.get("pages_processed")
        if isinstance(p, int) and not isinstance(p, bool):
            pages.append(p)
        bid = ((it.response or {}).get("backend") or {}).get("id")
        if bid:
            backends[bid] = backends.get(bid, 0) + 1
    return BatchSummary(
        total=len(items),
        succeeded=len(succeeded),
        failed=sum(1 for i in items if i.state == "failed"),
        duration_ms=duration_ms,
        pages_processed=sum(pages) if pages else None,
        backends=backends,
    )


def run_batch(
    sources: list[ResolvedSource],
    *,
    run_one: RunOne,
    jobs: int = 1,
    idempotency_key: str | None = None,
    request_echo: BatchRequestEcho | None = None,
    on_progress: OnProgress | None = None,
) -> BatchResult:
    """Run every source through `run_one` (serial when jobs==1, else a bounded thread
    pool), collect per-item results in INPUT order (M1), aggregate honestly (M8), and assemble the
    batch envelope. Every source the caller named is run: intake does not pre-judge one by its
    extension, so a format a backend cannot read arrives as that backend's own refusal. But still
    reach `on_progress`/`_emit` at the point they're classified, so a skip gets its own progress
    line and the `[N/total]` counter always reaches `total`, not just for items actually run."""
    items: list[BatchItem | None] = [None] * len(sources)
    to_run: list[int] = []

    started = time.perf_counter()
    done = 0
    total = len(sources)

    def _emit(idx: int) -> None:
        nonlocal done
        done += 1
        if on_progress:
            item = items[idx]
            assert item is not None
            on_progress(done, total, item)

    # Every source runs. Intake no longer pre-judges a document by its extension, so a format a
    # backend cannot read arrives as that backend's own refusal on a failed item.
    to_run.extend(range(len(sources)))

    if jobs <= 1:
        for idx in to_run:
            items[idx] = _run_item(sources[idx], run_one, idempotency_key)
            _emit(idx)
    else:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {
                pool.submit(_run_item, sources[idx], run_one, idempotency_key): idx
                for idx in to_run
            }
            from concurrent.futures import as_completed

            for fut in as_completed(futures):
                idx = futures[fut]
                items[idx] = fut.result()  # _run_item never raises (M6)
                _emit(idx)

    ordered = [i for i in items if i is not None]  # input order preserved (index-placed)
    duration_ms = int((time.perf_counter() - started) * 1000)
    return assemble_result(ordered, request_echo=request_echo, duration_ms=duration_ms)


def assemble_result(
    items: list[BatchItem],
    *,
    request_echo: BatchRequestEcho | None = None,
    duration_ms: int = 0,
) -> BatchResult:
    """Turn a finished, input-ordered item list into the batch envelope: honest summary (M8), the
    status truth table (M5), and a warning — the skip tally for a non-empty batch, or
    `empty_batch` when no source resolved to a document at all (an empty/hidden-only directory, a
    zero-match expansion, an empty `documents: []` request body — nothing was silently dropped, it
    is named in the envelope every surface shares). Shared by the platform runner and the native
    path so both produce an OBSERVATIONALLY EQUIVALENT envelope (M10)."""
    summary = _summarize(items, duration_ms)
    if not items:
        warnings: list[BatchWarning] | None = [
            BatchWarning(code="empty_batch", message="no source resolved to a document to process")
        ]
    else:
        # Nothing is skipped any more, so there is no skip summary to warn about. A file the
        # backend could not read is a failed item carrying that backend's own reason.
        warnings = None
    return BatchResult(
        status=BatchStatus(state=batch_state(summary.succeeded, summary.failed)),
        request=request_echo,
        items=items,
        summary=summary,
        warnings=warnings,
    )
