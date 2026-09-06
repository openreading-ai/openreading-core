"""`openreading` CLI — a thin shell over the public API (`openreading.run`/`route`). It resolves
credentials from the environment (`.env` / process env), dispatches to a backend (or the router),
and prints the one response schema.

    openreading parse   <file> --backend reducto        # run one backend
    openreading route   <file> --run                    # compliance-first plan (+ execute chain)
    openreading backends                                # which backends are configured, and why not
    openreading backends --check docling                # ...and is it actually answering? (probes)
    openreading benchmark list                          # public corpora and their terms lanes

Credentials never travel on the CLI: they are read from the environment by the broker
(OPENREADING_<SLUG>_<KEY> or the service-native var, e.g. REDUCTO_API_KEY). A `.env` in the
working directory is loaded automatically (never overriding an already-set var).

The user-facing reference (every subcommand, flags, exit codes 0-6 and 143) is the `openreading.cli`
package docstring; this module holds the `cmd_*` handlers, `build_parser`, and `main`.

Environment this module reads itself
------------------------------------
- `.env` / `--env-file PATH`: `main` calls `credentials.load_dotenv(args.env_file)` before any
  handler runs. Default is `./.env` when present; a missing file is silently nothing. It never
  overrides an already-set process variable (`openreading.credentials.load_dotenv`: explicit
  process env always wins), so an exported value always beats the file -- the failure avoided is
  a checked-in or stale `.env` silently overwriting the key you deliberately exported for this
  shell; the explicit act outranks the ambient file.
- `OPENREADING_LEDGER` (a directory path; arms the run journal). Read here in exactly one place:
  `_cmd_parse_batch`'s KeyboardInterrupt handler, where it decides between exit 6 ("interrupted,
  per-item runs may be resumable") and re-raising the bare interrupt. Unset, a Ctrl-C in a batch
  is an ordinary KeyboardInterrupt, byte-for-byte the pre-ledger behavior (an unset-ledger SIGTERM
  is caught one level up, by `_terminate_as_interrupt`, and exits 143 with one line). The
  single-document path does not read the variable: it relies on `api.run`'s `on_run_armed`
  callback, which fires only when the ledger actually armed for THAT run, so a named-backend /
  `auto` run (which never
  journals) cannot print a run id that does not exist. Which runs journal is `api._arm_ledger`'s
  call graph, documented in `openreading.api` and internal/design/ledger.md.

Every other knob is read where it is used, not here: `OPENREADING_CONFIG` and `./openreading.yaml`
discovery in `openreading.strategies.loader` (behind `--config`), `OPENREADING_LLM_DECIDER` in
`openreading.strategies.decider`, backend credentials in `openreading.credentials`, and the
server-only auth / compliance-posture variables in `openreading.server.app` -- setting one of those
in a shell and running the CLI does nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, TypeGuard

from openreading import __version__ as openreading_version
from openreading import api, config, schemas
from openreading.adapters.registry import BUILTIN_ADAPTERS, build_registry, make_adapter
from openreading.batch.runner import MAX_BATCH_JOBS, JobsLimitError
from openreading.batch.sources import (
    DEFAULT_MAX_ITEMS,
    SourceLimitError,
    SourceNotFoundError,
    is_url,
    looks_batch,
    normalize_input_format,
)
from openreading.cli.help import cmd_help
from openreading.credentials import EnvCredentialBroker, load_dotenv
from openreading.ledger.header import HeaderMismatch
from openreading.ledger.ports import PayloadExpired
from openreading.liveness import check_liveness, probe_kind
from openreading.readiness import (
    auth_rejected_backends,
    auth_rejected_hint,
    backend_readiness,
    missing_reason,
)
from openreading.router.executor import execute_plan
from openreading.router.router import Router, RouterConfig
from openreading.strategies import (
    PRESET_NAMES,
    ConfigError,
    NormalizeError,
    load_config,
    normalize_config,
    normalize_strategy,
    validate_config,
)
from openreading.types.errors import (
    ComplianceRefused,
    PlanExhaustedError,
    RetryableError,
    TerminalError,
    UnsupportedFeatureError,
)
from openreading.types.liveness import ProbeKind

# AdapterError subtypes that fold into ONE clean `[label] {e}` stderr line + exit 3 — never the
# generic `except Exception` crash handler below, which prefixes the raw class name and exits 1.
# Named here (mirroring router/executor.py's own `_TAXONOMY`, all four members) so cmd_parse and
# cmd_compare's fan-out share a single definition instead of each hand-rolling its own tuple — which
# is exactly how RetryableError went missing from both of them in the first place (BL-122), and how
# UnsupportedFeatureError went missing from cmd_compare's fan-out right after, in this same tuple
# (BL-129). cmd_parse itself never falls through to this generic UnsupportedFeatureError handling —
# its own dedicated `except UnsupportedFeatureError` clause, earlier in its try block, catches first.
# `_cmd_parse_batch` reuses the same constant too (BL-128) rather than hand-rolling its own third
# tuple — the identical gap, one call site later, for a native-batch backend's submit_many/
# run_to_completion.
_CLEAN_EXIT3_ERRORS = (TerminalError, ComplianceRefused, RetryableError, UnsupportedFeatureError)

# `benchmark run` defaults small because it spends the reader's money on someone else's API. Two
# documents is enough to see every target produce output and a score, and cheap enough that
# getting the command wrong costs cents. Scaling up is `--limit N`, and `--limit 0` is the whole
# prepared corpus. The default is deliberately not the publisher's smoke set, which is already
# tens of documents across every category.
DEFAULT_BENCHMARK_LIMIT = 2


def _print_exhausted(tag: str, e: PlanExhaustedError, trail_summary: str) -> None:
    """Report an exhausted chain/walk on stderr: the one-line trail, then — for every backend that
    failed because its key was REJECTED — the actionable `check <VAR>` hint. A trail records only
    backend/category/code, so the hint is re-derived from the slug (readiness.auth_rejected_hint)."""
    print(f"[{tag}] {trail_summary or str(e)}", file=sys.stderr)
    for backend in auth_rejected_backends(e.trail):
        print(f"[{tag}] {auth_rejected_hint(backend)}", file=sys.stderr)


class _InputFileError(Exception):
    """A file a command was told to read that could not be opened or does not hold JSON."""


def _describe_read_error(e: OSError | json.JSONDecodeError) -> str:
    """A path-FREE description of a failed read, for the `f"cannot read {path}: {...}"` sites below.

    `.strerror` when `e` is an `OSError` with `.strerror` set — true for a real
    `FileNotFoundError`/`PermissionError` (`.strerror` is the bare OS reason, e.g. "No such file or
    directory", never the path) and, since BL-141, for `SourceNotFoundError` too (constructed via the
    stdlib's own errno-style `OSError.__init__(errno, strerror, filename)` signature specifically so
    it lands in this branch uniformly, with no special case). `str(e)` otherwise, covering
    `json.JSONDecodeError`, whose message is purely about the malformed byte position and never
    mentions the filename either way.

    Every caller already knows the path (it's what it just tried to read) and prints it once, up
    front, in its own `cannot read {path}:` prefix — this exists so what follows the colon never
    repeats it. Before BL-141, callers interpolated `str(e)` directly there, and for any real
    filesystem error `str(e)` already embeds `: '<path>'` (`OSError.__str__`'s own formatting), so
    the line said the path twice.
    """
    if isinstance(e, OSError) and e.strerror:
        return e.strerror
    return str(e)


def _read_json_or_fail(path: str) -> Any:
    """Read and parse a JSON file, or raise with a message that says which of the two failed. A
    file the process cannot open and a file whose bytes are not JSON are different problems for
    the reader, and one wording for both sends them to check permissions on a file that reads
    fine.

    Every caller reads machine output a previous command wrote: a response, a trace, a comparison
    report. Nothing a person authors is JSON.
    """
    try:
        return json.loads(Path(path).read_text())
    except OSError as e:
        raise _InputFileError(f"cannot read {path}: {_describe_read_error(e)}") from e
    except json.JSONDecodeError as e:
        raise _InputFileError(
            f"{path} is not valid JSON: {e.msg} at line {e.lineno} column {e.colno}"
        ) from e


def cmd_parse(args) -> int:
    """`openreading parse`: one document, or a batch when the sources look like one
    (`looks_batch`). Exit codes are in the `openreading.cli` docstring."""
    # exactly one of --backend / --strategy / --no-strategy
    chosen = [x for x in (args.backend, args.strategy, "none" if args.no_strategy else None) if x]
    if len(chosen) != 1:
        print(
            "[parse] specify exactly one of --backend / --strategy / --no-strategy",
            file=sys.stderr,
        )
        return 2
    label = args.backend or (f"strategy:{args.strategy}" if args.strategy else "auto")
    if args.backend:
        # belt to argparse's `choices` braces: the slug must also resolve in the catalog, so a
        # divergence surfaces as exit 2 here rather than a traceback deeper in the run.
        try:
            adapter = make_adapter(args.backend)
        except KeyError as e:
            print(f"[parse] {e}", file=sys.stderr)
            return 2
        # A single document in a format the named backend does not read is refused here, in the
        # word the batch path already uses for it (`skip_reason: unsupported_format`). Dispatching
        # anyway hands the reader the parsing library's own stream error, which names neither the
        # format nor the fix. Guarded on a known extension and a descriptor that declares formats,
        # so an extensionless file and a silent descriptor dispatch exactly as before.
        if not looks_batch(args.files) and not is_url(args.files[0]):
            fmt = normalize_input_format(Path(args.files[0]).suffix.lstrip("."))
            supported = {
                normalize_input_format(f) for f in adapter.descriptor.capabilities.input_formats
            }
            if fmt and supported and fmt not in supported:
                print(
                    f"[{args.backend}] unsupported_format: {args.backend} does not read "
                    f".{fmt}. It reads {', '.join(sorted(supported))}.",
                    file=sys.stderr,
                )
                return 3

    overrides: dict[str, Any] = {}
    if args.pages:
        overrides["pages"] = {"ranges": [{"start": p, "end": p} for p in args.pages]}
    if args.extract is not None:
        overrides["extraction_schema"] = {
            "instructions": args.extract or "extract key/value fields"
        }

    run_kwargs: dict[str, Any] = {"operation": args.operation, "config": args.config, **overrides}
    if getattr(args, "keep_candidates", False):
        run_kwargs["keep_candidates"] = True  # retain strategy losers for `compare --from`
    if args.strategy:
        run_kwargs["strategy"] = args.strategy
    elif args.no_strategy:
        run_kwargs["backend"] = "strategy:none"
    else:
        run_kwargs["backend"] = args.backend

    # M2: a directory / glob / >=2 args is a batch; a single explicit file/URL stays single-document
    # (byte-identical to v0.5). The batch path shares the same backend/strategy + request overrides.
    if looks_batch(args.files):
        return _cmd_parse_batch(args, overrides, label)

    # BL-169: --deadline is CLI-friendly SECONDS; api.run's own deadline_ms= parameter (and every
    # internal deadline field it feeds) is milliseconds — same seconds-to-ms conversion
    # _cmd_parse_batch already applies for native-batch dispatch. Only affects a directly-named
    # backend (run()'s own docstring); `auto`/`--strategy` dispatch manages its own time budget and
    # silently ignores it.
    deadline_ms = int(args.deadline_s * 1000) if args.deadline_s is not None else None

    # Ledger T3 (plan §4.4): captured the instant the ledger arms (api.run's own on_run_armed
    # hook, invoked once immediately after _arm_ledger succeeds) so the KeyboardInterrupt handler
    # below has a real run id to print, even though the walk itself hasn't returned anything yet.
    armed_run_id: list[str] = []

    try:
        # backend stdout advisories (e.g. PyMuPDF find_tables) → stderr so stdout is only the JSON.
        with contextlib.redirect_stdout(sys.stderr):
            result = api.run(
                args.files[0],
                deadline_ms=deadline_ms,
                on_run_armed=armed_run_id.append,
                **run_kwargs,
            )
    except KeyboardInterrupt:
        # F3(b): the minimal live trigger for exit code 6 — a real crash (kill -9) cannot produce
        # any process-chosen exit code at all, but Ctrl-C is an ordinary exception Python's default
        # SIGINT handler raises, so catching it here is enough to make "interrupted, resumable" a
        # reachable outcome. Only resumable when the ledger actually armed for this run (a plain
        # named-backend/`auto` run never touches the ledger at all — nothing to resume); otherwise
        # this re-raises unchanged, exactly today's behavior (L1's zero-delta).
        if armed_run_id:
            rid = armed_run_id[0]
            print(f"[parse] interrupted; run {rid} is resumable", file=sys.stderr)
            print(f"[parse] resume with: openreading resume {rid}", file=sys.stderr)
            return 6
        raise
    except UnsupportedFeatureError as e:
        print(f"[{label}] unsupported feature ({e.feature}): {e}", file=sys.stderr)
        return 3
    except PlanExhaustedError as e:
        _print_exhausted(label, e, "")
        return 3
    except _CLEAN_EXIT3_ERRORS as e:
        if getattr(e, "backend_code", None) == "unknown_strategy":
            print(f"[{label}] {e}", file=sys.stderr)
            return 2
        # missing_credentials names the vars + signup; auth_rejected carries its `check <VAR>` hint;
        # every other backend_code has any resolved secret value redacted (`***`) out of its raw
        # message instead (both attached at the execution boundary by readiness.auth_hinted /
        # router.executor.execute_plan — BL-37). A RetryableError reaching here (rate-limit
        # exhaustion, or router.driver's poll loop past its deadline/MAX_CONSECUTIVE_FAULTS) gets the
        # identical clean exit — a directly-named backend has no next rung to fall back to the way
        # `auto`'s execute_plan does (BL-122).
        print(f"[{label}] {e}", file=sys.stderr)
        return 3
    except SourceNotFoundError as e:
        # BL-133: the single-document sibling of _cmd_parse_batch's own (SourceLimitError,
        # SourceNotFoundError) handling below — same exit code (2), same one-tagged-line shape.
        # Without this clause, SourceNotFoundError (an OSError subclass) still fell to the generic
        # except Exception below rather than exit 2 (the openreading.cli docstring's own
        # documented code for `parse`: an unresolvable source), landing at the wrong-but-clean
        # exit 1 instead. The path and the reason are printed apart, because `str(e)` on an
        # OSError leads with an "[Errno 2]" the reader who mistyped a filename cannot use.
        print(f"[{label}] cannot read {args.files[0]}: {_describe_read_error(e)}", file=sys.stderr)
        return 2
    except ConfigError as e:
        # The openreading.yaml, refused where it is read: a grammar error, or a `policy:` block
        # that is not a policy. Exit 3, the rung a caller-side mistake takes, and named before the
        # document is opened.
        print(f"[{label}] {e}", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001
        print(f"[{label}] error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    schemas.validate_response(result)  # never print a non-conforming response
    print(json.dumps(result, indent=2))
    return 0


def _save_batch_items(env: dict, save_dir: str) -> None:
    """--save-dir: write each succeeded item's inner response to <save-dir>/<relpath>.json (mirrors
    compare's fan-out flag), so the per-backend envelopes are re-usable offline."""
    root = Path(save_dir)
    for item in env.get("items", []):
        if item.get("state") != "succeeded" or not item.get("response"):
            continue
        rel = item["source"].get("relpath") or item["source"].get("filename")
        out = root / f"{rel}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(item["response"], indent=2))


def _usd(v: float) -> str:
    """A dollar amount for the cost preflight. Cents below a dollar-scale total, but four places
    once rounding to the cent would print `$0.00` for a real (if small) bill — the preflight's
    whole job is to be a number the reader can multiply, and zero multiplies to zero."""
    return f"${v:,.2f}" if v >= 0.01 else f"${v:.4f}"


def _page_number(raw: str) -> int:
    """argparse type for --pages. A page number below 1 is a typo on a 1-based flag, and letting
    it reach the request model turns it into a pydantic dump and exit 1, where every other bad
    flag value on this CLI is one line and exit 2.

    The failure this raises for a non-number is the common one, and it is not a typo: `--pages`
    takes a variable number of values, so `parse --pages 1 doc.pdf` feeds argparse the FILE. The
    message says that, because argparse's own would name this function at the reader.
    """
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"'{raw}' is not a page number. --pages takes several values, so name FILE before it"
            " (`parse doc.pdf --pages 1 2`) or close the list with --"
        ) from None
    if value < 1:
        raise argparse.ArgumentTypeError("page numbers are 1-based")
    return value


def _cmd_parse_batch(args, overrides: dict, label: str) -> int:
    """Batch parse (Manifest v0.6): one envelope over many documents. Progress + cost preflight go
    to stderr; stdout stays the single batch-result JSON. Exit 4 = partial (some items failed)."""
    bkwargs: dict[str, Any] = {**overrides, "config": args.config, "operation": args.operation}
    if getattr(args, "keep_candidates", False):
        # Saved item responses retain alternatives that `compare --from` can read individually.
        bkwargs["keep_candidates"] = True
    if args.strategy:
        bkwargs["strategy"] = args.strategy
    elif args.no_strategy:
        bkwargs["backend"] = "strategy:none"
    else:
        bkwargs["backend"] = args.backend

    def on_progress(done: int, total: int, item) -> None:
        loc = item.source.relpath or item.source.filename
        extra = (item.error.code if item.error else None) or item.skip_reason or ""
        # Per-item isolation (M6) means a failure never raises out of the batch, so this line is
        # the only place its message is read — stdout is the envelope, usually redirected to a file.
        if item.error and item.error.message:
            extra = f"{extra}: {item.error.message}"
        print(f"[{done}/{total}] {loc} {item.state} {extra}".rstrip(), file=sys.stderr)

    def on_preflight(resolved, backend: str) -> None:
        # Both advisories describe what `api.run_batch` is about to do to a DIRECTLY NAMED backend:
        # `auto` and strategies resolve per item inside the router, so neither the rate nor the
        # concurrency cap below is knowable here — and run_batch skips the cap for them too.
        if backend == "auto" or backend.startswith("strategy:"):
            return
        try:
            d = make_adapter(backend).descriptor
        except KeyError:
            return

        # The requested --jobs is silently reduced to min(requested, descriptor.batch
        # .max_concurrency) inside run_batch, and only the reduced value survives, in
        # `request.jobs`. Without this line a caller who asks for 16 workers on a backend that
        # caps at 4 sees no speedup, no error, and no way to learn which of the two numbers the
        # run actually used. Fires only when the request is genuinely unachievable, so the
        # default (--jobs 1, under every cap) stays silent.
        cap = d.batch.max_concurrency if d.batch else None
        if cap and args.jobs > cap:
            print(
                f"[preflight] --jobs {args.jobs} requested; {backend} caps platform concurrency "
                f"at {cap} (descriptor.batch.max_concurrency), so this run uses {cap}",
                file=sys.stderr,
            )

        live = [r for r in resolved if r.skip_reason is None]
        if len(live) <= 10 or d.type.value != "hosted_api":
            return
        # The rate is per PAGE, and items are documents. Naming the item count beside a per-page
        # rate invites multiplying the two, which under-reads a real corpus by its average page
        # count. So: state the basis in words, then multiply out the ONE total that is actually
        # computable before any file is opened (intake reads no bytes and never fetches a URL, so
        # page counts do not exist yet) and label it as the single-page floor it is.
        n = len(live)
        ends = [v for v in (d.cost.usd_per_page_equiv_low, d.cost.usd_per_page_equiv_high) if v]
        rate = (
            "-".join(f"${v}" for v in ends) if ends else "billed per page-equiv"
        )  # one endpoint published, or two, or none
        basis = f"~{rate} per page-equiv" if ends else rate
        print(
            f"[preflight] {n} items on hosted backend {backend}: {basis}, not per item",
            file=sys.stderr,
        )
        if ends:
            total = "-".join(_usd(v * n) for v in ends)
            print(
                f"[preflight] {n} items would cost ~{total} if every item is one page; multiply by "
                "your average page count (pages are not counted before the run)",
                file=sys.stderr,
            )

    try:
        with contextlib.redirect_stdout(sys.stderr):  # backend chatter → stderr; stdout stays JSON
            deadline_ms = (
                int(args.deadline_s * 1000) if args.deadline_s is not None else None
            )  # BL-135: seconds on the CLI (human-friendly); ms internally (RunContext.deadline_ms)
            env = api.run_batch(
                args.files,
                jobs=args.jobs,
                max_jobs=args.max_jobs,
                max_items=args.max_items,
                deadline_ms=deadline_ms,
                on_progress=on_progress,
                on_preflight=on_preflight,
                **bkwargs,
            )
        if args.save_dir:
            _save_batch_items(env, args.save_dir)
        # BL-84: moved inside this try/except (was after it) as a defense-in-depth backstop — ANY
        # future schema-conformance bug becomes a clean coded exit here, not a bare traceback after
        # every document has already parsed successfully.
        schemas.validate_batch_result(env)  # never print a non-conforming envelope
    except KeyboardInterrupt:
        # F3(b), batch sibling: a batch has no single run_id of its own (batch resume/coordination
        # is explicitly out of this tranche's scope — the single-document case is T3's whole
        # scope), so this can only point at where per-item journals live, not name one resumable
        # run. Only reachable — return 6 rather than re-raise — when the ledger is actually armed
        # for this process; otherwise today's behavior (a bare KeyboardInterrupt) is unchanged.
        if os.environ.get("OPENREADING_LEDGER"):
            print(
                "[batch] interrupted. Per-item runs under $OPENREADING_LEDGER may be "
                "individually resumable with openreading resume <RUN_ID>. Batch-level resume "
                "is not supported.",
                file=sys.stderr,
            )
            return 6
        raise
    except (SourceLimitError, JobsLimitError) as e:
        print(f"[batch] {e}", file=sys.stderr)
        return 2
    except SourceNotFoundError as e:
        # Split out of the tuple above so the errno never reaches the terminal: BL-143 gives this
        # exception a real errno for any caller that prints `str(e)`, and CPython renders that as
        # a leading "[Errno 2]" the reader who mistyped a glob cannot use.
        print(f"[batch] {_describe_read_error(e)}: {e.filename!r}", file=sys.stderr)
        return 2
    except _CLEAN_EXIT3_ERRORS as e:
        # missing_credentials msg names vars + signup; a RetryableError reaching here (a
        # native-batch backend's submit_many rate-limit exhaustion, or router.driver's poll loop
        # past its deadline/MAX_CONSECUTIVE_FAULTS) gets the identical clean exit — a directly-named
        # backend has no next rung to fall back to the way `auto`'s execute_plan does (BL-128, the
        # batch-dispatch sibling of BL-122's cmd_parse/cmd_compare fix).
        print(f"[{label}] {e}", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001
        print(f"[batch] error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    # A source list that resolved to zero documents (an empty or hidden-file-only directory, a
    # zero-match expansion) has nothing for on_progress to loop over above — surface the envelope's
    # own empty_batch warning as the one stderr line a caller watching the terminal (stdout is
    # usually redirected to a file) needs, rather than total silence indistinguishable from a hang.
    for w in env.get("warnings") or []:
        if w.get("code") == "empty_batch":
            print(f"[batch] {w.get('message') or w['code']}", file=sys.stderr)

    print(json.dumps(env, indent=2))
    state = env["status"]["state"]
    return 4 if state == "partial" else (1 if state == "failed" else 0)


def cmd_resume(args) -> int:
    """`openreading resume <RUN_ID>` (internal/design/ledger.md §10): re-derive the run's own identity
    from the LIVE openreading.yaml + registry and refuse by name if it no longer matches the
    header recorded at the run's first arm (AC-4) — a resumed run replays recorded decisions, it
    never falls back to a fresher config. Takes exactly `RUN_ID`; every other option comes from
    the ledger itself (§10)."""
    try:
        # A resumed walk dispatches live for every step not yet terminal in the journal, so a
        # backend prints its stdout advisories here exactly as a fresh `parse` does — and `resume`
        # was the one command not redirecting them, breaking "stdout is one JSON document" on the
        # recovery path, where the caller is most likely to be a script piping into `jq`.
        with contextlib.redirect_stdout(sys.stderr):
            result = api.resume_run(args.run_id)
    except HeaderMismatch as e:
        for field, old, new in e.fields:
            # §10's own transcript names this case "openreading.yaml changed" specifically — every
            # other hard field gets the same shape, naming itself instead.
            what = "openreading.yaml" if field == "config_hash" else field
            print(
                f"[resume] refused: {what} changed since {args.run_id} ({old} -> {new})",
                file=sys.stderr,
            )
        print(
            "[resume] a resumed run replays recorded decisions; start a new run instead",
            file=sys.stderr,
        )
        return 3
    except LookupError as e:
        print(f"[resume] {e}", file=sys.stderr)
        return 3
    except PayloadExpired as e:
        print(f"[resume] {e}", file=sys.stderr)
        return 3
    except PlanExhaustedError as e:
        # Finding 6/7 (Phase C round-1): `cmd_parse` already routes the identical exception
        # through `_print_exhausted` (above) for its own `auth_rejected`-hint enrichment — a resumed
        # walk dispatches live for any step not yet terminal in the journal, so it can reach a real
        # `auth_rejected` response exactly like a fresh `parse` can. Without this clause,
        # `PlanExhaustedError` (a `TerminalError` subclass) fell through to the generic
        # `_CLEAN_EXIT3_ERRORS` branch below and lost that hint.
        _print_exhausted("resume", e, "")
        return 3
    except _CLEAN_EXIT3_ERRORS as e:
        print(f"[resume] {e}", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001
        print(f"[resume] error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    schemas.validate_response(result)
    print(json.dumps(result, indent=2))
    return 0


def cmd_route(args) -> int:
    """`openreading route`: print the compliance-first plan as JSON, and with `--run` execute the
    whole chain. The plan is still printed when the chain is exhausted."""
    try:
        loaded = config.load(args.config)
    except ConfigError as e:
        print(f"[route] {e}", file=sys.stderr)
        return 3
    try:
        req = api.build_request(args.file, "auto")
    except OSError as e:
        print(f"[route] cannot read {args.file}: {_describe_read_error(e)}", file=sys.stderr)
        return 3
    req, router_config = config.apply(req, loaded.policy if loaded else None, RouterConfig())
    plan = Router(build_registry(), router_config).route(req)
    out: dict[str, Any] = {
        "chosen": plan.chosen.descriptor.id if plan.chosen else None,
        "fallbacks": [a.descriptor.id for a in plan.fallbacks],
        "dropped": {
            i: {"stage": dr.stage, "code": dr.code, "reason": dr.detail}
            for i, dr in sorted(plan.dropped.items())
        },
        "terminal_reason": plan.terminal_reason,
    }
    if args.run and plan.chosen:
        # execute the WHOLE chain chosen→fallbacks (compliance already enforced; never widened).
        # Backend stdout advisories (e.g. PyMuPDF find_tables) → stderr so stdout is only the JSON.
        try:
            with contextlib.redirect_stdout(sys.stderr):
                result = execute_plan(plan, req).to_schema_dict()
            out["result"] = result
        except PlanExhaustedError as e:
            trail = "; ".join(f"{t['backend']}:{t['category']}({t['code']})" for t in e.trail)
            _print_exhausted(
                "route --run", e, f"plan exhausted: {trail or 'no eligible backend ran'}"
            )
            print(json.dumps(out, indent=2))  # the plan is still the answer to `route`
            return 3
    print(json.dumps(out, indent=2))
    return 0 if plan.chosen else 4


def cmd_serve(args) -> int:
    """`openreading serve`: build the app, claim the listening socket, then hand the bound socket
    to uvicorn. Why the bind comes first is in the comment below."""
    try:
        import uvicorn
    except ImportError:
        print(
            "[serve] serve needs the [server] extra. Run uv sync --all-extras --dev "
            "in your clone (make sync does the same), or pip install -e '.[server]' "
            "in your virtualenv.",
            file=sys.stderr,
        )
        return 3
    from openreading.server import ServerConfigError, create_app

    try:
        app = create_app(cors_origins=args.cors_origin or None)
    except ServerConfigError as e:
        # A malformed OPENREADING_API_KEYS / _SCOPES is an operator config error, so it belongs on
        # the same rung as every other "cannot run" on this CLI (exit 3) and must be readable by
        # the same log rule: one `[serve] …` line, no traceback. Left uncaught it surfaced as a
        # 12-line stack with the only useful sentence last, which an operator alerting on the
        # `[tag]` convention never matched — a server that refuses to start is exactly the moment
        # that line has to land. Only the message is printed: it names the malformed entry's
        # POSITION and never its VALUE (BL-159 AC-5), and a startup log must not become the place
        # a bearer token leaks.
        print(f"[serve] {e}", file=sys.stderr)
        return 3
    # localhost and ::1 are loopback too. Warning about exposure while binding the loopback
    # interface teaches an operator to skip the one warning that matters.
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print(
            f"[serve] warning: binding {args.host} exposes the server. Anyone who can reach it "
            "spends your vendor keys. Put it behind your own auth or proxy.",
            file=sys.stderr,
        )
    # Claim the listening socket HERE and hand uvicorn the bound socket, rather than a host/port
    # for it to claim later. uvicorn's own startup order is lifespan-first, bind-second, so
    # `INFO: Application startup complete.` is logged BEFORE the port is claimed: a readiness gate
    # grepping the log for that line passes a server that is about to die of a port conflict, and
    # the operator reads a healthy startup followed by an exit, with no line joining the two.
    # Binding first makes every startup line uvicorn prints true at the moment it prints it, and
    # turns a conflict into what every other "cannot run" on this CLI is — one tagged line, exit 3.
    # (Even so, no log line is the readiness contract: poll `GET /healthz` until it answers.)
    #
    # Done by hand rather than with `uvicorn.Config.bind_socket()` only because that helper logs
    # its own "Uvicorn running on ..." line, which uvicorn then logs again when it starts serving;
    # two identical startup lines is a worse thing to hand a responder than these six. Otherwise
    # it is the same sequence, `listen()` included — `loop.create_server(sock=...)` does that.
    import socket

    sock = socket.socket(family=socket.AF_INET6 if ":" in args.host else socket.AF_INET)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((args.host, args.port))
    except OSError as e:
        sock.close()
        print(
            f"[serve] cannot bind {args.host}:{args.port}: {e.strerror or e}. Free it or pass a "
            "different --port.",
            file=sys.stderr,
        )
        return 3
    sock.set_inheritable(True)
    # uvicorn suppresses its own "Uvicorn running on ..." line when it is handed a socket (it
    # assumes `bind_socket()` logged one), so this replaces it — and improves on it: it is printed
    # only once the port is genuinely claimed, it reports the port the kernel actually gave us
    # (`--port 0`), and it names the readiness check, because no log line is a readiness contract
    # and a gate that greps one is a gate that can be fooled.
    bound_host, bound_port = sock.getsockname()[:2]
    print(
        f"[serve] listening on http://{bound_host}:{bound_port}. Readiness: GET /healthz",
        file=sys.stderr,
    )
    try:
        uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port)).run(sockets=[sock])
    finally:
        sock.close()
    return 0


def cmd_backends(args) -> int:
    """List backends and whether they are CONFIGURED here. `--check` additionally probes the named
    backends for real liveness (internal/design/liveness.md §9).

    `--check` never defaults to "everything": a bare `openreading backends` is byte-for-byte what
    it has always been, offline and free. The literal `--check all` exists as explicit typed
    consent to probe every backend that declares a probe — on a CLI, a flag the user typed IS the
    consent that a page load can never be."""
    broker = EnvCredentialBroker()
    slugs = sorted(BUILTIN_ADAPTERS)
    adapters = {s: make_adapter(s) for s in slugs}
    rows = [backend_readiness(adapters[s], broker=broker) for s in slugs]

    requested = _check_targets(getattr(args, "check", None), adapters)
    if requested is None:
        return 3

    if not requested:
        print(f"{'BACKEND':<30} {'TYPE':<18} {'CONFIGURED':<11} MISSING")
        for r in rows:
            miss = missing_reason(r)
            print(
                f"{r.slug:<30} {r.type:<18} {('yes' if r.ready else 'no'):<11} "
                f"{', '.join(miss) or '-'}"
            )
        return 0

    # --check: the probe lane. Reports are printed as they complete so a slow backend never hides
    # the ones that already answered.
    print(f"{'BACKEND':<30} {'PROBE':<10} {'STATUS':<22} {'MEASURED':<9} {'LATENCY':<9} DETAIL")
    for slug in requested:
        report = check_liveness(
            adapters[slug], broker=broker, timeout_s=getattr(args, "timeout", None)
        )
        latency = f"{report.latency_ms:.0f}ms" if report.latency_ms is not None else "-"
        print(
            f"{report.backend:<30} {report.probe.value:<10} {report.status.value:<22} "
            f"{('yes' if report.measured else 'no'):<9} {latency:<9} {report.detail}"
        )
    return 0


def _check_targets(raw: str | None, adapters: dict) -> list[str] | None:
    """Resolve `--check`'s argument to the slugs to probe. None ⇒ a usage error was printed (the
    caller returns 3); an empty list ⇒ no `--check` was given, so nothing is probed.

    `all` means "every backend that DECLARES a probe" rather than literally all fifteen: probing a
    backend whose answer can only ever be the configuration inference spends the user's patience
    for a result `openreading backends` already printed for free."""
    if not raw:
        return []
    if raw.strip() == "all":
        return [s for s, a in adapters.items() if probe_kind(a.descriptor) is not ProbeKind.NONE]
    wanted = [s.strip() for s in raw.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in adapters]
    if unknown:
        print(_unknown_backends_line("backends", unknown, adapters), file=sys.stderr)
        return None
    return list(dict.fromkeys(wanted))


def _unknown_backends_line(tag: str, unknown: list[str], known) -> str:
    """One wording for an unknown backend id, wherever it is typed. Three call sites each
    rendering their own is how two of them ended up withholding the list the reader needs to fix
    the typo."""
    return f"[{tag}] unknown backend(s): {', '.join(unknown)}; known: {', '.join(sorted(known))}"


def _yaml_dump(obj: Any) -> str:
    import yaml  # lazy — only when a strategy command actually renders YAML

    return yaml.safe_dump(obj, sort_keys=False, default_flow_style=False, allow_unicode=True)


def cmd_strategy_show(args) -> int:
    """Dump a strategy or preset. By default prints the body AS WRITTEN (a Plain strategy prints
    Plain); `--longhand` prints the canonical (normalized) tree for either dialect."""
    try:
        loaded = load_config(args.config)
    except ConfigError as e:
        print(f"[strategy show] {e}", file=sys.stderr)
        return 3
    config = loaded.config if loaded else None
    if getattr(args, "longhand", False):
        try:
            tree = normalize_strategy(args.name, config)
        except NormalizeError as e:
            print(f"[strategy show] {e}", file=sys.stderr)
            return 3
        print(_yaml_dump({args.name: tree}), end="")
        return 0
    body = _strategy_body_as_written(args.name, loaded)
    if body is None:
        # The same set `api.run` names when `--strategy` misses, in the same words: the command
        # whose job is browsing strategy names is the last one that should withhold them.
        known = (set(config.strategies) if config else set()) | set(PRESET_NAMES)
        print(
            f"[strategy show] unknown strategy {args.name!r}; defined: {', '.join(sorted(known))}",
            file=sys.stderr,
        )
        return 3
    print(_yaml_dump({args.name: body}), end="")
    return 0


def _strategy_body_as_written(name: str, loaded):
    """The body the user wrote — the pre-desugar source for a file strategy, else the vendored
    preset. The loaded config holds desugared longhand, so a file strategy's source is re-read."""
    from openreading.strategies.presets import PRESETS

    if loaded is not None and name in loaded.config.strategies:
        import yaml

        source = yaml.safe_load(loaded.path.read_text())
        return (source.get("strategies") or {}).get(name)
    return PRESETS.get(name)


def cmd_strategy_list(args) -> int:
    """List built-in presets and (if a config is found) the user's strategies."""
    try:
        loaded = load_config(args.config)
    except ConfigError as e:
        print(f"[strategy list] {e}", file=sys.stderr)
        return 3
    config = loaded.config if loaded else None
    print("PRESETS:")
    for name in sorted(PRESET_NAMES):
        print(f"  {name}")
    if config and config.strategies:
        print("STRATEGIES:" + (f"  (from {loaded.path})" if loaded else ""))
        for name in config.strategy_names():
            print(f"  {name}")
    return 0


def cmd_strategy_validate(args) -> int:
    """Check an openreading.yaml: grammar (schema, via the loader) + world-consistency. Prints
    every error and warning; exit 3 if any error, else 0. The file's own `policy:` block adds a
    context for the steps-unreachable check."""
    try:
        loaded = load_config(args.config)
    except ConfigError as e:  # parse / schema failure — a located grammar error
        # Deliberately left untagged (not [strategy validate]): ConfigError's own message is
        # already "<source>: <detail>", matching ValidationIssue.render()'s "LEVEL source:path:
        # message" shape used by every other line below — locked in by test_cli_validate_error_exit3.
        print(f"ERROR {e}", file=sys.stderr)
        return 3
    if loaded is None:
        print("[strategy validate] no openreading.yaml found (use --config PATH)", file=sys.stderr)
        return 3
    issues = validate_config(loaded.config, raw=loaded.raw, plain_info=loaded.plain_info)
    errors = [i for i in issues if i.level == "error"]
    warnings = [i for i in issues if i.level == "warning"]
    for issue in errors + warnings:
        stream = sys.stderr if issue.level == "error" else sys.stdout
        print(issue.render(str(loaded.path)), file=stream)
    _print_strategy_summaries(loaded)
    if not issues:
        print(f"{loaded.path}: OK ({len(loaded.config.strategies)} strategies)")
    else:
        print(f"{loaded.path}: {len(errors)} error(s), {len(warnings)} warning(s)")
    return 3 if errors else 0


def _dialect_badge(name: str, loaded) -> str:
    from openreading.strategies.plain import first_advanced_key

    info = loaded.plain_info.get(name)
    if info is not None and info.dialect == "plain":
        return "dialect: plain"
    key = first_advanced_key(loaded.config.strategies.get(name))
    return f"dialect: advanced (first advanced key: {key})" if key else "dialect: advanced"


def _print_strategy_summaries(loaded) -> None:
    """Per strategy: the dialect badge, the source YAML as written, and a plain-English summary of
    what it does; then a glossary of the criterion words (`looks bad`, etc.) that appeared. All
    best-effort — never let a summary failure break `validate` (real problems are already reported).
    """
    import textwrap

    import yaml

    from openreading.strategies import normalize_strategy
    from openreading.strategies.describe import CRITERION_GLOSS, criteria_used, describe_strategy

    # `loaded.raw` is post-desugar longhand; re-read the source for the ORIGINAL (as-written) bodies.
    source_bodies: dict = {}
    try:
        source_bodies = (yaml.safe_load(Path(loaded.path).read_text()) or {}).get(
            "strategies"
        ) or {}
    except Exception:
        source_bodies = {}

    used: set[str] = set()
    for name in loaded.config.strategies:
        print(f"  {name}: {_dialect_badge(name, loaded)}")
        body = source_bodies.get(name)
        if body is not None:
            dumped = yaml.safe_dump(body, default_flow_style=None, sort_keys=False).rstrip("\n")
            for line in dumped.splitlines():
                print(f"      {line}")
        try:
            tree = normalize_strategy(name, loaded.config)
            print(f"    → {describe_strategy(tree)}")
            used |= criteria_used(tree)
        except Exception:
            print("    → (no description available)")

    if used:
        pad = max(len(word) for word, _ in CRITERION_GLOSS.values()) + 2
        print("\n  what the words mean:")
        for fam, (word, gloss) in CRITERION_GLOSS.items():
            if fam in used:
                wrapped = textwrap.fill(gloss, width=88, subsequent_indent=" " * (4 + pad))
                print(f"    {word:<{pad}}{wrapped}")


def cmd_strategy_plan(args) -> int:
    """Terraform-style speculative plan: the pruned tree for THIS document + policy, no execution."""
    from openreading.strategies import compile_strategy

    try:
        loaded = load_config(args.config)
    except ConfigError as e:
        print(f"[strategy plan] {e}", file=sys.stderr)
        return 3
    if loaded is None:
        print("[strategy plan] no openreading.yaml found (use --config PATH)", file=sys.stderr)
        return 3
    try:
        req = api.build_request(args.file, "auto")
    except OSError as e:
        print(
            f"[strategy plan] cannot read {args.file}: {_describe_read_error(e)}", file=sys.stderr
        )
        return 3
    req, router_config = config.apply(req, loaded.config.policy, RouterConfig())
    try:
        compiled = compile_strategy(
            req, args.strategy, loaded.config, build_registry(), router_config
        )
    except (NormalizeError, ComplianceRefused) as e:
        print(f"[strategy plan] {e}", file=sys.stderr)
        return 3
    out = {
        "strategy": compiled.name,
        "config_hash": compiled.config_hash,
        "eligible": compiled.eligible,
        "dropped": [d.as_dict() for d in compiled.dropped],
        "tree": compiled.root,
    }
    print(json.dumps(out, indent=2))
    return 0


def _render_orchestration(orch: dict) -> None:
    """One response's orchestration block, as a story. Shared by the single-document path and by
    the per-item walk a folder run needs."""
    print(
        f"strategy {orch.get('strategy')}  →  {orch.get('chosen_backend')} ({orch.get('outcome')})"
    )
    for a in orch.get("attempts", []):
        cost = f"${a['cost_usd']:.4f}" if a.get("cost_usd") else "$0"
        dur = f"{a['duration_ms']}ms" if a.get("duration_ms") is not None else "-"
        print(f"  {a['node']:<16} {a['backend']:<12} {a['category']:<26} {dur:>7}  {cost}")

        def _mark(g):
            return "FIRED" if g["fired"] else ("skipped" if g.get("skipped") else "ok")

        gates = a.get("gates", [])
        if any(g.get("source") for g in gates):
            # Plain dialect (§9): group predicate rows under the source word they compiled from.
            groups: dict[str, list] = {}
            for g in gates:
                groups.setdefault(g.get("source") or "(ungrouped)", []).append(g)
            for source, members in groups.items():
                print(f"      {source}")
                for g in members:
                    print(
                        f"        {g['predicate']:<24} obs={g['observed']} "
                        f"thr={g['threshold']}  {_mark(g)}"
                    )
        else:  # advanced: flat, exactly as before
            for g in gates:
                print(
                    f"      {g['predicate']:<26} obs={g['observed']} "
                    f"thr={g['threshold']}  {_mark(g)}"
                )
    for dec in orch.get("decisions", []):
        where = dec.get("node_path") or dec.get("point") or "?"
        line = (
            f"  decision {where:<20} point={dec.get('point')}  chosen={dec.get('chosen')}  "
            f"decider={dec.get('decider')}"
        )
        if dec.get("downgraded") is not None:
            # The "didn't match configuration" signal (decider.md §3.4) — only shown when a
            # decision point actually resolved to something other than its configured choice.
            line += f"  downgraded={dec['downgraded']}"
        print(line)
    for d in orch.get("dropped", []):
        print(f"  dropped {d['backend']} (stage {d['stage']}: {d['code']})")


def _batch_items(doc: dict) -> list[dict] | None:
    """The items of a batch-result, or None when `doc` is a single response.

    A `parse <folder>` run is one batch-result holding a response per document, so a strategy's
    orchestration sits one level down. Reading only the top level told a reader who had just run
    a strategy over a folder that they had not run one.
    """
    items = doc.get("items")
    return items if isinstance(items, list) and "summary" in doc else None


def cmd_explain(args) -> int:
    """Render a response's orchestration block, or a comparison report, as a human story."""
    try:
        doc = _read_json_or_fail(args.response)
    except _InputFileError as e:
        print(f"[explain] {e}", file=sys.stderr)
        return 3
    if "subjects" in doc and "fields" in doc and "findings" in doc:  # a comparison report
        from openreading.comparison.render import render_table

        print(render_table(doc))
        return 0

    items = _batch_items(doc)
    if items is not None:
        explained = 0
        for item in items:
            where = (item.get("source") or {}).get("relpath") or "?"
            state = item.get("state")
            item_orch = ((item.get("response") or {}).get("orchestration")) or None
            if not item_orch:
                # A skipped or failed item, or one a named backend ran. Name it either way: a
                # document missing from the report is the thing a reader cannot ask about.
                print(f"{where}  ({state}, no orchestration)")
                continue
            print(f"{where}")
            _render_orchestration(item_orch)
            explained += 1
        if not explained:
            print(
                "[explain] no orchestration in any item of this batch-result (was it a"
                " --strategy run?)",
                file=sys.stderr,
            )
            return 3
        return 0

    orch = doc.get("orchestration")
    if not orch:
        print(
            "[explain] no orchestration block in this response (was it a strategy run?)",
            file=sys.stderr,
        )
        return 3
    _render_orchestration(orch)
    return 0


def cmd_replay(args) -> int:
    """Re-execute a strategy taking the LOGGED decision at each decision point (TraceDecider,
    decider.md §5). Deterministic and offline for local backends: the decisions replay exactly;
    a decision point absent from the trace takes the engine default (`trace_missing`)."""
    from openreading.credentials import EnvCredentialBroker
    from openreading.router.clock import RealClock
    from openreading.strategies import compile_strategy, run_strategy

    try:
        trace_doc = _read_json_or_fail(args.trace)
    except _InputFileError as e:
        print(f"[replay] {e}", file=sys.stderr)
        return 3
    orch = trace_doc.get("orchestration") or trace_doc  # a full response OR a bare orchestration
    decisions = orch.get("decisions", [])
    name = args.strategy or orch.get("strategy")
    if not name:
        print(
            "[replay] no --strategy given and the trace carries no strategy name", file=sys.stderr
        )
        return 2
    try:
        loaded = load_config(args.config)
    except (ConfigError, NormalizeError) as e:
        print(f"[replay] {e}", file=sys.stderr)
        return 3
    if loaded is None:
        print("[replay] no openreading.yaml found (use --config PATH)", file=sys.stderr)
        return 3
    try:
        req = api.build_request(args.file, "auto")
    except OSError as e:
        print(f"[replay] cannot read {args.file}: {_describe_read_error(e)}", file=sys.stderr)
        return 3
    req, router_config = config.apply(req, loaded.config.policy, RouterConfig())
    registry = build_registry()
    try:
        compiled = compile_strategy(req, name, loaded.config, registry, router_config)
        # BL-163: a whole-trace check, before any decision point is consulted — `config_hash`
        # captures the compliance posture + eligible/dropped backend set a trace was recorded
        # under, so a mismatch means this trace's logged decisions were made against a DIFFERENT
        # configuration than the one compiling right now, not merely a different document. A trace
        # missing config_hash entirely (an older or hand-built trace) has nothing to compare
        # against and is left to _replay_decision's existing per-decision trace_missing downgrade
        # — this only refuses an EXPLICIT mismatch, never an absence.
        trace_config_hash = orch.get("config_hash")
        if trace_config_hash is not None and trace_config_hash != compiled.config_hash:
            print(
                f"[replay] trace config_hash {trace_config_hash!r} does not match the freshly "
                f"compiled config_hash {compiled.config_hash!r}. Refusing to replay a trace "
                f"recorded under a different configuration or compliance posture.",
                file=sys.stderr,
            )
            return 3
        with contextlib.redirect_stdout(sys.stderr):
            result = run_strategy(
                compiled,
                req,
                registry=registry,
                broker=EnvCredentialBroker(),
                clock=RealClock(),
                replay=decisions,
            )
    except (NormalizeError, ComplianceRefused) as e:
        print(f"[replay] {e}", file=sys.stderr)
        return 3
    except PlanExhaustedError as e:
        _print_exhausted("replay", e, "")
        return 3
    # BL-133: the `except TerminalError` clause formerly here is dead code, confirmed by a
    # full-repo grep for `raise TerminalError(` inside strategies/engine.py and prune.py (no call
    # site) and by 0% coverage — every backend-level TerminalError raised during run_strategy's
    # walk is already absorbed into PlanExhaustedError by a per-node catch (engine.py's three
    # `except (TerminalError, RetryableError, UnsupportedFeatureError, ComplianceRefused)` sites),
    # caught above. The same class of dead clause BL-107 already removed from cmd_calibrate.
    result.response.orchestration = result.orchestration
    out = result.response.to_schema_dict()
    schemas.validate_response(out)
    print(json.dumps(out, indent=2))
    return 0


def cmd_calibrate(args) -> int:
    """Derive gate thresholds from a sample of documents (signals.md §5). A case whose `expected`
    names none of the eval scorer's five recognized dimensions is fine — an ordinary "not labeled
    yet" shape, excluded from `scorer_agreement` rather than silently required (BL-87); the
    report's `n_scored` field says how many cases actually contributed. Runs the strategy's rung-1
    backend over the dataset, scores with the eval scorers, sweeps each gated threshold, and prints
    candidate operating points + a ready-to-paste `escalate_if:` RECOMMENDATION (never rewrites the
    config — the file the user commits is the authority)."""
    from openreading.strategies.calibrate import calibrate_strategy

    try:
        loaded = load_config(args.config)
    except (ConfigError, NormalizeError) as e:
        print(f"[calibrate] {e}", file=sys.stderr)
        return 3
    if loaded is None:
        print("[calibrate] no openreading.yaml found (use --config PATH)", file=sys.stderr)
        return 3
    try:
        # backend stdout advisories (e.g. PyMuPDF) → stderr so stdout is only the JSON report
        with contextlib.redirect_stdout(sys.stderr):
            report = calibrate_strategy(
                args.dataset,
                loaded.config,
                args.strategy,
                build_registry(),
                target_escalation=args.target_escalation,
                max_cost_per_doc=args.max_cost_per_doc,
                router_config=config.router_config(loaded.config.policy),
            )
    # calibrate_strategy resolves and drives its rung-1 backend directly — no Router/execute_plan/
    # run_strategy machinery anywhere on this call graph, so PlanExhaustedError can never be raised
    # here (BL-107 removed the `except PlanExhaustedError` clause that used to sit above this one,
    # confirmed dead: 0% covered, and no test anywhere exercised it).
    # TerminalError covers a rung-1 backend that can't run at all (missing_credentials /
    # auth_rejected) as well as a per-case fault calibrate_strategy's own try/except now converts
    # into a clean, case-naming TerminalError instead of a bare crash (BL-107). RetryableError and
    # UnsupportedFeatureError are the two most plausible outcomes of a real hosted rung-1 backend
    # under this function's own non-overridable 60-second deadline — calibrate_strategy re-raises
    # them under their own type (with the same case-naming context attached), so both need their
    # own clean, coded exit here too, matching every other backend_code already handled below.
    except (
        ValueError,
        FileNotFoundError,
        NormalizeError,
        RetryableError,
        TerminalError,
        UnsupportedFeatureError,
        ComplianceRefused,
    ) as e:
        print(f"[calibrate] {e}", file=sys.stderr)
        return 3
    # n_scored < n_docs (BL-87): scorer_agreement is grounded only in cases whose `expected`
    # named a recognized dimension — an unlabeled sample makes it a flat, precise-looking number
    # that measured nothing, easy to mistake for "measured and found wanting."
    if report.n_scored == 0:
        print(
            f"[calibrate] 0 of {report.n_docs} cases were scored. None named a dimension "
            "scorers.score() recognizes, so scorer_agreement is not measured at any threshold "
            "(only escalation_rate/cost_per_doc are real signal here)",
            file=sys.stderr,
        )
    elif report.n_scored < report.n_docs:
        print(
            f"[calibrate] only {report.n_scored} of {report.n_docs} cases were scored. The rest "
            "named no recognized `expected` dimension, so scorer_agreement reflects the scored "
            "subset only",
            file=sys.stderr,
        )
    print(json.dumps(report.as_dict(), indent=2))
    return 0


def _render_leaderboard_table(report: Any) -> str:
    """Human `--format table` (the default — BL-160's UX names the ranked table as the primary
    output, `--format json` as the script-consumable opt-in, the mirror of compare/calibrate's own
    json-default). The dataset identity prints first so a screenshot of the ranking is never read
    as a universal verdict divorced from the N documents that actually produced it (Risks)."""
    d = report.dataset
    lines = [
        f"dataset: {d.path}  ({d.case_count} case(s): {', '.join(d.case_names)})",
        "",
        f"{'rank':>4}  {'backend':<28} {'mean':>6} {'scored':>7} {'cost/doc':>10} "
        f"{'errors':>7}  dimensions",
    ]
    for b in report.backends:
        dims = " ".join(f"{k}={v:.2f}" for k, v in b.dimensions.items())
        nd = "  [non-deterministic: single sample]" if b.non_deterministic else ""
        # `scored` is the denominator `mean` rests on, and the schema marks n_scored required for
        # reading mean_score at all. Without it a backend that never scored a case and one that
        # scored 0.0 on every case it ran are the same row, and `errors` does not separate them —
        # a case carrying no recognized `expected` dimension is unscored without erroring. A mean
        # over zero scored cases is not a measurement, so it prints as an em dash rather than a
        # 0.000 a reader would compare against a measured 0.000.
        mean = f"{b.mean_score:>6.3f}" if b.n_scored else f"{'—':>6}"
        lines.append(
            f"{b.rank:>4}  {b.backend_id:<28} {mean} {f'{b.n_scored}/{b.n_cases}':>7} "
            f"{b.cost_per_doc:>10.4f} {b.errors:>7}  {dims}{nd}"
        )
    lines.append("")
    lines.append("per-case result:")
    tally = {"win": 0, "tie": 0, "all-zero": 0, "no result": 0}
    for c in report.cases:
        scores = ", ".join(
            f"{bid}={s:.2f}" if s is not None else f"{bid}=—" for bid, s in c.scores.items()
        )
        # The report's `winner` breaks a tie alphabetically (leaderboard-report.v0.1.json), which
        # is the right rule for a byte-stable JSON field and the wrong thing to print to a human:
        # naming one backend on a tie, or on a case every backend scored 0.00, invites a per-case
        # win tally that reads as a sweep when nothing was won. The JSON field is unchanged; the
        # human block states the outcome it can actually support, and totals it.
        real = {bid: s for bid, s in c.scores.items() if s is not None}
        if not real:
            outcome, bucket = "no result (no backend produced a score)", "no result"
        else:
            top = max(real.values())
            leaders = sorted(bid for bid, s in real.items() if s == top)
            if top == 0.0:
                outcome, bucket = "no winner (every scored backend got 0.00)", "all-zero"
            elif len(leaders) > 1:
                outcome, bucket = f"tie={','.join(leaders)}", "tie"
            else:
                outcome, bucket = f"winner={leaders[0]}", "win"
        tally[bucket] += 1
        lines.append(f"  {c.name}: {outcome}  ({scores})")
    lines.append("")
    lines.append(
        f"tally over {len(report.cases)} case(s): "
        + ", ".join(f"{n} {label}" for label, n in tally.items())
    )
    return "\n".join(lines)


def cmd_leaderboard(args) -> int:
    """Rank N registered backends on the SAME dataset.case.json corpus through the unchanged
    evals.runner.run_case path (internal/product/specs/eval-leaderboard.product-spec.md) — one
    BenchmarkReport: measured mean score, per-dimension breakdown, a per-case winner table, an
    error tally, and each backend's cost basis reported alongside its score. Reuses run_case's
    existing per-case compliance gate; never a second scoring or gating path (AC-1/AC-2)."""
    from openreading.evals.leaderboard import run_leaderboard

    try:
        loaded = config.load(args.config)
    except ConfigError as e:
        print(f"[leaderboard] {e}", file=sys.stderr)
        return 3

    if args.all_ready:
        broker = EnvCredentialBroker()
        ids = [
            r.slug
            for r in (
                backend_readiness(make_adapter(s), broker=broker) for s in sorted(BUILTIN_ADAPTERS)
            )
            if r.ready
        ]
    else:
        ids = [b.strip() for b in (args.backends or "").split(",") if b.strip()]
        unknown = [b for b in ids if b not in BUILTIN_ADAPTERS]
        if unknown:
            print(_unknown_backends_line("leaderboard", unknown, BUILTIN_ADAPTERS), file=sys.stderr)
            return 2
    if len(ids) < 2:
        print(
            f"[leaderboard] need at least two backends to rank (got {len(ids)}). "
            "Pass --backends a,b or --all-ready.",
            file=sys.stderr,
        )
        return 2

    try:
        # backend stdout advisories (e.g. PyMuPDF) → stderr so stdout is only the rendered report.
        with contextlib.redirect_stdout(sys.stderr):
            report = run_leaderboard(
                args.dataset,
                ids,
                build_registry(),
                router_config=config.router_config(loaded.policy if loaded else None),
            )
    # Mirrors cmd_calibrate's own except tuple for the identical dataset-driven shape: a per-case
    # backend fault never reaches here (run_case/run_dataset are unchanged and always return a
    # scored CaseResult — AC-1/AC-2), so _CLEAN_EXIT3_ERRORS is defensive symmetry with
    # compare/calibrate's coded-exit family (AC-6) rather than a path real backend behavior takes
    # today; ValueError/FileNotFoundError cover an empty/missing dataset_dir or (defensively, the
    # CLI already validated every id against BUILTIN_ADAPTERS above) an unregistered backend.
    except (ValueError, FileNotFoundError, *_CLEAN_EXIT3_ERRORS) as e:
        print(f"[leaderboard] {e}", file=sys.stderr)
        return 3

    if args.format == "json":
        print(json.dumps(report.to_schema_dict(), indent=2))
    else:
        print(_render_leaderboard_table(report))
    return 0


def _benchmark_descriptor(args):
    """Resolve one public benchmark and print lookup failures consistently."""
    from openreading.evals.benchmarks import get_benchmark

    try:
        return get_benchmark(args.benchmark)
    except KeyError as exc:
        print(f"[benchmark] {exc.args[0]}", file=sys.stderr)
        return None


def _check_benchmark_terms(args, descriptor) -> bool:
    """Apply the profile's lane gate before any optional import or download."""
    from openreading.evals.benchmarks import BenchmarkTermsError, require_benchmark_terms

    try:
        require_benchmark_terms(
            descriptor,
            allow_research_only=args.allow_research_only,
            allow_unverified_terms=args.allow_unverified_terms,
        )
    except BenchmarkTermsError as exc:
        print(f"[benchmark] {exc}", file=sys.stderr)
        return False
    return True


def cmd_benchmark_list(args) -> int:
    """List static public benchmark facts without importing an optional package."""
    from openreading.evals.benchmarks import list_benchmarks

    print(f"{'id':<20} {'status':<10} {'terms':<15} dimensions")
    for descriptor in list_benchmarks():
        print(
            f"{descriptor.id:<20} {descriptor.status:<10} "
            f"{descriptor.license_lane:<15} {', '.join(descriptor.dimensions)}"
        )
    return 0


def cmd_benchmark_show(args) -> int:
    """Show publisher links, separate terms, scale, and installation needs."""
    descriptor = _benchmark_descriptor(args)
    if descriptor is None:
        return 2
    print(f"{descriptor.title} ({descriptor.id})")
    print(f"status: {descriptor.status}")
    print(f"terms lane: {descriptor.license_lane}")
    print(f"source: {descriptor.source_url}")
    print(f"data license: {descriptor.data_license}")
    print(f"data terms: {descriptor.data_license_url}")
    print(f"code license: {descriptor.code_license}")
    print(f"code terms: {descriptor.code_license_url}")
    print(f"scorer revision: {descriptor.default_revision}")
    print(f"dimensions: {', '.join(descriptor.dimensions)}")
    if descriptor.estimated_documents is not None:
        print(f"published scale: {descriptor.estimated_documents} documents")
    if descriptor.estimated_pages is not None:
        print(f"published pages: {descriptor.estimated_pages}")
    if descriptor.package and descriptor.install_extra:
        print(f"package: {descriptor.package}")
        print(f"install: pip install 'openreading[{descriptor.install_extra}]'")
        print(f"presets: {', '.join(descriptor.presets)}")
    return 0


def _benchmark_targets(args):
    """Parse repeated target flags into the collision-free target contract.

    A target repeated verbatim is dropped, in first-seen order, and said out loud. One target's
    publisher pipeline name is derived from the target plus its configuration, so a duplicate is
    not a second measurement: it reruns one pipeline into one directory and then renders that
    pipeline twice in the cross-target leaderboard, side by side, as though two things had been
    compared. Two rows that are the same run is the one thing a bake-off must never show.
    """
    from openreading.evals.targets import BenchmarkTarget

    targets = []
    for value in args.target or []:
        try:
            target = BenchmarkTarget.parse(value)
        except ValueError as exc:
            print(f"[benchmark] {exc}", file=sys.stderr)
            return None
        # `BenchmarkTarget.parse` checks the SYNTAX, and it lives in `openreading.evals`, which
        # cannot see the adapter registry without the dependency running the wrong way. Identity
        # is checked here instead, where `compare` and `leaderboard` already check theirs. A typo
        # otherwise priced a run that could not exist, and said nothing.
        if target.kind == "backend" and target.name not in BUILTIN_ADAPTERS:
            print(
                _unknown_backends_line("benchmark", [target.name], BUILTIN_ADAPTERS),
                file=sys.stderr,
            )
            return None
        if target in targets:
            print(f"[benchmark] ignoring repeated target {target.reference}", file=sys.stderr)
            continue
        targets.append(target)
    return targets


def _render_benchmark_estimate(descriptor, preset: str, targets) -> str:
    """Price the whole published corpus, in pages, before anything is downloaded.

    Pages, not documents, because every hosted backend charges per page and the two differ by a
    lot. This is the ceiling: `run` defaults to a handful of documents and prints the real count.
    """
    from openreading.evals.preflight import _backend_target_cost

    lines = [f"estimate: {descriptor.id} {preset}, {len(targets)} target(s)"]
    if preset != "full":
        lines.append("documents: publisher smoke subset, counted after preparation")
        lines.append("`benchmark run` prints the real page count and cost before it spends")
        return "\n".join(lines)

    documents = descriptor.estimated_documents
    pages = descriptor.estimated_pages
    lines.append(f"documents: {documents if documents is not None else 'publisher-defined'}")
    lines.append(f"pages (the billing unit): {pages if pages is not None else 'publisher-defined'}")
    if pages is None:
        return "\n".join(lines)
    low = high = 0.0
    for target in targets:
        if target.kind == "strategy":
            lines.append(
                f"  {target.reference}: not priced (a strategy escalates, so one document is one "
                "or more billed calls)"
            )
            continue
        cost = _backend_target_cost(target.reference, target.name, pages)
        if cost.priced:
            low += cost.low_usd or 0.0
            high += cost.high_usd or 0.0
            lines.append(f"  {target.reference}: ${cost.low_usd:.2f} to ${cost.high_usd:.2f}")
        else:
            lines.append(f"  {target.reference}: not priced ({cost.note})")
    if targets:
        lines.append(f"  total (priced targets): ${low:.2f} to ${high:.2f}")
    lines.append("  a range from each backend's declared per-page rates, not a quote")
    return "\n".join(lines)


def cmd_benchmark_estimate(args) -> int:
    """Print publisher scale and target-call counts without running a backend."""
    descriptor = _benchmark_descriptor(args)
    if descriptor is None:
        return 2
    # A cataloged profile has published scale and no way to spend it. Printing "target calls:
    # 1000000" for one reads as a run you could start, and the next command is the one that says
    # no. Refuse here, where the numbers would otherwise be the only answer.
    if descriptor.status != "runnable":
        print(
            f"[benchmark] {descriptor.id} is cataloged for discovery but has no runnable profile",
            file=sys.stderr,
        )
        return 2
    targets = _benchmark_targets(args)
    if targets is None:
        return 2
    print(_render_benchmark_estimate(descriptor, args.preset, targets))
    return 0


def cmd_benchmark_prepare(args) -> int:
    """Prepare a publisher dataset after its terms gate passes."""
    from openreading.evals.official import (
        BenchmarkDependencyError,
        BenchmarkProfileError,
        prepare_official_benchmark,
    )

    descriptor = _benchmark_descriptor(args)
    if descriptor is None or not _check_benchmark_terms(args, descriptor):
        return 2
    try:
        path = prepare_official_benchmark(
            descriptor.id,
            cache_dir=Path(args.cache_dir),
            preset=args.preset,
            force=args.force,
        )
    except (BenchmarkDependencyError, BenchmarkProfileError, ValueError) as exc:
        print(f"[benchmark] {exc}", file=sys.stderr)
        return 2
    print(f"prepared: {path}")
    return 0


def _benchmark_subset(args, descriptor, data_dir: Path, targets) -> Path | None:
    """Cut the prepared corpus down to what this run touches, price it, and ask before spending.

    Returns the corpus directory to hand the publisher, or None when the reader declined or the
    selection could not be resolved. A run covering the whole prepared corpus uses it in place,
    because copying gigabytes to change nothing is its own kind of surprise.
    """
    import hashlib

    from openreading.evals.preflight import confirm, estimate_cost
    from openreading.evals.subset import CorpusError, materialize_subset, plan_subset

    try:
        plan = plan_subset(data_dir, limit=args.limit, names=tuple(args.doc or ()))
    except (CorpusError, ValueError) as exc:
        print(f"[benchmark] {exc}", file=sys.stderr)
        return None

    estimate = estimate_cost(plan, targets)
    print(estimate.render())
    for document in plan.documents:
        print(f"  document: {document.doc_id}")
    if not confirm(estimate, assume_yes=args.yes):
        print("[benchmark] stopped before spending", file=sys.stderr)
        return None

    if plan.is_complete:
        return data_dir
    # Keyed on the chosen documents, so the same --limit reuses one directory and the publisher's
    # own resume sees the corpus it saw last time.
    signature = hashlib.sha256(
        "\n".join(document.doc_id for document in plan.documents).encode()
    ).hexdigest()[:10]
    destination = Path(args.cache_dir) / descriptor.id / "subsets" / signature
    return materialize_subset(plan, destination)


def cmd_benchmark_run(args) -> int:
    """Prepare once, then run each backend or strategy through the official scorer."""
    from openreading.evals.official import (
        BenchmarkDependencyError,
        BenchmarkProfileError,
        build_official_comparison,
        prepare_official_benchmark,
        run_official_benchmark,
    )
    from openreading.evals.report import ReportError

    descriptor = _benchmark_descriptor(args)
    if descriptor is None or not _check_benchmark_terms(args, descriptor):
        return 2
    targets = _benchmark_targets(args)
    if targets is None:
        return 2
    if not targets:
        print(
            "[benchmark] run needs at least one --target backend:NAME or strategy:NAME",
            file=sys.stderr,
        )
        return 2
    # Checked before preparation, not inside the dispatcher. The publisher downloader runs first
    # and a full ParseBench set is gigabytes, so a rejected --jobs used to cost that download.
    if args.jobs < 1:
        print("[benchmark] --jobs must be at least 1", file=sys.stderr)
        return 2
    try:
        data_dir = prepare_official_benchmark(
            descriptor.id,
            cache_dir=Path(args.cache_dir),
            preset=args.preset,
            force=False,
        )
        # The corpus is on disk now, so size and price the run from the documents it will really
        # touch rather than from the publisher's headline totals. Downloading is free; the calls
        # after this point are not.
        run_dir = _benchmark_subset(args, descriptor, data_dir, targets)
        if run_dir is None:
            return 2
        from openreading.evals.report import read_run, render, write_manifest
        from openreading.evals.subset import plan_subset

        failed = False
        runs = []
        for target in targets:
            result = run_official_benchmark(
                descriptor.id,
                target,
                data_dir=run_dir,
                output_dir=Path(args.output_dir),
                preset=args.preset,
                config=args.config,
                jobs=args.jobs,
                force=args.force,
            )
            runs.append(result)
            if result.exit_code == 0:
                print(
                    f"completed: {target.reference} as {result.pipeline_name} in {result.output_dir}"
                )
            else:
                failed = True
                print(
                    f"[benchmark] {target.reference} failed with exit code {result.exit_code}",
                    file=sys.stderr,
                )
        # The publisher's metadata never records which OpenReading target made which pipeline,
        # so write that mapping beside its artifacts before anything tries to read them back.
        write_manifest(
            Path(args.output_dir),
            benchmark_id=descriptor.id,
            preset=args.preset,
            documents=tuple(
                doc.doc_id for doc in plan_subset(run_dir, limit=0, names=()).documents
            ),
            targets=tuple((run.target.reference, run.pipeline_name) for run in runs),
        )
        if len([run for run in runs if run.exit_code == 0]) >= 2:
            comparison = build_official_comparison(
                descriptor.id, runs, output_dir=Path(args.output_dir)
            )
            if comparison.exit_code == 0:
                print(f"comparison: {comparison.artifact}")
            else:
                failed = True
                print(
                    f"[benchmark] official comparison failed with exit code {comparison.exit_code}",
                    file=sys.stderr,
                )
        # The whole reason the run was started. Printed here so the answer is in the terminal
        # rather than only in an HTML file the reader has to go open.
        try:
            print()
            print(render(read_run(Path(args.output_dir))))
        except ReportError as exc:  # a publisher that wrote nothing readable
            print(f"[benchmark] {exc}", file=sys.stderr)
        return 1 if failed else 0
    except (BenchmarkDependencyError, BenchmarkProfileError, ValueError) as exc:
        print(f"[benchmark] {exc}", file=sys.stderr)
        return 2
    except _CLEAN_EXIT3_ERRORS as exc:
        print(f"[benchmark] {exc}", file=sys.stderr)
        return 3


def cmd_rules(args) -> int:
    """Generate publisher rules from the expectations a dataset's cases already carry."""
    from openreading.evals.rules import suggest_rules

    # Read the case files directly rather than through `load_dataset`, which resolves documents
    # and builds a request per case. Generating rules needs neither a backend nor the PDF bytes.
    sources = sorted(Path(args.dataset).glob("*/case.json"))
    if not sources:
        print(f"[rules] no <case>/case.json under {args.dataset}", file=sys.stderr)
        return 3
    written = 0
    for source in sources:
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[rules] {source}: {exc}", file=sys.stderr)
            return 3
        name = payload.get("name") or source.parent.name
        expected = payload.get("expected") or {}
        if "rules" in expected and not args.force:
            print(f"[rules] {name}: already has rules, skipped (--force to replace)")
            continue
        suggested = suggest_rules(expected)
        if not suggested:
            known = ", ".join(sorted(expected)) or "an empty expected"
            print(f"[rules] {name}: nothing to generate from {known}")
            continue
        if not args.write:
            print(f"{name}: {len(suggested)} rule(s) would be added")
            print(json.dumps(suggested, indent=2))
            continue
        expected["rules"] = suggested
        payload["expected"] = expected
        source.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        written += 1
        print(f"{name}: wrote {len(suggested)} rule(s)")
    if not args.write:
        # Printing by default, because this rewrites files a person hand-labeled.
        print("\nnothing written. Re-run with --write to apply.", file=sys.stderr)
    else:
        print(
            f"\nwrote rules into {written} case(s). Add `text_absent` strings to a case to "
            "generate the `absent` rules that catch invented content.",
            file=sys.stderr,
        )
    return 0


def cmd_benchmark_report(args) -> int:
    """Read a finished run's publisher artifacts and print the comparison."""
    from openreading.evals.report import ReportError, read_run, render, to_json

    try:
        reports = read_run(Path(args.output_dir))
    except ReportError as exc:
        print(f"[benchmark] {exc}", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(to_json(reports), indent=2))
    else:
        print(render(reports))
    return 0


def cmd_strategy_normalize(args) -> int:
    """Print the whole config's strategies in canonical longhand YAML (the `docker compose
    config` analog). Requires a config file."""
    try:
        loaded = load_config(args.config)
    except (ConfigError, NormalizeError) as e:
        print(f"[strategy normalize] {e}", file=sys.stderr)
        return 3
    if loaded is None:
        print("[strategy normalize] no openreading.yaml found (use --config PATH)", file=sys.stderr)
        return 3
    try:
        longhand = normalize_config(loaded.config)
    except NormalizeError as e:
        print(f"[strategy normalize] {e}", file=sys.stderr)
        return 3
    print(_yaml_dump({"version": loaded.config.version, "strategies": longhand}), end="")
    return 0


def _corpus_labels(inputs: list[str]) -> list[str]:
    """Label each batch run by its file stem, disambiguating collisions with #2/#3…"""
    out: list[str] = []
    seen: dict[str, int] = {}
    for p in inputs:
        stem = Path(p).stem
        n = seen.get(stem, 0)
        seen[stem] = n + 1
        out.append(stem if n == 0 else f"{stem}#{n + 1}")
    return out


def cmd_compare(args) -> int:
    """Compare N response envelopes (files) or fan one document across backends, then render the
    delta. Pure comparison; fan-out is CLI-only sugar over independent DIRECT parses."""
    import difflib

    from openreading.comparison import CompareInputError
    from openreading.comparison.corpus import (
        corpus_pairs,
        corpus_report_dict,
        is_batch_envelope,
        render_corpus_diffs,
        render_corpus_table,
    )
    from openreading.comparison.ingest import load_subjects
    from openreading.comparison.render import render_diffs, render_markdown, render_table
    from openreading.comparison.report import build_report
    from openreading.comparison.stances import load_truth, resolve_baseline
    from openreading.evals.scorers import canonical_text

    responses: list[dict[str, Any]] = []
    sources: list[str] = []

    if args.from_response:
        try:
            doc = _read_json_or_fail(args.from_response)
        except _InputFileError as e:
            print(f"[compare] {e}", file=sys.stderr)
            return 5
        orch = doc.get("orchestration") or {}
        cands = orch.get("candidates") or []
        if not cands:
            # The flag cannot retain a response that never completed, such as a cancelled race
            # loser. Name the input limitation before suggesting another potentially billed run.
            attempts = len(orch.get("attempts") or [])
            if is_batch_envelope(doc):
                why = "this is a batch-result. Pass one item's response saved with parse --save-dir"
            elif not orch:
                why = "this response has no orchestration block, so it was not a strategy run"
            elif attempts <= 1:
                why = (
                    f"strategy '{orch.get('strategy')}' resolved on its first rung, so no branch"
                    " lost and there is nothing to compare against"
                )
            else:
                why = "this run kept no candidates"
            print(
                f"[compare] --from: {why}. Candidates require completed parallel alternatives"
                " and --keep-candidates. Sequential steps retain none. A race can cancel them."
                " Use a compare: step to wait for alternatives. See openreading help chaining.",
                file=sys.stderr,
            )
            return 5
        winner = {k: v for k, v in doc.items() if k != "orchestration"}  # drop the trace itself
        responses = [winner, *(c["response"] for c in cands)]
        sources = ["candidate"] * len(responses)
    elif args.backends or args.all_ready:
        if len(args.inputs) != 1:
            print("[compare] fan-out compares ONE document across backends", file=sys.stderr)
            return 2
        # --all-ready silently wins over --backends, so the typed list is thrown away whole, an
        # unknown id in it included. Refusing is the only way the reader learns that.
        if args.backends and args.all_ready:
            print(
                "[compare] --backends and --all-ready are alternatives. Pass one.",
                file=sys.stderr,
            )
            return 2
        doc = args.inputs[0]
        # Resolve the source once, before any backend runs. Left to the fan-out loop, a mistyped
        # filename came back from the adapter as a raw `SourceNotFoundError: [Errno 2]` at exit 1,
        # and a directory as an `IsADirectoryError`. `parse` refuses both at exit 2 with a
        # sentence, for the reason its own handler records: an errno is not something the reader
        # who mistyped a path can act on.
        if not is_url(doc):
            source = Path(doc)
            if source.is_dir():
                print(
                    f"[compare] fan-out compares ONE document, and '{doc}' is a directory. Parse"
                    " the folder once per backend and compare the two envelopes:"
                    f" `openreading parse {doc} --backend A > a.json`, the same for B, then"
                    " `openreading compare a.json b.json`.",
                    file=sys.stderr,
                )
                return 2
            if not source.exists():
                print(f"[compare] cannot read {doc}: no such file or directory", file=sys.stderr)
                return 2
        if args.all_ready:
            broker = EnvCredentialBroker()
            ids = [
                r.slug
                for r in (
                    backend_readiness(make_adapter(s), broker=broker)
                    for s in sorted(BUILTIN_ADAPTERS)
                )
                if r.ready
            ]
        else:
            ids = [b.strip() for b in args.backends.split(",") if b.strip()]
            unknown = [b for b in ids if b not in BUILTIN_ADAPTERS]
            if unknown:
                print(_unknown_backends_line("compare", unknown, BUILTIN_ADAPTERS), file=sys.stderr)
                return 2
        if len(ids) < 2:
            print(
                f"[compare] need at least two backends to compare (got {len(ids)}). "
                "Pass --backends a,b or --all-ready.",
                file=sys.stderr,
            )
            return 2
        save = Path(args.save_dir) if args.save_dir else None
        if save:
            save.mkdir(parents=True, exist_ok=True)
        # BL-169: same seconds-to-ms conversion as parse's own --deadline; applied identically to
        # every fanned-out backend, since compare shares parse's exact original Bug B exposure
        # (api.run(doc, backend=bid) against the generic 120s default with no escape hatch).
        deadline_ms = int(args.deadline_s * 1000) if args.deadline_s is not None else None
        # Fan-out is serial: deterministic subject order and one hosted call in flight at a time,
        # so a wide --all-ready run cannot stampede provider rate limits.
        for bid in ids:
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    r = api.run(doc, backend=bid, deadline_ms=deadline_ms)
            except _CLEAN_EXIT3_ERRORS as e:
                # missing creds → names vars + signup; a RetryableError (rate-limit exhaustion, or
                # a poll job past its deadline/MAX_CONSECUTIVE_FAULTS) gets the same clean exit (BL-122).
                # An UnsupportedFeatureError gets it too (BL-129) — this generic handler has no
                # per-feature detail to add, so it prints the plainer `[{bid}] {e}` form rather than
                # cmd_parse's own dedicated clause's `unsupported feature ({e.feature}): {e}` — a
                # deliberate match to every other member's own message shape here, not an oversight.
                print(f"[{bid}] {e}", file=sys.stderr)
                return 3
            except Exception as e:  # noqa: BLE001
                print(f"[{bid}] error: {type(e).__name__}: {e}", file=sys.stderr)
                return 1
            responses.append(r)
            sources.append("fanout")
            if save:
                (save / f"{bid}.json").write_text(json.dumps(r, indent=2))
    else:
        if len(args.inputs) < 2:
            print(
                "[compare] needs at least two subjects: two or more response JSON files, one "
                "document with --backends or --all-ready, or --from a run saved with "
                "--keep-candidates",
                file=sys.stderr,
            )
            return 2
        for p in args.inputs:
            try:
                responses.append(_read_json_or_fail(p))
            except _InputFileError as e:
                print(f"[compare] {e}", file=sys.stderr)
                return 5
            sources.append("file")

    # Corpus mode (Manifest v0.6 §8): every subject is a batch-result envelope → pair documents
    # across the runs and emit a corpus report. Mixing batch + single-response subjects is an error.
    batch_flags = [is_batch_envelope(r) for r in responses]
    if any(batch_flags):
        if not all(batch_flags):
            print("[compare] cannot mix batch-result and single-response subjects", file=sys.stderr)
            return 2
        if len(responses) < 2:
            print("[compare] corpus compare needs at least two batch runs", file=sys.stderr)
            return 2
        if args.format == "diff":
            print(
                "[compare] --format diff is 2-way text only; use diffs/table/json for corpus",
                file=sys.stderr,
            )
            return 2
        # Corpus mode returns before any of these three is read, so accepting them silently would
        # report a scored run that never scored anything, including a --truth path that does not
        # exist, and exit 0.
        if args.baseline or args.truth or args.show_agreements:
            print(
                "[compare] --baseline, --truth and --show-agreements apply to "
                "single-response subjects. A corpus compare accepts none of them.",
                file=sys.stderr,
            )
            return 2
        labels = _corpus_labels(list(args.inputs))
        pairs = corpus_pairs(responses, labels)
        if args.format == "json":
            print(
                json.dumps(
                    corpus_report_dict(pairs, responses, labels, list(args.inputs)), indent=2
                )
            )
        elif args.format == "diffs":
            print(render_corpus_diffs(pairs, labels))
        else:  # table / md → the per-document verdict table
            print(render_corpus_table(pairs, labels))
        return 0

    if args.format == "diff" and len(responses) != 2:
        print("[compare] --format diff requires exactly two subjects", file=sys.stderr)
        return 2

    try:
        subjects = load_subjects(responses, sources=sources)
        baseline_label = resolve_baseline(args.baseline, subjects) if args.baseline else None
        truth = load_truth(args.truth) if args.truth else None
        report = build_report(subjects, baseline_label=baseline_label, truth=truth)
    except CompareInputError as e:
        print(f"[compare] {e}", file=sys.stderr)  # re-run with --keep-candidates, or fix inputs
        return 5

    if args.format == "json":
        print(json.dumps(report, indent=2))
    elif args.format == "table":
        print(render_table(report, show_agreements=args.show_agreements))
    elif args.format == "md":
        print(render_markdown(report, show_agreements=args.show_agreements))
    elif args.format == "diffs":  # N-way content deltas + payload diff (no structure noise)
        print(render_diffs(subjects, report, baseline_label=baseline_label))
    else:  # diff (exactly two subjects)
        a, b = subjects[0], subjects[1]
        diff = difflib.unified_diff(
            canonical_text(a.response).splitlines(),
            canonical_text(b.response).splitlines(),
            fromfile=a.label,
            tofile=b.label,
            lineterm="",
        )
        print("\n".join(diff))
        conflicts = [
            r for r in report["fields"]["rows"] if r["verdict"] in ("disagree", "partial", "unique")
        ]
        if conflicts:
            print("\nFIELD DELTAS")
            for r in conflicts:
                vals = ", ".join(
                    f"{lbl}={bs['value']}" for lbl, bs in r["by_subject"].items() if bs["present"]
                )
                print(f"  {r['key']} [{r['verdict']}]  {vals}")
    return 0


# --- help text -----------------------------------------------------------------------------
# One epilog per addressable command, keyed by the path a reader types after `openreading`.
# Each carries runnable examples, the verb that consumes this verb's output, the exit codes THIS
# command can actually return, and a pointer to its chapter. Each stays inside one screen: a flag
# page a reader has to scroll is a flag page a reader stops reading, and the long form already
# has a home in `openreading help`. Nothing here restates a default that the flag's own `help=`
# already carries, because two sites for one number is how one of them goes stale.
EPILOGS = {
    "parse": """\
Examples:
  openreading parse examples/ --backend pymupdf > all.json   # a whole folder
  openreading parse examples/ --backend pymupdf --jobs 4 --save-dir out/
  openreading parse 'scans/**/*.png' --backend tesseract     # a glob, quoted
  openreading parse examples/john_smith_1000_2026_01.pdf --backend pymupdf
  openreading parse examples/ --no-strategy         # let the router choose
  openreading parse doc.pdf --strategy fast > run.json   # follow a plan

One file or URL prints one response. A folder, a glob, or two or more
arguments prints one batch-result over all of them. Unsupported formats are
skipped in a batch. One file with an unsupported extension exits 3.

Then:
  openreading compare mu.json te.json --format table   # where they differ
  openreading explain run.json     # what that --strategy run decided, and why

Exits: 0 ok. 2 usage, or a source that does not exist. 3 cannot run (a
missing key, a refused feature). 4 batch partial. 1 nothing succeeded, an
empty folder included. 6 interrupted with OPENREADING_LEDGER armed.

More: openreading help parse, openreading help batch""",
    "resume": """\
Examples:
  export OPENREADING_LEDGER=./.openreading  # arm the journal before you run
  openreading parse big.pdf --strategy offline_first   # Ctrl-C gives an id
  openreading resume 7dbf6b71-adb5-4e90-9188-a184fdba9d05 > resumed.json
  ls $OPENREADING_LEDGER/*.header.json    # find an id nobody wrote down

The journal is the on-disk record of a run's steps. A run is resumable once
the ledger was armed for it, whether it went on to succeed, was interrupted,
or crashed. Every step already finished replays from the journal with no
network call, and only what was never reached runs for real. Only a strategy
dispatch journals, so a named --backend run writes nothing while looking
armed. A batch names no single run ID. RUN_ID is the only run option here.

Then:
  openreading explain resumed.json     # what the finished run decided

Exits: 0 ok. 3 an unknown run id, OPENREADING_LEDGER unset, payloads already
expired, or a refusal because openreading.yaml changed since the first run.
1 anything else.

More: openreading help resume, openreading help exit-codes""",
    "route": """\
Examples:
  printf 'version: 1\\npolicy: {require_baa: true, no_train_on_data: true}\\n' \\
    > openreading.yaml
  openreading route examples/john_smith_1000_2026_01.pdf
  openreading route doc.pdf --run > out.json

A policy is the policy: block of your openreading.yaml, and these nine keys
are the whole grammar. An unknown key is refused by name, at exit 3:
  require_baa   no_train_on_data   data_region   require_local
  max_retention   optimize_for
  allow_unverified_compliance   train_optout_confirmed   baa_tier_confirmed
Nothing widens the set a policy allows. The plan prints as JSON either way,
naming every dropped backend with the stage and code that dropped it.

Then:
  openreading backends --check pymupdf   # is the chosen backend answering

Exits: 0 a plan. 4 an empty plan, also with --run. The plan still prints.
3 an openreading.yaml that will not load, an unreadable document, or --run on
a plan every backend in which failed.

More: openreading help compliance, openreading help backends""",
    "backends": """\
Examples:
  openreading backends                   # what runs here, offline and free
  openreading backends --check pymupdf   # probe one backend for real
  openreading backends --check all       # probe every one that has a probe
  openreading backends --env-file ci.env # resolve keys from a file
  openreading backends 2>/dev/null | awk '$3=="yes" {print $1}'  # ready ids

Configured means the extra is installed and the keys resolve. It does not
mean reachable, so a URL pointing at a dead port is still configured. --check
answers that other question by calling the backend, and it is never implicit,
because a flag you typed is consent a page load can never be. Read the
MEASURED column: yes means the backend was called, no means the status was
inferred with no round trip. A probe is never a billed request.

Then:
  openreading parse examples/ --backend pymupdf   # run one that is ready

Exits: 0 always, a backend reported unreachable included. 3 an unknown
--check slug.

More: openreading help backends, openreading help env""",
    "serve": """\
Examples:
  openreading serve                  # http://127.0.0.1:8787, loopback only
  openreading serve --port 0         # the kernel picks, the line names it
  openreading serve --host 0.0.0.0   # reachable by others, so read below
  openreading serve --cors-origin http://localhost:3000  # one browser origin

Authentication is OFF by default. With no OPENREADING_API_KEYS set, anyone who
reaches this socket spends your vendor credits, which is why the default bind
is loopback and a --host outside it warns. Set OPENREADING_API_KEYS to a
comma-separated list of bearer tokens to turn it on. Every endpoint but
GET /healthz and POST /v1/webhooks/{backend_id} then answers 401 without an
Authorization: Bearer header. Both auth variables are read once at startup, so
rotating a token means a restart.

Then:
  curl -s localhost:8787/healthz   # poll this for readiness, not a log line
  curl -s localhost:8787/v1/backends       # the backends table, as JSON

Exits: 0 a clean stop. 3 the [server] extra is missing, the port is already
bound, or either auth variable is malformed. 143 stopped by SIGTERM.

More: openreading help serve   (path roots, token scopes, minting a token)""",
    "strategy": """\
Examples:
  openreading strategy list                   # presets, then your own
  openreading strategy validate               # check and explain every one
  openreading strategy show fast --longhand   # the tree Plain compiles to

A minimal openreading.yaml, on two backends that need no key:
  version: 1
  strategies:
    main:
      try: [pymupdf, tesseract]
      escalate_when: looks_bad

--config points elsewhere. --env-file goes before the sub-verb, not after.

Then:
  openreading parse examples/ --strategy fast   # run documents through one
  openreading explain out.json     # what the finished run actually decided

Exits: 0 ok. 3 no config, an unparseable one, a validate error, an unknown
name, or a compliance refusal. A warning never fails a validate.

More: openreading help strategy""",
    "strategy show": """\
Examples:
  openreading strategy show fast              # a built-in preset, as written
  openreading strategy show fast --longhand   # the full tree it compiles to
  openreading strategy show main --config ci/openreading.yaml

A strategy written in Plain prints as Plain, because that is the file you
edit. --longhand prints the canonical desugared tree the engine walks, which
is what you read when a run did something you did not expect. A preset needs
no config, so this answers on a fresh clone.

Then:
  openreading strategy plan doc.pdf --strategy fast   # prune it for one doc
  openreading parse examples/ --strategy fast         # run it

Exits: 0 ok. 3 an unknown name, or an openreading.yaml that will not parse.

More: openreading help strategy""",
    "strategy list": """\
Examples:
  openreading strategy list                     # presets, then your own
  openreading strategy list --config ci/openreading.yaml

Four presets ship inside the package and need no file: cost_saver, fast,
max_accuracy, offline_first. Your own strategies come from openreading.yaml,
and the listing prints the path it read them from, so a surprise entry has an
address. This verb runs config-free, so it works on a fresh clone.

Then:
  openreading strategy show fast     # the body of one of them
  openreading parse examples/ --strategy fast   # run documents through it

Exits: 0 ok. 3 an openreading.yaml that will not parse.

More: openreading help strategy""",
    "strategy validate": """\
Examples:
  openreading strategy validate                    # grammar, then English
  openreading strategy validate --config ci/openreading.yaml

You get, per strategy, a dialect badge, the body as you wrote it, a
plain-English summary, and a glossary of the judgment words it uses. Errors go
to stderr and warnings to stdout, and a warning never fails the run. Issues
carry a file and a node path rather than a line number. A step the file's own
policy: block makes unreachable is flagged before you ever run it.

Then:
  openreading strategy plan doc.pdf --strategy fast  # prune it for one doc
  openreading parse examples/ --strategy fast        # run it

Exits: 0 no errors, warnings included. 3 any error, no openreading.yaml
found, or one that will not parse.

More: openreading help strategy""",
    "strategy normalize": """\
Examples:
  openreading strategy normalize                  # canonical longhand YAML
  openreading strategy normalize > longhand.yaml  # keep it for review
  openreading strategy normalize --config ci/openreading.yaml

This is the `docker compose config` analog for strategies. It prints the whole
file's strategies as the canonical full-grammar YAML the engine compiles them
to, so two files that behave the same normalize the same. Use it to diff a
Plain file against a longhand one, or to see what a Plain key expanded into
before you commit a hand-written tree.

Then:
  openreading strategy validate      # check the file you started from
  openreading strategy plan doc.pdf --strategy fast   # prune for one doc

Exits: 0 ok. 3 no openreading.yaml found, or one that will not parse.

More: openreading help strategy""",
    "strategy plan": """\
Examples:
  openreading strategy plan doc.pdf --strategy fast --config openreading.yaml
  openreading strategy plan doc.pdf --strategy fast | jq .dropped

This verb needs an openreading.yaml even to plan a built-in preset, so pass
--config when yours is not in the working directory.

You get {strategy, config_hash, eligible, dropped[], tree} as JSON and no
execution at all, which makes this the plan step: see what this document under
this policy would do before it spends anything. Every dropped backend carries
the stage and the code that dropped it.

Then:
  openreading parse doc.pdf --strategy fast    # run the plan you printed

Exits: 0 ok. 3 an unreadable document or policy, an unknown strategy, no
openreading.yaml found, or a compliance refusal.

More: openreading help strategy, openreading help compliance""",
    "compare": """\
Examples:
  openreading compare a.json b.json --format table   # two saved responses
  openreading compare doc.pdf --backends pymupdf,tesseract --format diffs
  openreading compare mu.json te.json --format table    # two folder runs
  openreading compare --from run.json     # a strategy winner vs its losers

diffs leads with a content verdict, then tables, types and block counts, so
repackaged text never reads as missing text. When every subject is a
batch-result from `parse <folder>`, documents pair across runs into a corpus
report, so name each run after the backend that produced it. Fan-out runs
serially, so --all-ready cannot stampede a rate limit. --from reads completed
parallel outputs only. Sequential steps retain none. Races can cancel them.
Corpus mode refuses --baseline, --truth and --show-agreements.

Then:
  openreading compare a.json b.json > report.json   # save the delta
  openreading explain report.json    # the same delta, rendered as a table

Exits: 0 ok. 2 misuse (under two subjects, an unknown fan-out backend,
--format diff with other than two, mixed subject kinds). 3 a fanned-out
backend cannot run. 5 a bad envelope, or --from found none. 1 anything else.

More: openreading help compare, openreading help chaining""",
    "explain": """\
Examples:
  openreading parse doc.pdf --strategy fast > run.json
  openreading explain run.json      # gate by gate, what the run decided
  openreading compare a.json b.json > report.json
  openreading explain report.json   # the saved comparison, as a table

You get the strategy, the backend that answered, and each attempt with its
node, category, duration and cost. Gate rows show observed against threshold
and whether they fired. Decision points name the decider, and say when one
resolved to something other than what you configured. This reads one saved
response and calls nothing, so it is free and offline. A folder run is a
batch-result, so this reads each item's own response in turn.

Then:
  openreading replay doc.pdf --trace run.json   # take those decisions again
  openreading calibrate samples/ --strategy main   # tune a configured cascade

Exits: 0 ok. 3 an unreadable file, or no orchestration anywhere in it, which
is what a plain --backend run gives you.

More: openreading help explain, openreading help chaining""",
    "replay": """\
Examples:
  openreading parse doc.pdf --strategy main > run.json
  openreading replay doc.pdf --trace run.json > again.json
  openreading replay doc.pdf --trace run.json --strategy main

At every decision point this takes the choice the trace logged instead of
deciding again, so the run is deterministic and consults no LLM. A decision
the trace does not carry falls back to the engine default. The strategy name
comes from --strategy or from the trace, and a trace whose config_hash no
longer matches is refused rather than replayed under a different file.

Then:
  openreading explain again.json     # confirm it took the same path
  openreading compare run.json again.json --format diffs   # or where not

Exits: 0 ok. 2 no strategy name in either --strategy or the trace. 3 an
unreadable document, trace, config or policy, a config_hash mismatch, or a
compliance refusal.

More: openreading help replay, openreading help exit-codes""",
    "calibrate": """\
Examples:
  openreading calibrate src/openreading/evals/sample --strategy main \\
    --config openreading.yaml       # the sample dataset that ships here
  openreading calibrate samples/ --strategy main --target-escalation 0.15
  openreading calibrate samples/ --strategy main --max-cost-per-doc 0.05

This needs an openreading.yaml, because it tunes a strategy you wrote. A gate
is a threshold your strategy sets for a result it will accept. This
runs the strategy's first rung over your sample, scores each result, sweeps
every gated threshold, and prints candidate operating points against the
targets you named. It proposes an escalate_if: block ready to paste. It never
rewrites openreading.yaml, because the file you commit is the authority.
Unlabeled cases are fine and sit out of the agreement number.

Then:
  openreading strategy validate     # after you paste the recommendation
  openreading parse examples/ --strategy main   # run with the new gate

Exits: 0 ok. 3 no openreading.yaml, an unreadable dataset or policy, a
compliance refusal on a case, or a first-rung backend that cannot run.

More: openreading help calibrate, openreading help datasets""",
    "benchmark": """\
Examples:
  openreading benchmark list                # offline, no package needed
  openreading benchmark show parsebench     # sources, terms, scale
  openreading benchmark prepare parsebench --preset smoke
  openreading benchmark estimate parsebench --target backend:pymupdf
  openreading benchmark run parsebench --target backend:pymupdf

A profile connects one public dataset and its publisher's official scorer to
OpenReading. A target is one backend or strategy measured on it, written
backend:NAME or strategy:NAME. run touches two documents unless --limit says
otherwise, and it prints pages and a dollar range before it spends.
--env-file goes before the sub-verb, never after it.

Then:
  openreading benchmark report --format json | jq .   # for a script
  openreading leaderboard samples/ --backends a,b     # your own documents

Exits: 0 complete. 1 the publisher recorded a failure, scoring included.
2 an unknown profile, target, preset or document, a missing package, or a run
you stopped at the spending prompt. 3 a fault outside the publisher run.

More: openreading help benchmark, openreading help cost""",
    "benchmark list": """\
Examples:
  openreading benchmark list                  # every profile, offline
  openreading benchmark list | grep runnable  # the ones you can run today

Two lanes print. runnable means an official scorer package exists and this CLI
drives it. cataloged means the profile is described here and not wired up. The
terms column is the publisher's own dataset terms, and it decides which
acknowledgement flag a run needs: commercial needs none, research_only needs
--allow-research-only, unverified needs --allow-unverified-terms. Neither flag
makes a cataloged profile runnable, and neither says a use is lawful.

Then:
  openreading benchmark show parsebench   # the detail behind one row

Exits: 0 always. This verb touches no network and needs no extra package.

More: openreading help benchmark""",
    "benchmark show": """\
Examples:
  openreading benchmark show parsebench     # a runnable profile
  openreading benchmark show extractbench   # the other runnable one
  openreading benchmark show docile         # a cataloged one

You get the publisher's source links, the data and code licenses and their
terms URLs, the scorer revision pinned here, the metric dimensions, the
published document and page counts, and the install extra. Read the two
license lines before you download anything, because the terms are the
publisher's and this command only reports them.

Then:
  openreading benchmark prepare parsebench --preset smoke   # download it
  openreading benchmark estimate parsebench --target backend:pymupdf

Exits: 0 ok. 2 an unknown benchmark id.

More: openreading help benchmark""",
    "benchmark prepare": """\
Examples:
  openreading benchmark prepare parsebench --preset smoke
  openreading benchmark prepare parsebench --preset full
  openreading benchmark prepare parsebench --cache-dir ./bench-cache
  openreading benchmark prepare parsebench --force    # replace the cache

This runs the publisher's own downloader and writes under the benchmark cache
directory unless --cache-dir says otherwise. Nothing is scored and no backend
is called, so this costs network and disk only. An ordinary rerun reuses what
is already there. --force asks the publisher downloader to replace its cache.
A runnable profile needs the install extra that `benchmark show` names.

Then:
  openreading benchmark run parsebench --target backend:pymupdf

Exits: 0 prepared. 2 an unknown or cataloged profile, a bad preset, a missing
package, a terms acknowledgement you have not given, or a failed download.

More: openreading help benchmark""",
    "benchmark estimate": """\
Examples:
  openreading benchmark estimate parsebench --target backend:pymupdf
  openreading benchmark estimate parsebench --preset full \\
    --target backend:pymupdf --target backend:tesseract

You get the published scale and the call count each target implies, with no
inference and no download. Repeat --target to price several pipelines in one
pass. This refuses a cataloged profile, because printing its published scale
would read as a run you could start. The real page count and dollar range come
from `benchmark run`, which prints them before it spends.

Then:
  openreading benchmark prepare parsebench --preset smoke
  openreading benchmark run parsebench --target backend:pymupdf

Exits: 0 ok. 2 an unknown or cataloged profile, a bad target or preset, or a
terms acknowledgement you have not given.

More: openreading help benchmark, openreading help cost""",
    "benchmark report": """\
Examples:
  openreading benchmark report                        # ./benchmark-results
  openreading benchmark report --output-dir ./runs/nightly
  openreading benchmark report --format json | jq .   # for a script

This reprints the comparison a finished run already produced, ranked by the
publisher's own numbers, and it re-runs nothing and calls nothing. It computes
no score of its own: it reads the publisher's evaluation report back, so this
terminal and the publisher's dashboard cannot disagree. openreading-run.json
beside the artifacts says which pipeline was which target. The run directory
is named by --output-dir, not by a positional.

Then:
  openreading benchmark run parsebench --target backend:pymupdf --force

Exits: 0 ok. 2 the artifact directory is missing, unreadable, or holds no
finished run.

More: openreading help benchmark""",
    "benchmark run": """\
Examples:
  openreading benchmark run parsebench --target backend:pymupdf   # 2 docs
  openreading benchmark run parsebench --target backend:pymupdf --limit 20
  openreading benchmark run parsebench --target strategy:main --limit 0 --yes
  openreading benchmark run parsebench --target backend:pymupdf --force

Two documents run unless you say otherwise, because this spends your money on
someone else's API. --limit N runs N, --limit 0 runs everything, --doc NAME
runs the ones you name, and a repeated --target ranks two pipelines in one
run. Documents are picked round-robin across categories in a stable order, so
a rerun resumes instead of re-billing. Pages and a dollar range print first.
Anything unpriced or over a dollar asks, so CI needs --yes.

Then:
  openreading benchmark report          # the same table, later

Exits: 0 complete. 1 the publisher recorded a failed document, or scoring
failed. 2 a bad profile, target, preset, --jobs or --doc, a missing package,
or a run stopped at the prompt. 3 a fault outside the publisher's boundary.

More: openreading help benchmark, openreading help cost""",
    "rules": """\
Examples:
  openreading rules src/openreading/evals/sample   # print what it would add
  openreading rules mydata --write     # edit the case.json files in place
  openreading rules mydata --write --force   # replace rules already there

Each string under text_contains becomes a present rule, passed through
untouched. Each table cell becomes a table rule carrying its right neighbour
and its column heading, which is what makes it structural rather than a second
presence check. text, markdown and typed_fields generate nothing, because a
whole-document string is a similarity measure. Printing is the default,
because --write rewrites files a person hand-labeled.

Then:
  openreading leaderboard mydata --backends pymupdf,tesseract

Exits: 0 ok. 3 a dataset directory with no <case>/case.json, or a case.json
that will not parse.

More: openreading help rules, openreading help datasets""",
    "help": """\
Examples:
  openreading help                # every chapter, grouped by what you want
  openreading help batch          # folders, globs, and many files at once
  openreading help chaining       # which verb's output feeds which verb
  openreading help exit-codes | grep 143     # it is text, so grep it

A chapter is a section of this package's own reference, printed as written.
Aliases reach the same chapter, so `help folder` and `help glob` both open
the batch chapter. There is no pager, on purpose: pipe it to one when you
want one.

Then:
  openreading COMMAND --help      examples and exit codes for one command
  python -m pydoc openreading.cli   all of it, in source order

Exits: 0 the index or a chapter. 2 an unknown topic, which prints the index
on stderr with a suggestion when available. 3 python -OO discarded the manual.

More: openreading help quickstart""",
    "leaderboard": """\
Examples:
  openreading leaderboard src/openreading/evals/sample \\
    --backends pymupdf,tesseract           # two local backends, no key
  openreading leaderboard mydata --all-ready       # every ready backend
  openreading leaderboard mydata --backends a,b --format json > rank.json
  openreading leaderboard mydata --backends a,b --config policy.yaml

Pass one of --backends or --all-ready, and rank at least two. This takes a
LABELED dataset of <case>/case.json, not a folder of documents, and it runs
the backends itself. The table prints the dataset's own path and case names
above the ranking, so a screenshot never reads as a universal verdict. Every
backend makes a real call per case, so --all-ready over a large dataset is
cases times backends in billable calls.

Then:
  openreading compare a.json b.json --format diffs  # why one of them lost

Exits: 0 ok. 2 fewer than two backends, or an unknown id in --backends. 3 an
unreadable policy, an empty or unresolvable dataset, or a cannot-run fault.

More: openreading help leaderboard, openreading help datasets""",
}

# argparse renders the description before the subcommand list and the epilog after it. The
# quickstart goes in the description on purpose: a first-time reader must reach something they
# can paste inside the first screen, and a thirteen-verb listing pushes an epilog past the fold.
TOP_DESCRIPTION = """\
One JSON shape from every document parser, so switching or comparing parsers
never changes your code.

QUICKSTART. No key or account. Install dependencies first (see the README).

  openreading backends                        # what already runs here
  F=examples/john_smith_1000_2026_01.pdf
  openreading parse $F --backend pymupdf > out.json     # one document
  openreading parse examples/ --backend pymupdf > all.json    # a folder
  openreading compare $F --backends pymupdf,tesseract --format table
  # comparison needs the tesseract executable on PATH"""

TOP_EPILOG = """\
I WANT TO ...                          RUN
  see what works here, with no keys    openreading backends
  read one document                    openreading parse FILE --backend SLUG
  read a folder, a glob, or a list     openreading parse DIR/ --backend SLUG
  let OpenReading pick the backend     openreading parse FILE --no-strategy
  follow a plan I wrote down           openreading parse FILE --strategy NAME
  know which backends a policy allows  openreading route FILE
  see where two backends disagree      openreading compare A.json B.json
  know why a run chose what it chose   openreading explain RUN.json
  rank backends on my labeled dataset  openreading leaderboard DIR --all-ready
  explore published benchmarks         openreading benchmark list
  pick a run back up after a stop      openreading resume RUN_ID
  call this from another language      openreading serve

FOLDERS AND GLOBS, NOT ONLY FILES. Every parse source may be a file, a URL, a
folder or a quoted glob, and you may pass several at once. One file prints one
response. Anything else prints one batch-result over every document, with
--jobs to run them at once and --save-dir to keep each one's own JSON.
Read `openreading help batch` before you point this at a corpus.

THINGS CHAIN.
  parse > out.json  ->  compare > report.json  ->  explain report.json
  route --run  ->  jq .result  ->  compare
  parse --strategy  ->  explain, replay;  calibrate  ->  strategy validate

LEARN MORE
  openreading COMMAND --help   # examples and exit codes for one command
  openreading help             # the manual's topic index
  openreading help batch       # one chapter of it"""


class _HelpFormatter(argparse.HelpFormatter):
    """Wrap a description, print an epilog exactly as written.

    argparse ships wrap-both (`HelpFormatter`) or raw-both (`RawDescriptionHelpFormatter`), and
    this CLI needs one of each: a description is a sentence that should reflow to the reader's
    terminal, and an epilog is a block of commands they are meant to paste, which reflowing
    destroys. Both arrive through `_fill_text`, so the text itself decides. A block that already
    carries its own line breaks was laid out on purpose; a single run of words was not.
    """

    def _fill_text(self, text: str, width: int, indent: str) -> str:
        if "\n" in text.strip():
            return "".join(indent + line for line in text.splitlines(keepends=True))
        return super()._fill_text(text, width, indent)


def _attach_epilogs(parser: argparse.ArgumentParser, path: str = "") -> None:
    """Hang the epilogs on the tree after it is built, so `build_parser` stays a shape and not a
    wall of prose. `RawDescriptionHelpFormatter` goes on with them: argparse otherwise reflows an
    example into a paragraph, which turns a command you can paste into a sentence you cannot."""
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        for name, sub_parser in action.choices.items():
            key = f"{path} {name}".strip()
            if key in EPILOGS:
                sub_parser.epilog = EPILOGS[key]
                sub_parser.formatter_class = _HelpFormatter
            _attach_epilogs(sub_parser, key)


def build_parser() -> argparse.ArgumentParser:
    """The argparse tree for every subcommand. `--version` is declared before the required
    subcommand so it answers without one."""
    p = argparse.ArgumentParser(
        prog="openreading",
        description=TOP_DESCRIPTION,
        epilog=TOP_EPILOG,
        formatter_class=_HelpFormatter,
    )
    # Declared before the required subcommand so `openreading --version` answers instead of failing
    # the "command is required" check: step zero of every incident is "what is deployed?", and the
    # version was otherwise reachable only from pyproject.toml, `openreading.__version__`, or
    # `GET /healthz` on a server that may be the thing that is down.
    p.add_argument(
        "--version",
        action="version",
        version=f"openreading {openreading_version}",
        help="print the installed openreading version and exit",
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--env-file", default=None, help="path to a .env file (default: ./.env if present)"
    )
    # A metavar keeps the thirteen verb names out of the usage line, where they pushed the
    # English off the screen. The choices still gate the value and still print in full on a
    # bad one.
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    help_p = sub.add_parser(
        "help",
        parents=[common],
        help="print one chapter of the manual, or list the chapters",
        description="Print the long-form manual on stdout: the topic index with no argument, "
        "one chapter with a topic name.",
    )
    help_p.add_argument(
        "topic",
        nargs="?",
        metavar="TOPIC",
        help="a topic name (`openreading help` with no topic lists them)",
    )
    help_p.set_defaults(func=cmd_help)

    parse = sub.add_parser(
        "parse",
        parents=[common],
        help="read one document, a folder, a glob, or a list of them",
        description="Read one document, or a folder or glob of them, with one backend or one "
        "strategy, and print JSON on stdout.",
    )
    parse.add_argument(
        "files",
        nargs="+",
        metavar="FILE",
        # Lead with the consequence, not with the accepted forms: the reader wants to know which
        # envelope they get back, and the folder case is the one nobody discovers on their own.
        help="one file or URL prints one response; a directory, a glob, or two or more "
        "arguments print one batch-result holding a response per document. A directory "
        "expands recursively, and a directory holding one file is still a batch-result. "
        "See `openreading help batch`.",
    )
    # The three selectors are checked at runtime rather than by a mutually exclusive group:
    # argparse's message names only the pair it caught, and the runtime one names all three.
    choose = parse.add_argument_group("choosing what runs (exactly one of the first three)")
    choose.add_argument(
        "--backend",
        default=None,
        choices=sorted(BUILTIN_ADAPTERS),
        # The choices still gate the value and still print in full on an invalid one; the metavar
        # only keeps fifteen slugs out of the usage line, where they buried the English.
        metavar="SLUG",
        help="run one named backend (`openreading backends` lists the ids)",
    )
    choose.add_argument(
        "--strategy",
        default=None,
        metavar="NAME",
        help="run a strategy from openreading.yaml, or a built-in preset "
        "(`openreading strategy list` prints both)",
    )
    choose.add_argument(
        "--no-strategy",
        action="store_true",
        help="force the router's auto choice, ignoring defaults.strategy",
    )
    choose.add_argument(
        "--config", default=None, metavar="PATH", help="path to an openreading.yaml"
    )
    ask = parse.add_argument_group("what to ask the backend for")
    ask.add_argument(
        "--pages",
        type=_page_number,
        nargs="*",
        default=None,
        metavar="N",
        help="1-based page numbers. This takes a variable number of values, so name FILE "
        "before it or close the list with --",
    )
    ask.add_argument(
        "--operation",
        default=None,
        metavar="OP",
        help="backend sub-operation (e.g. AnalyzeLending)",
    )
    ask.add_argument(
        "--extract",
        nargs="?",
        const="",
        default=None,
        metavar="INSTRUCTIONS",
        help="request schema-driven field extraction. A named backend that cannot do it "
        "refuses the run and exits 3 (unsupported_feature, nothing on stdout) rather than "
        "silently dropping the ask. Takes an optional value, so name FILE before it",
    )
    # The group title names the input FORM that switches modes, because a reader with one file
    # needs to know at a glance that this whole block is not about their run.
    many = parse.add_argument_group(
        "many documents (a directory, a glob, or two or more FILE arguments)"
    )
    many.add_argument(
        "--jobs",
        type=int,
        default=1,
        metavar="N",
        help="run this many documents at once. Default 1, which is serial, deterministic and "
        "safe against a vendor rate limit. Concurrency changes how long a folder takes and "
        "never what it costs",
    )
    many.add_argument(
        "--max-jobs",
        type=int,
        default=MAX_BATCH_JOBS,
        dest="max_jobs",
        metavar="N",
        help=f"ceiling on --jobs (default {MAX_BATCH_JOBS}); a non-positive --jobs clamps to 1, "
        "and one above this ceiling exits 2",
    )
    many.add_argument(
        "--max-items",
        type=int,
        default=DEFAULT_MAX_ITEMS,
        dest="max_items",
        metavar="N",
        help=f"hard cap on expanded files (default {DEFAULT_MAX_ITEMS}); exceeding it exits 2 "
        "before anything runs",
    )
    many.add_argument(
        "--save-dir",
        default=None,
        dest="save_dir",
        metavar="DIR",
        help="also write each succeeded item's own response to DIR/<relpath>.json, which is "
        "what `compare` reads",
    )
    budget = parse.add_argument_group("time and payload")
    budget.add_argument(
        "--deadline",
        type=float,
        default=None,
        dest="deadline_s",
        metavar="SECONDS",
        help="absolute time budget override, in seconds. It applies to a single document "
        "run with --backend NAME, and to a batch a backend runs natively. It has no "
        "effect on `auto` or `--strategy` dispatch, which manage their own time budget. "
        "A value of 0 or less means fail fast, so nothing waits. `openreading help parse` "
        "has the per-dispatch defaults",
    )
    budget.add_argument(
        "--keep-candidates",
        action="store_true",
        help="retain completed parallel alternatives under orchestration.candidates[] for "
        "`compare --from`. Sequential steps retain none. A race can cancel its alternatives. "
        "Off by default, and no effect on a direct backend run",
    )
    parse.set_defaults(func=cmd_parse)

    resume = sub.add_parser(
        "resume",
        parents=[common],
        help="resume an interrupted/failed run from its ledger journal "
        "(`openreading help resume` explains the journal)",
        description="Continue an interrupted run from its ledger journal, skipping the steps "
        "that already finished.",
    )
    resume.add_argument(
        "run_id",
        metavar="RUN_ID",
        help="the run id to resume. `parse` prints it on interrupt, or read it from a "
        "run's own header under $OPENREADING_LEDGER. No other flags: every option comes "
        "from the ledger.",
    )
    resume.set_defaults(func=cmd_resume)

    route = sub.add_parser(
        "route",
        parents=[common],
        help="show the compliance-first routing plan for a document",
        description="Show which backends your compliance policy allows for a document, and why "
        "the rest were dropped, before anything runs.",
    )
    route.add_argument("file", help="path or http(s):// URL")
    route.add_argument("--config", default=None, metavar="PATH", help="path to an openreading.yaml")
    route.add_argument(
        "--run",
        action="store_true",
        help="also execute the plan: the chosen backend first, then each fallback in turn",
    )
    route.set_defaults(func=cmd_route)

    backends = sub.add_parser(
        "backends",
        parents=[common],
        help="list backends and whether they are configured to run",
        description="List every backend and whether this machine is configured to run it, "
        "naming the variables or extras still missing.",
    )
    backends.add_argument(
        "--check",
        default=None,
        metavar="SLUG[,SLUG...]|all",
        help="also probe these backends for real liveness. This makes network calls and "
        "is never implicit. 'all' probes every backend that declares a probe.",
    )
    backends.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="per-probe timeout for --check. The default is the adapter's declared "
        "liveness.timeout_s, else 5s. Any value is clamped to [0.1, 30].",
    )
    backends.set_defaults(func=cmd_backends)

    serve = sub.add_parser(
        "serve",
        parents=[common],
        help="run the HTTP API (needs [server] extra)",
        description=(
            "Run the HTTP API on your own machine, so a client in another language gets\n"
            "the same shapes the CLI prints."
        ),
    )
    serve.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8787, help="port (default 8787)")
    serve.add_argument(
        "--cors-origin", action="append", default=None, help="allowed CORS origin (repeatable)"
    )
    serve.set_defaults(func=cmd_serve)

    # `openreading strategy <show|list|normalize|validate|plan>` inspects the openreading.yaml
    # orchestration config. The verbs that work on a run instead sit at the top level: `explain`
    # renders a trace, `replay` re-runs it from the logged decisions, `calibrate` proposes
    # gate thresholds.
    strategy_desc = (
        "Inspect and understand your openreading.yaml strategies. A strategy is a recipe\n"
        "for which backends run, in what order or together, and when to move on. Written\n"
        'in "Plain": six keys.\n'
        "\n"
        "  try: [a, b, c]      run in order; move on if a step fails or the result looks bad\n"
        "  race: [a, b]        run at once; first success wins, the rest are cancelled\n"
        "  compare: [a, b]     run at once; keep the objectively better result\n"
        "  then: x             where compare sends the document when it cannot trust the winner\n"
        "  escalate_when: ...  when to move on, using any of the four judgment words below\n"
        '  max_time: "2m"      give up after this long\n'
        "\n"
        "escalate_when takes any of:  looks_bad, low_confidence, missing: [field, ...], disagree\n"
        "  (disagree is compare-only).  auto = the best remaining backend, usable as a try rung\n"
        "  or a then: target."
    )
    strategy = sub.add_parser(
        "strategy",
        parents=[common],
        help="inspect openreading.yaml strategies",
        description=strategy_desc,
    )
    strat_sub = strategy.add_subparsers(dest="strategy_command", required=True)

    st_show = strat_sub.add_parser(
        "show",
        help="dump a strategy or preset (body as written)",
        description="Print one strategy's body exactly as it is written, or the canonical "
        "longhand tree the engine compiles it to.",
    )
    st_show.add_argument("name", help="strategy or built-in preset name")
    st_show.add_argument("--config", default=None, help="path to an openreading.yaml")
    st_show.add_argument(
        "--longhand", action="store_true", help="print the canonical (normalized) tree instead"
    )
    st_show.set_defaults(func=cmd_strategy_show)

    st_list = strat_sub.add_parser(
        "list",
        help="list built-in presets and configured strategies",
        description="List the presets that ship inside the package, then the strategies your "
        "openreading.yaml defines, with the path they came from.",
    )
    st_list.add_argument("--config", default=None, help="path to an openreading.yaml")
    st_list.set_defaults(func=cmd_strategy_list)

    st_val = strat_sub.add_parser(
        "validate",
        help="check + explain every strategy in plain English",
        description="Check every strategy in the config against the grammar and against the "
        "world it runs in, then explain each one in plain English.",
    )
    st_val.add_argument("--config", default=None, help="path to an openreading.yaml")
    st_val.set_defaults(func=cmd_strategy_validate)

    st_norm = strat_sub.add_parser(
        "normalize",
        help="print the config's strategies as longhand",
        description="Print every strategy in the config as the canonical full-grammar YAML the "
        "engine compiles it to, so two files that behave the same look the same.",
    )
    st_norm.add_argument("--config", default=None, help="path to an openreading.yaml")
    st_norm.set_defaults(func=cmd_strategy_normalize)

    st_plan = strat_sub.add_parser(
        "plan",
        help="pruned tree for a document (no execution)",
        description="Print the pruned tree this document would walk under this policy, and "
        "execute none of it.",
    )
    st_plan.add_argument("file", help="path or http(s):// URL")
    st_plan.add_argument("--strategy", required=True, help="strategy or preset name")
    st_plan.add_argument("--config", default=None, help="path to an openreading.yaml")
    st_plan.set_defaults(func=cmd_strategy_plan)

    compare = sub.add_parser(
        "compare",
        parents=[common],
        help="compare backends' outputs and show the delta (fields/text/blocks)",
        description="Show where two or more backends disagree on the same document, field by "
        "field and line by line.",
    )
    compare.add_argument(
        "inputs",
        nargs="*",
        default=[],
        metavar="SUBJECT",
        help="two or more response or batch-result JSON files, or one document with --backends. "
        "Omit with --from",
    )
    pick = compare.add_argument_group("picking the subjects (files, or one of these)")
    pick.add_argument(
        "--backends",
        default=None,
        metavar="A,B",
        help="comma-separated backend ids to fan out over one document. Fan-out is serial, so "
        "it cannot stampede a rate limit, and each backend is a full billed run",
    )
    pick.add_argument("--all-ready", action="store_true", help="fan out over every ready backend")
    pick.add_argument(
        "--from",
        dest="from_response",
        default=None,
        metavar="RUN.json",
        help="a saved strategy response (run parse with --keep-candidates): compare its winner "
        "against completed parallel alternatives in orchestration.candidates[]. Sequential "
        "steps retain none. A race can cancel its alternatives. For a batch, pass one item's "
        "response written by parse --save-dir",
    )
    pick.add_argument(
        "--save-dir",
        default=None,
        metavar="DIR",
        help="write each fan-out response into this directory, one file per backend",
    )
    pick.add_argument(
        "--deadline",
        type=float,
        default=None,
        dest="deadline_s",
        metavar="SECONDS",
        help="absolute time budget override, in seconds, applied to every fanned-out backend. "
        "Raise it for a long-running hosted async job. Default, when omitted, is "
        "the generic 120s single-document deadline. A non-positive value (0 or negative) means "
        "fail fast: do not wait at all",
    )
    show = compare.add_argument_group("how it prints")
    show.add_argument(
        "--format",
        choices=["json", "table", "diff", "diffs", "md"],
        default="json",
        help="json (default) | table | diff (a 2-way git-style text diff, exactly 2 subjects) | "
        "diffs (content, tables, types and block counts, any number of subjects) | md",
    )
    show.add_argument(
        "--show-agreements",
        action="store_true",
        help="also list agreeing fields in table/md formats (hidden by default). "
        "Single-response subjects only; refused for corpus comparisons",
    )
    show.add_argument(
        "--baseline",
        default=None,
        metavar="SUBJECT",
        help="sign deltas against a subjects[].label (normally backend.id), or add a response "
        "JSON as a new baseline subject. Single-response subjects only; refused for corpus",
    )
    score = compare.add_argument_group("scoring against a golden")
    score.add_argument(
        "--truth",
        default=None,
        metavar="GOLDEN.json",
        help="score each subject against a golden.json containing the evals expected object. "
        "Single-response subjects only; refused for corpus. See `openreading help datasets`",
    )
    compare.set_defaults(func=cmd_compare)

    explain = sub.add_parser(
        "explain",
        parents=[common],
        help="render a response's orchestration block or a comparison",
        description="Print what a saved strategy run did, gate by gate, or render a saved "
        "comparison report.",
    )
    explain.add_argument(
        "response",
        help="path to a saved response, batch-result or comparison-report JSON. "
        "Corpus comparison reports are not supported",
    )
    explain.set_defaults(func=cmd_explain)

    replay = sub.add_parser(
        "replay",
        parents=[common],
        help="re-run a strategy taking a trace's logged decisions",
        description="Re-run a strategy taking each decision from a saved trace instead of "
        "deciding again.",
    )
    replay.add_argument("file", help="path or http(s):// URL of the document")
    replay.add_argument(
        "--trace", required=True, help="a saved response/orchestration JSON to replay"
    )
    replay.add_argument("--strategy", default=None, help="strategy name (default: from the trace)")
    replay.add_argument("--config", default=None, help="path to an openreading.yaml")
    replay.set_defaults(func=cmd_replay)

    calibrate = sub.add_parser(
        "calibrate",
        parents=[common],
        help="derive gate thresholds from a sample of documents, labels optional",
        description="Derive gate thresholds from a sample of your own documents and print them "
        "as a recommendation.",
    )
    calibrate.add_argument(
        "dataset",
        help="a dataset dir of */case.json documents (unlabeled cases are fine, "
        "excluded from scorer_agreement)",
    )
    calibrate.add_argument("--strategy", required=True, help="the strategy to tune")
    calibrate.add_argument("--config", default=None, help="path to an openreading.yaml")
    calibrate.add_argument(
        "--target-escalation",
        type=float,
        default=None,
        help="fraction of docs that should escalate past rung 1 (e.g. 0.15)",
    )
    calibrate.add_argument(
        "--max-cost-per-doc", type=float, default=None, help="budget ceiling per doc (e.g. 0.05)"
    )
    calibrate.set_defaults(func=cmd_calibrate)

    benchmark = sub.add_parser(
        "benchmark",
        parents=[common],
        help="discover and run publisher-owned public document benchmarks",
        description="Discover public document corpora or run an OpenReading backend or strategy "
        "through a supported publisher's official scorer.",
    )
    benchmark_sub = benchmark.add_subparsers(dest="benchmark_command", required=True)

    benchmark_list = benchmark_sub.add_parser(
        "list",
        help="list supported and cataloged public benchmarks without network access",
        description="List every benchmark profile this package knows, and say which ones it can "
        "actually run. Touches no network.",
    )
    benchmark_list.set_defaults(func=cmd_benchmark_list)

    benchmark_show = benchmark_sub.add_parser(
        "show",
        help="show source links, terms, scale, dimensions, and installation needs",
        description="Show one benchmark's publisher links, licenses and terms, pinned scorer "
        "revision, metric dimensions, published scale, and the install extra it needs.",
    )
    benchmark_show.add_argument("benchmark", help="benchmark identifier from `benchmark list`")
    benchmark_show.set_defaults(func=cmd_benchmark_show)

    def add_benchmark_selection(parser, *, targets: bool = False) -> None:
        parser.add_argument("benchmark", help="benchmark identifier from `benchmark list`")
        parser.add_argument(
            "--preset",
            choices=["smoke", "full"],
            default="smoke",
            help="publisher smoke subset (default) or the full dataset",
        )
        if targets:
            parser.add_argument(
                "--target",
                action="append",
                default=[],
                help="backend:NAME or strategy:NAME. Repeat to evaluate several targets.",
            )
        parser.add_argument(
            "--allow-research-only",
            action="store_true",
            help="acknowledge the publisher's stated research-only dataset terms",
        )
        parser.add_argument(
            "--allow-unverified-terms",
            action="store_true",
            help="acknowledge that dataset or source terms remain unverified",
        )

    benchmark_prepare = benchmark_sub.add_parser(
        "prepare",
        help="download a runnable dataset through its publisher package",
        description="Download one benchmark's dataset through the publisher's own downloader. "
        "Nothing is scored and no backend is called.",
    )
    add_benchmark_selection(benchmark_prepare)
    benchmark_prepare.add_argument(
        "--cache-dir",
        type=Path,
        default=Path.home() / ".cache" / "openreading" / "benchmarks",
        help="dataset cache root (default ~/.cache/openreading/benchmarks)",
    )
    benchmark_prepare.add_argument(
        "--force", action="store_true", help="ask the publisher downloader to replace its cache"
    )
    benchmark_prepare.set_defaults(func=cmd_benchmark_prepare)

    benchmark_estimate = benchmark_sub.add_parser(
        "estimate",
        help="show published scale and target-call counts without inference",
        description="Show the published scale and the number of calls each target implies, with "
        "no download and no inference.",
    )
    add_benchmark_selection(benchmark_estimate, targets=True)
    benchmark_estimate.set_defaults(func=cmd_benchmark_estimate)

    benchmark_report = benchmark_sub.add_parser(
        "report",
        help="print the comparison from a finished run, without re-running it",
        description="Reprint the comparison a finished run already produced, reading the "
        "publisher's own evaluation report back. Re-runs nothing and computes no score.",
    )
    benchmark_report.add_argument(
        "--output-dir",
        type=Path,
        default=Path("benchmark-results"),
        help="publisher artifact root to read (default ./benchmark-results)",
    )
    benchmark_report.add_argument(
        "--format", choices=["text", "json"], default="text", help="table (default) or JSON"
    )
    benchmark_report.set_defaults(func=cmd_benchmark_report)

    benchmark_run = benchmark_sub.add_parser(
        "run",
        help="prepare, run OpenReading targets, and invoke the official scorer",
        description="Prepare the dataset, run each target over it, and score the results with "
        "the publisher's official scorer. This spends money, so it runs two documents by "
        "default and prints pages and a dollar range before it starts.",
    )
    add_benchmark_selection(benchmark_run, targets=True)
    benchmark_run.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_BENCHMARK_LIMIT,
        help=(
            f"documents to run, spread across the corpus's categories "
            f"(default {DEFAULT_BENCHMARK_LIMIT}; 0 runs every prepared document)"
        ),
    )
    benchmark_run.add_argument(
        "--doc",
        action="append",
        default=[],
        help="run one named document instead of --limit, by id or file stem. Repeat for several.",
    )
    benchmark_run.add_argument(
        "--yes",
        action="store_true",
        help="skip the spending confirmation for unpriced or over-$1 runs. "
        "Those runs require this flag when no terminal is attached.",
    )
    benchmark_run.add_argument("--config", default=None, help="strategy openreading.yaml path")
    benchmark_run.add_argument(
        "--cache-dir",
        type=Path,
        default=Path.home() / ".cache" / "openreading" / "benchmarks",
        help="dataset cache root (default ~/.cache/openreading/benchmarks)",
    )
    benchmark_run.add_argument(
        "--output-dir",
        type=Path,
        default=Path("benchmark-results"),
        help="publisher artifact root (default ./benchmark-results)",
    )
    benchmark_run.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="maximum documents processed concurrently (default 1)",
    )
    benchmark_run.add_argument(
        "--force", action="store_true", help="rerun cases with existing publisher artifacts"
    )
    benchmark_run.set_defaults(func=cmd_benchmark_run)

    rules = sub.add_parser(
        "rules",
        parents=[common],
        help="generate publisher rules from a dataset's existing expectations",
        description="Turn the `text_contains` and `tables` a case already carries into "
        "ParseBench rule objects, so nobody hand-authors another company's JSON. Prints by "
        "default; --write edits the case.json files in place.",
    )
    rules.add_argument("dataset", help="dataset directory of <case>/case.json files")
    rules.add_argument(
        "--write", action="store_true", help="edit the case.json files instead of printing"
    )
    rules.add_argument(
        "--force", action="store_true", help="replace an existing `rules` key rather than skipping"
    )
    rules.set_defaults(func=cmd_rules)

    leaderboard = sub.add_parser(
        "leaderboard",
        parents=[common],
        help="rank registered backends on one dataset, measured rather than vendor-claimed",
        description="Rank backends on one dataset by measured score, and print the cost that "
        "produced each score.",
    )
    leaderboard.add_argument(
        "dataset",
        metavar="DATASET",
        # Naming the shape here is what stops a reader pointing this at the folder of documents
        # they just parsed. It takes labels; `compare` is the verb that needs none.
        help="a LABELED dataset dir of <case>/case.json documents (evals.dataset shape), not a "
        "folder of documents",
    )
    # `required=True` on a mutually exclusive group would state the choice rule and lose the
    # arity rule, and "rank at least two" is the half people get wrong.
    which = leaderboard.add_argument_group("which backends to rank (one of these is required)")
    which.add_argument(
        "--backends",
        default=None,
        metavar="A,B",
        help="comma-separated backend ids to rank, at least two. Every backend makes a real call "
        "per case, so this is cases times backends in billable calls",
    )
    which.add_argument(
        "--all-ready", action="store_true", help="rank every backend the environment is ready for"
    )
    how = leaderboard.add_argument_group("how it runs and prints")
    how.add_argument("--config", default=None, metavar="PATH", help="path to an openreading.yaml")
    how.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="table (default, human) | json (schema-valid BenchmarkReport)",
    )
    leaderboard.set_defaults(func=cmd_leaderboard)

    _attach_epilogs(p)
    return p


def _is_asyncio_sigint_handler(
    handler: object,
) -> TypeGuard[Callable[[int, object], None]]:
    """True only for the SIGINT handler `asyncio.Runner.run` installs for the duration of a run.

    Delegation is a loaded gun pointed at the stop signal: whatever we hand SIGTERM to becomes the
    only thing that stops this process. A handler that merely records the signal — an embedder, a
    test harness, a signal-aware library — returns without unwinding anything, so delegating to it
    would turn a supervisor's stop into a silent no-op and the run would carry on. Recognising one
    specific handler, rather than trusting any callable, is the difference between a stop that is
    safer and a stop that never happens.

    `Runner.run` installs `functools.partial(self._on_sigint, main_task=task)`, so the bound method
    and the `Runner` it is bound to are both reachable through the partial. Every part of that is
    CPython's private shape and may change; when it does, this returns False and the caller raises
    `KeyboardInterrupt` directly — the pre-delegation behaviour, which always stops the process and
    is only exposed to the narrow teardown window delegation exists to dodge. Fail-safe, not
    fail-open, is the whole reason the check is written this way round.
    """
    bound = getattr(handler, "func", None)  # the partial's wrapped `Runner._on_sigint`
    return getattr(bound, "__name__", None) == "_on_sigint" and isinstance(
        getattr(bound, "__self__", None), asyncio.Runner
    )


def _asyncio_sigint_verdict(handler: object) -> Literal["cancel", "stopping", "declines"]:
    """What `Runner._on_sigint` will DO if this stop is handed to it — asked before it is called,
    because after it raises the answer is no longer recoverable.

    `_on_sigint` cancels the main task only on the first interrupt of a run whose task is still
    running; every other call is `raise KeyboardInterrupt()` straight out of the handler. Two
    opposite situations end at that one raise:

    - `"stopping"` — an interrupt is already counted for this run, so a real Ctrl-C is unwinding it
      right now and the raise is asyncio's escalation, aimed into the teardown already under way.
      Swallowing it is rule one seen from the other side, and the exit code belongs to the SIGINT
      that started the stop (130, not 143).
    - `"declines"` — nothing is counted, but the walk has already finished and the loop is a few
      frames from returning. Nothing is unwinding, so a swallowed raise here loses the stop
      outright: the command runs to a clean exit 0 with a supervisor's SIGTERM already delivered,
      and a batch keeps parsing its remaining items. That is the exact failure delegation exists to
      avoid ("a delegation that does not stop the process is a worse failure"), reached down the
      delegation itself. It was measured as a green run on three CPythons and one `assert 0 == 143`
      on the fourth, because the window is one loop teardown wide per item and a batch has one per
      document. So `"declines"` takes the direct raise instead — by then `_run_forever_cleanup` has
      unmarked the loop as running, which is what makes the raise safe here and not in the startup
      window next door.

    Read through the same private `functools.partial` shape `_is_asyncio_sigint_handler` recognises
    (which is why this is only ever called after that returned True). An unreadable shape — a
    future CPython — answers `"declines"`, so an unknown asyncio falls back to the raise that
    always stops rather than to the swallow that might not."""
    runner = getattr(getattr(handler, "func", None), "__self__", None)
    if getattr(runner, "_interrupt_count", None):
        return "stopping"
    task = (getattr(handler, "keywords", None) or {}).get("main_task")
    return "cancel" if isinstance(task, asyncio.Task) and not task.done() else "declines"


@contextlib.contextmanager
def _terminate_as_interrupt(*, enabled: bool = True):
    """Make SIGTERM arrive as `KeyboardInterrupt`, so a scheduler's stop signal takes the same path
    Ctrl-C already does, and is restored on the way out.

    Python's default disposition for SIGTERM kills the process outright: no exception, so no
    `except KeyboardInterrupt` clause runs, no exit code is chosen, nothing is printed. Under the
    ledger that meant the interrupted step kept its `attempted` record with no terminal record
    beside it — indistinguishable on resume from a crash before dispatch, so the rung re-dispatched
    and was billed a second time. Since every supervisor stops a process with SIGTERM (systemd,
    Kubernetes, a cron timeout wrapper, a cancelled CI job), the resume story was unreachable from
    every deployment shape that most needs it, and reachable only from a terminal.

    Raising `KeyboardInterrupt` rather than adding a parallel shutdown path is the point: the exit
    code, the "resumable" line with its run id, and the `cancelled` journal record asyncio writes
    when the walk unwinds are all existing, tested consequences of Ctrl-C, and a mirror cannot
    drift away from them.

    Three dispositions are left alone. An inherited `SIG_IGN` means a parent deliberately shielded
    this process (`nohup`, a shell's asynchronous `&` job, a masking supervisor); reinstalling a
    handler over that shield would break a contract someone set on purpose. A `getsignal` of `None`
    reports an unknown handler — one installed from C, before `main()` ran — which the Python API
    cannot hand back: `signal.signal(SIGTERM, None)` raises `TypeError`, so installing over such a
    handler both destroys it for the rest of the process and arms our own restore to raise out of a
    `finally`, replacing whatever was unwinding. Someone else owns that signal. And `signal.signal`
    only works on the main thread, so an embedded caller driving `main()` from a worker thread gets
    today's behaviour rather than a `ValueError`. `enabled=False` is the fourth: `serve` hands the
    process to uvicorn, which installs its own handlers for a graceful drain and then re-raises the
    captured signal after restoring what was there before — so a handler of ours would fire AFTER a
    clean shutdown and offer resume advice about a journal a server never arms.

    Where the interrupt is raised matters as much as that it is raised
    ----------------------------------------------------------------
    A `KeyboardInterrupt` thrown from a signal handler lands in whatever frame the main thread
    happened to be executing, and two of those frames belong to asyncio's own event-loop
    bookkeeping. `BaseEventLoop.run_forever` marks the loop running in `_run_forever_setup()` and
    unmarks it in `_run_forever_cleanup()`; the setup call sits OUTSIDE `run_forever`'s `try` and
    the cleanup call is the whole of its `finally`, so an exception raised inside either one leaves
    `loop.is_running()` true with nothing left to correct it. `asyncio.run`'s teardown then reaches
    `loop.close()`, which refuses — `RuntimeError: Cannot close a running event loop` — from inside
    a `finally`, where it REPLACES the interrupt that was unwinding. Every clause keyed on the
    interrupt is bypassed at once: no exit 6, no run id, no `cancelled` record, no exit 143, no
    one-line message. What the caller gets instead is `[strategy:<name>] error: RuntimeError: ...`
    and exit 1, which reads like the document failed to parse. Both rules below exist to keep the
    interrupt out of those two frames.

    Rule one: the stop is claimed AT MOST ONCE, and the claim covers SIGTERM AND SIGINT together.
    A second stop signal is not a second decision, it is the same stop arriving down another path —
    every signal-forwarding parent (`uv run`, tini and the other container init shims, any
    supervisor whose `killpg` reaches both the wrapper and the wrapped process) delivers it twice
    by construction, and a responder who runs `kill` and then reaches for Ctrl-C sends two
    different ones. The first stop is already unwinding by then, and it unwinds THROUGH
    `_run_forever_cleanup`, so a second interrupt raised on top of it is aimed squarely at the
    window above. This is the flake that was measured twice: 32 of 40 `uv run` + `killpg` runs died
    at exit 1 on the RuntimeError, and after a first fix that deduped SIGTERM against SIGTERM only,
    1 of 160 SIGTERM-then-SIGINT runs still did — because the repeat arrived as the OTHER signal
    and never reached this handler at all. `asyncio.Runner._on_sigint` counts interrupts and does
    `raise KeyboardInterrupt()` on every call after the first, so spending asyncio's count on our
    delegation is what makes a subsequent real Ctrl-C the dangerous one. SIGINT is therefore taken
    over at the instant the stop is claimed, BEFORE the delegation runs, and dropped from then on.
    Ignoring repeats costs nothing operationally — a supervisor's escalation is SIGKILL, which is
    not ours to catch and still works — but it does mean "Ctrl-C twice to force out" ends at the
    first Ctrl-C once a stop is under way.

    Rule two: hand the stop to `asyncio.Runner`'s own SIGINT handler while one is installed.
    `Runner` installs one for the whole of `run_until_complete`, and it is interrupt-safe by
    construction: it cancels the main task and wakes the loop instead of throwing through the
    loop's internals, and the `KeyboardInterrupt` is then raised by `Runner` itself once the loop
    has unwound and closed. That covers the frame a run is in for essentially all of its wall time.
    It is also the more faithful mirror — Ctrl-C reaches exactly this handler — and it keeps the
    `cancelled` journal record, which is written by the cancellation path and not by the interrupt.
    Only that handler qualifies (`_is_asyncio_sigint_handler`); anything else, including an ignored
    SIGINT and a third-party handler that merely sets a flag, gets the direct `KeyboardInterrupt`,
    because a delegation that does not stop the process is a worse failure than the one being
    fixed.

    Rule three: ask that handler what it will do BEFORE handing it the stop
    (`_asyncio_sigint_verdict`), because it answers two opposite situations with the same
    `KeyboardInterrupt` and once it has raised they are indistinguishable. Raising because an
    interrupt is already counted means a real Ctrl-C beat the SIGTERM by milliseconds and its
    teardown is under way: swallow, and re-attribute the claim to SIGINT so the exit code names the
    signal that actually started the stop (130, not 143). Raising because the main task is already
    DONE means the opposite — nothing is unwinding, asyncio has nothing left to cancel, and
    swallowing loses the supervisor's stop entirely. A batch then parses its remaining documents
    and exits 0 with the SIGTERM already delivered, which is a process that ignores SIGTERM, dressed
    as a clean run. That case takes the direct raise, which is safe in that particular frame:
    `_run_forever_cleanup` unmarks the loop as running before the last statement, so the interrupt
    unwinds through `Runner.close()` intact rather than stranding the loop the way the startup
    window next door does.

    Known gaps. SIGINT arriving twice with no SIGTERM involved is asyncio's own escalation and is
    untouched: `Runner` refuses to install its handler unless SIGINT is still `default_int_handler`
    when `run()` is called, so wrapping SIGINT up front would cost the safe cancellation path for
    every Ctrl-C — a certain loss traded against an unlikely one. Also left: a first stop signal
    arriving during `asyncio.run`'s own teardown, after `Runner` has restored SIGINT and while it
    is closing the loop. That is a window of microseconds at the very end of a run, and it is the
    same window Ctrl-C has had all along — closing either needs a fix in CPython, not here.
    """
    if not enabled:
        yield
        return
    try:
        previous = signal.getsignal(signal.SIGTERM)
    except (AttributeError, ValueError):  # pragma: no cover - no SIGTERM on this platform
        yield
        return
    if previous is signal.SIG_IGN or previous is None:
        yield  # someone else owns this signal — see the third paragraph of the docstring
        return

    fired: list[int] = []

    def _already_stopping(_signum, _frame):
        return  # rule one, for every stop signal after the first — see this function's docstring

    def _raise(signum, _frame):
        if fired:
            return  # rule one: the same stop, arriving again
        fired.append(signum)
        sigint = signal.getsignal(signal.SIGINT)
        if _is_asyncio_sigint_handler(sigint):
            # rule two: asyncio knows how to unwind itself. Take SIGINT over FIRST, so a real
            # Ctrl-C landing while the delegation runs cannot reach asyncio's interrupt counter
            # and be raised into the teardown this call is about to start.
            # A Python handler only ever runs on the main thread of the main interpreter, so
            # this cannot raise — but this is the one frame where an escaping exception IS the
            # bug being fixed, so it is suppressed rather than reasoned about.
            with contextlib.suppress(OSError, ValueError):
                signal.signal(signal.SIGINT, _already_stopping)
            verdict = _asyncio_sigint_verdict(sigint)
            if verdict == "stopping":
                # a real Ctrl-C started the stop first, so the interrupt already unwinding is
                # that one and the exit code belongs to it. Nothing to add — least of all a
                # second interrupt aimed into its teardown.
                fired[0] = signal.SIGINT
                return
            if verdict == "cancel":
                try:
                    sigint(signal.SIGINT, _frame)
                except KeyboardInterrupt:
                    fired[0] = signal.SIGINT  # asyncio counted a stop between the ask and the call
                return
            # "declines": the walk is over and asyncio has nothing left to cancel, so nobody else
            # is going to stop this process — fall through to the raise that always does.
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _raise)
    except (OSError, ValueError):  # pragma: no cover - not the main thread
        yield
        return
    try:
        yield
    except KeyboardInterrupt:
        if not fired or fired[0] != signal.SIGTERM:
            raise  # a real Ctrl-C on an unarmed run: pre-ledger behaviour, unchanged
        # An armed run never reaches here — its handler already returned 6 with a run id. Unarmed
        # there is nothing to resume, and the mirror has to stop: letting the interrupt unwind
        # would report 130 (SIGINT) for a signal the caller did not send, over a traceback that
        # costs a responder their first minute. One line, and the conventional 128 + SIGTERM.
        print(
            "[openreading] terminated by SIGTERM. No run journal was armed, so nothing is"
            " resumable. Set OPENREADING_LEDGER to a directory to make the next one resumable.",
            file=sys.stderr,
        )
        raise SystemExit(143) from None
    finally:
        signal.signal(signal.SIGTERM, previous)
        if signal.getsignal(signal.SIGINT) is _already_stopping:
            # `Runner`'s own restore declines to touch a handler it no longer owns, so handing
            # SIGINT back is ours. `default_int_handler` is both what `Runner` would have restored
            # and what SIGINT must have held for `Runner` to have installed anything at all.
            signal.signal(signal.SIGINT, signal.default_int_handler)


def main(argv: list[str] | None = None) -> int:
    """Parse argv, load ./.env or --env-file, dispatch the handler, and return its exit code.
    SIGTERM arrives as KeyboardInterrupt for every command but `serve`, which hands its own
    signals to uvicorn."""
    args = build_parser().parse_args(argv)
    load_dotenv(getattr(args, "env_file", None))  # ./.env or --env-file; never overrides set env
    with _terminate_as_interrupt(enabled=args.func is not cmd_serve):
        return args.func(args)
