"""`openreading` CLI — a thin shell over the public API (`openreading.run`/`route`). It resolves
credentials from the environment (`.env` / process env), dispatches to a backend (or the router),
and prints the one response schema.

    openreading parse   <file> --backend reducto        # run one backend
    openreading route   <file> --policy phi.json --run  # compliance-first plan (+ execute chain)
    openreading backends                                # which backends are configured, and why not
    openreading backends --check docling                # ...and is it actually answering? (probes)

Credentials never travel on the CLI: they are read from the environment by the broker
(OPENREADING_<SLUG>_<KEY> or the service-native var, e.g. REDUCTO_API_KEY). A `.env` in the
working directory is loaded automatically (never overriding an already-set var).

The user-facing reference (every subcommand, flags, exit codes 0-6) is the `openreading.cli`
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
  is an ordinary KeyboardInterrupt, byte-for-byte the pre-ledger behavior. The single-document
  path does not read the variable: it relies on `api.run`'s `on_run_armed` callback, which fires
  only when the ledger actually armed for THAT run, so a named-backend / `auto` run (which never
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
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from openreading import api, schemas
from openreading.adapters.registry import BUILTIN_ADAPTERS, build_registry, make_adapter
from openreading.batch.runner import MAX_BATCH_JOBS, JobsLimitError
from openreading.batch.sources import (
    DEFAULT_MAX_ITEMS,
    SourceLimitError,
    SourceNotFoundError,
    looks_batch,
)
from openreading.credentials import EnvCredentialBroker, load_dotenv
from openreading.ledger.header import HeaderMismatch
from openreading.ledger.ports import PayloadExpired
from openreading.liveness import check_liveness, probe_kind
from openreading.readiness import auth_rejected_backends, auth_rejected_hint, backend_readiness
from openreading.router.executor import execute_plan
from openreading.router.router import Router
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


def _print_exhausted(tag: str, e: PlanExhaustedError, trail_summary: str) -> None:
    """Report an exhausted chain/walk on stderr: the one-line trail, then — for every backend that
    failed because its key was REJECTED — the actionable `check <VAR>` hint. A trail records only
    backend/category/code, so the hint is re-derived from the slug (readiness.auth_rejected_hint)."""
    print(f"[{tag}] {trail_summary or str(e)}", file=sys.stderr)
    for backend in auth_rejected_backends(e.trail):
        print(f"[{tag}] {auth_rejected_hint(backend)}", file=sys.stderr)


class _PolicyError(Exception):
    """A `--policy` file that can't be read or parsed."""


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


def _load_policy(path: str | None) -> dict[str, Any] | None:
    """Read a `--policy` JSON file. Raises like `load_config` does for `--config` so each command
    reports it under its own tag — a bad policy path is a user error, not a crash."""
    # `is None`, not falsy: only an absent flag means "no policy". An explicit `--policy ""` has to
    # fail loudly rather than silently drop the compliance constraints the caller meant to apply.
    if path is None:
        return None
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise _PolicyError(f"cannot read policy {path}: {_describe_read_error(e)}") from e


def cmd_parse(args) -> int:
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
            make_adapter(args.backend)
        except KeyError as e:
            print(f"[parse] {e}", file=sys.stderr)
            return 2

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
        # except Exception below rather than exit 2 (the openreading.cli docstring's own documented code for `parse`:
        # an unresolvable source), landing at the wrong-but-clean exit 1 instead.
        print(f"[{label}] {e}", file=sys.stderr)
        return 2
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


def _cmd_parse_batch(args, overrides: dict, label: str) -> int:
    """Batch parse (Manifest v0.6): one envelope over many documents. Progress + cost preflight go
    to stderr; stdout stays the single batch-result JSON. Exit 4 = partial (some items failed)."""
    bkwargs: dict[str, Any] = {**overrides, "config": args.config, "operation": args.operation}
    if getattr(args, "keep_candidates", False):
        bkwargs["keep_candidates"] = True  # retained per item, so `compare --from` works on a batch
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
        live = [r for r in resolved if r.skip_reason is None]
        if backend == "auto" or backend.startswith("strategy:") or len(live) <= 10:
            return
        try:
            d = make_adapter(backend).descriptor
        except KeyError:
            return
        if d.type.value == "hosted_api":
            lo, hi = d.cost.usd_per_page_equiv_low, d.cost.usd_per_page_equiv_high
            rng = f"~${lo}-${hi}/page-equiv" if lo is not None else "billed per page"
            print(
                f"[preflight] {len(live)} items → hosted backend {backend} ({rng} each)",
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
                "[batch] interrupted; per-item runs under $OPENREADING_LEDGER may be "
                "individually resumable (openreading resume <RUN_ID>) — batch-level resume "
                "is not yet supported",
                file=sys.stderr,
            )
            return 6
        raise
    except (SourceLimitError, SourceNotFoundError, JobsLimitError) as e:
        print(f"[batch] {e}", file=sys.stderr)
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
        # Finding 6/7 (Phase C round-1, sophia): `cmd_parse` already routes the identical exception
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
    try:
        policy = _load_policy(args.policy)
    except _PolicyError as e:
        print(f"[route] {e}", file=sys.stderr)
        return 3
    try:
        req = api.build_request(args.file, "auto", policy=policy)
    except OSError as e:
        print(f"[route] cannot read {args.file}: {_describe_read_error(e)}", file=sys.stderr)
        return 3
    plan = Router(build_registry(), api.router_config(policy)).route(req)
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
                "route --run", e, f"plan exhausted — {trail or 'no eligible backend ran'}"
            )
            print(json.dumps(out, indent=2))  # the plan is still the answer to `route`
            return 3
    print(json.dumps(out, indent=2))
    return 0 if plan.chosen else 4


def cmd_serve(args) -> int:
    try:
        import uvicorn
    except ImportError:
        print(
            "[serve] serve needs the server extra: pip install 'openreading[server]'",
            file=sys.stderr,
        )
        return 3
    from openreading.server import create_app

    app = create_app(cors_origins=args.cors_origin or None)
    if args.host != "127.0.0.1":
        print(
            f"[serve] warning: binding {args.host} exposes the server — anyone who can reach it "
            "spends your vendor keys. Put it behind your own auth/proxy.",
            file=sys.stderr,
        )
    uvicorn.run(app, host=args.host, port=args.port)
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
            miss = r.missing_deps or r.required_missing
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

    `all` means "every backend that DECLARES a probe" rather than literally all thirteen: probing a
    backend whose answer can only ever be the configuration inference spends the user's patience
    for a result `openreading backends` already printed for free."""
    if not raw:
        return []
    if raw.strip() == "all":
        return [s for s, a in adapters.items() if probe_kind(a.descriptor) is not ProbeKind.NONE]
    wanted = [s.strip() for s in raw.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in adapters]
    if unknown:
        print(
            f"[backends] unknown backend(s): {', '.join(unknown)}; known: "
            f"{', '.join(sorted(adapters))}",
            file=sys.stderr,
        )
        return None
    return list(dict.fromkeys(wanted))


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
        print(f"[strategy show] unknown strategy {args.name!r}", file=sys.stderr)
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
    every error and warning; exit 3 if any error, else 0. `--policy p.json` adds a compliance
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
    try:
        policy = _load_policy(args.policy)
    except _PolicyError as e:
        print(f"[strategy validate] {e}", file=sys.stderr)
        return 3
    issues = validate_config(
        loaded.config, policy=policy, raw=loaded.raw, plain_info=loaded.plain_info
    )
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
        policy = _load_policy(args.policy)
    except _PolicyError as e:
        print(f"[strategy plan] {e}", file=sys.stderr)
        return 3
    try:
        req = api.build_request(args.file, "auto", policy=policy)
    except OSError as e:
        print(
            f"[strategy plan] cannot read {args.file}: {_describe_read_error(e)}", file=sys.stderr
        )
        return 3
    try:
        compiled = compile_strategy(
            req, args.strategy, loaded.config, build_registry(), api.router_config(policy)
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


def cmd_explain(args) -> int:
    """Render a response's orchestration block, or a comparison report, as a human story."""
    try:
        doc = json.loads(Path(args.response).read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"[explain] cannot read {args.response}: {_describe_read_error(e)}", file=sys.stderr)
        return 3
    if "subjects" in doc and "fields" in doc and "findings" in doc:  # a comparison report
        from openreading.comparison.render import render_table

        print(render_table(doc))
        return 0
    orch = doc.get("orchestration")
    if not orch:
        print(
            "[explain] no orchestration block in this response (was it a strategy run?)",
            file=sys.stderr,
        )
        return 3
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
    return 0


def cmd_replay(args) -> int:
    """Re-execute a strategy taking the LOGGED decision at each decision point (TraceDecider,
    decider.md §5). Deterministic and offline for local backends: the decisions replay exactly;
    a decision point absent from the trace takes the engine default (`trace_missing`)."""
    from openreading.credentials import EnvCredentialBroker
    from openreading.router.clock import RealClock
    from openreading.strategies import compile_strategy, run_strategy

    try:
        trace_doc = json.loads(Path(args.trace).read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"[replay] cannot read {args.trace}: {_describe_read_error(e)}", file=sys.stderr)
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
        policy = _load_policy(args.policy)
    except _PolicyError as e:
        print(f"[replay] {e}", file=sys.stderr)
        return 3
    try:
        req = api.build_request(args.file, "auto", policy=policy)
    except OSError as e:
        print(f"[replay] cannot read {args.file}: {_describe_read_error(e)}", file=sys.stderr)
        return 3
    registry = build_registry()
    try:
        compiled = compile_strategy(req, name, loaded.config, registry, api.router_config(policy))
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
                f"compiled config_hash {compiled.config_hash!r} — refusing to replay a trace "
                f"recorded under a different configuration or compliance posture",
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
    yet" shape, excluded from `scorer_agreement` rather than silently required (BL-87/Noor); the
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
        policy = _load_policy(args.policy)
    except _PolicyError as e:
        print(f"[calibrate] {e}", file=sys.stderr)
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
                router_config=api.router_config(policy),
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
    # n_scored < n_docs (BL-87/Noor): scorer_agreement is grounded only in cases whose `expected`
    # named a recognized dimension — an unlabeled sample makes it a flat, precise-looking number
    # that measured nothing, easy to mistake for "measured and found wanting."
    if report.n_scored == 0:
        print(
            f"[calibrate] 0 of {report.n_docs} cases were scored — none named a dimension "
            "scorers.score() recognizes, so scorer_agreement is not measured at any threshold "
            "(only escalation_rate/cost_per_doc are real signal here)",
            file=sys.stderr,
        )
    elif report.n_scored < report.n_docs:
        print(
            f"[calibrate] only {report.n_scored} of {report.n_docs} cases were scored — the rest "
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
        f"{'rank':>4}  {'backend':<28} {'mean':>6} {'cost/doc':>10} {'errors':>7}  dimensions",
    ]
    for b in report.backends:
        dims = " ".join(f"{k}={v:.2f}" for k, v in b.dimensions.items())
        nd = "  [non-deterministic: single sample]" if b.non_deterministic else ""
        lines.append(
            f"{b.rank:>4}  {b.backend_id:<28} {b.mean_score:>6.3f} {b.cost_per_doc:>10.4f} "
            f"{b.errors:>7}  {dims}{nd}"
        )
    lines.append("")
    lines.append("per-case winner:")
    for c in report.cases:
        scores = ", ".join(
            f"{bid}={s:.2f}" if s is not None else f"{bid}=—" for bid, s in c.scores.items()
        )
        lines.append(f"  {c.name}: winner={c.winner or '—'}  ({scores})")
    return "\n".join(lines)


def cmd_leaderboard(args) -> int:
    """Rank N registered backends on the SAME dataset.case.json corpus through the unchanged
    evals.runner.run_case path (internal/product/specs/eval-leaderboard.product-spec.md) — one
    BenchmarkReport: measured mean score, per-dimension breakdown, a per-case winner table, an
    error tally, and each backend's cost basis reported alongside its score. Reuses run_case's
    existing per-case compliance gate; never a second scoring or gating path (AC-1/AC-2)."""
    from openreading.evals.leaderboard import run_leaderboard

    try:
        policy = _load_policy(args.policy)
    except _PolicyError as e:
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
            print(f"[leaderboard] unknown backend(s): {', '.join(unknown)}", file=sys.stderr)
            return 2
    if len(ids) < 2:
        print(f"[leaderboard] need at least two backends to rank (got {len(ids)})", file=sys.stderr)
        return 2

    try:
        # backend stdout advisories (e.g. PyMuPDF) → stderr so stdout is only the rendered report.
        with contextlib.redirect_stdout(sys.stderr):
            report = run_leaderboard(
                args.dataset, ids, build_registry(), router_config=api.router_config(policy)
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
            doc = json.loads(Path(args.from_response).read_text())
        except (OSError, json.JSONDecodeError) as e:
            print(
                f"[compare] cannot read {args.from_response}: {_describe_read_error(e)}",
                file=sys.stderr,
            )
            return 5
        cands = (doc.get("orchestration") or {}).get("candidates") or []
        if not cands:
            print(
                "[compare] --from: this response kept no candidates — re-run "
                "`parse --strategy <name> --keep-candidates`",
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
        doc = args.inputs[0]
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
                print(f"[compare] unknown backend(s): {', '.join(unknown)}", file=sys.stderr)
                return 2
        if len(ids) < 2:
            print(
                f"[compare] need at least two backends to compare (got {len(ids)})", file=sys.stderr
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
            print("[compare] needs ≥2 response files, or one doc + --backends", file=sys.stderr)
            return 2
        for p in args.inputs:
            try:
                responses.append(json.loads(Path(p).read_text()))
            except (OSError, json.JSONDecodeError) as e:
                print(f"[compare] cannot read {p}: {_describe_read_error(e)}", file=sys.stderr)
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="openreading", description="Unified document-processing CLI.")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--env-file", default=None, help="path to a .env file (default: ./.env if present)"
    )
    sub = p.add_subparsers(dest="command", required=True)

    parse = sub.add_parser(
        "parse",
        parents=[common],
        help="parse a document (or a whole directory/glob) with a backend",
    )
    parse.add_argument(
        "files",
        nargs="+",
        metavar="FILE",
        help="path, http(s):// URL, directory, or glob. A directory / glob / >=2 args triggers "
        "batch mode (one batch-result JSON over many documents); a single file/URL stays single.",
    )
    parse.add_argument(
        "--backend",
        default=None,
        choices=sorted(BUILTIN_ADAPTERS),
        help="run one named backend (mutually exclusive with --strategy)",
    )
    parse.add_argument(
        "--strategy", default=None, help="run a named strategy from openreading.yaml"
    )
    parse.add_argument(
        "--no-strategy",
        action="store_true",
        help="force the router's auto choice, ignoring defaults.strategy",
    )
    parse.add_argument("--config", default=None, help="path to an openreading.yaml")
    parse.add_argument(
        "--operation", default=None, help="backend sub-operation (e.g. AnalyzeLending)"
    )
    parse.add_argument("--pages", type=int, nargs="*", default=None, help="1-based page numbers")
    parse.add_argument(
        "--extract",
        nargs="?",
        const="",
        default=None,
        metavar="INSTRUCTIONS",
        help="request schema-driven field extraction (backends that can't do it report "
        "unsupported_feature rather than silently dropping the ask)",
    )
    parse.add_argument(
        "--keep-candidates",
        action="store_true",
        help="retain every strategy branch's output under orchestration.candidates[] "
        "(for `compare --from`); off by default, no effect on a direct backend run",
    )
    parse.add_argument(
        "--jobs", type=int, default=1, help="batch: concurrent workers (default 1, serial)"
    )
    parse.add_argument(
        "--max-jobs",
        type=int,
        default=MAX_BATCH_JOBS,
        dest="max_jobs",
        help=f"batch: ceiling on --jobs (default {MAX_BATCH_JOBS}); non-positive --jobs clamps "
        "to 1, above this exits 2",
    )
    parse.add_argument(
        "--max-items",
        type=int,
        default=DEFAULT_MAX_ITEMS,
        dest="max_items",
        help=f"batch: hard cap on expanded files (default {DEFAULT_MAX_ITEMS})",
    )
    parse.add_argument(
        "--deadline",
        type=float,
        default=None,
        dest="deadline_s",
        help="absolute time budget override, in seconds. Single document with --backend NAME "
        "(BL-169): overrides the generic 120s default for that one directly-named backend — "
        "raise this for a long-running hosted async job (e.g. a large Textract document). "
        "Batch native dispatch (BL-135): overrides the deadline for a backend dispatched "
        "natively (internal/design/batch-intake.md §7 — currently anthropic-claude); default there "
        "is adapter-appropriate (e.g. 1h for anthropic-claude's documented 'most <1h'). Has no "
        "effect on `auto` or `--strategy` dispatch, which manage their own time budget. A "
        "non-positive value (0 or negative) means fail fast: don't wait at all (BL-138)",
    )
    parse.add_argument(
        "--save-dir",
        default=None,
        dest="save_dir",
        help="batch: also write each succeeded item's response to <dir>/<relpath>.json",
    )
    parse.set_defaults(func=cmd_parse)

    resume = sub.add_parser(
        "resume",
        parents=[common],
        help="resume an interrupted/failed run from its ledger journal (internal/design/ledger.md §10)",
    )
    resume.add_argument(
        "run_id",
        metavar="RUN_ID",
        help="the run id to resume — printed by `parse` on interrupt, or read from a "
        "run's own header under $OPENREADING_LEDGER. No other flags: every option comes "
        "from the ledger.",
    )
    resume.set_defaults(func=cmd_resume)

    route = sub.add_parser(
        "route", parents=[common], help="show the compliance-first routing plan for a document"
    )
    route.add_argument("file", help="path or http(s):// URL")
    route.add_argument("--policy", required=True, help="policy.json with compliance constraints")
    route.add_argument(
        "--run", action="store_true", help="also execute the chosen backend if it is ready"
    )
    route.set_defaults(func=cmd_route)

    backends = sub.add_parser(
        "backends", parents=[common], help="list backends and whether they are configured to run"
    )
    backends.add_argument(
        "--check",
        default=None,
        metavar="SLUG[,SLUG...]|all",
        help="also PROBE these backends for real liveness (network I/O; never implicit). "
        "'all' probes every backend that declares a probe.",
    )
    backends.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="per-probe timeout for --check (default 5s, clamped to [0.1, 30])",
    )
    backends.set_defaults(func=cmd_backends)

    serve = sub.add_parser(
        "serve", parents=[common], help="run the HTTP API (needs [server] extra)"
    )
    serve.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8787, help="port (default 8787)")
    serve.add_argument(
        "--cors-origin", action="append", default=None, help="allowed CORS origin (repeatable)"
    )
    serve.set_defaults(func=cmd_serve)

    # `openreading strategy <show|list|normalize|validate|plan>` — inspect the openreading.yaml
    # orchestration config. `explain` renders a run's trace; `replay` re-runs it (14.3);
    # `calibrate` lands in 15.2.
    strategy_desc = (
        "Inspect and understand your openreading.yaml strategies — recipes for which backends\n"
        'run, in what order or together, and when to move on. Written in "Plain": six keys.\n'
        "\n"
        "  try: [a, b, c]      run in order; move on if a step fails or the result looks bad\n"
        "  race: [a, b]        run at once; first success wins, the rest are cancelled\n"
        "  compare: [a, b]     run at once; keep the objectively better result\n"
        "  then: x             where compare sends the doc when it can't trust the winner\n"
        "  escalate_when: ...  when to move on — any of four judgment words below\n"
        '  max_time: "2m"      give up after this long\n'
        "\n"
        "escalate_when takes any of:  looks_bad · low_confidence · missing: [field, ...] · disagree\n"
        "  (disagree is compare-only).  auto = the best remaining backend — usable as a try rung\n"
        "  or a then: target."
    )
    strategy_epilog = (
        "Examples:\n"
        "  openreading strategy validate                    # check + explain every strategy\n"
        "  openreading strategy list                        # presets + your strategies\n"
        "  openreading strategy show contracts --longhand   # the full tree Plain compiles to\n"
        "  openreading parse doc.pdf --strategy contracts   # run a document through one\n"
        "\n"
        "A minimal openreading.yaml:\n"
        "  version: 1\n"
        "  strategies:\n"
        "    main:\n"
        "      try: [pymupdf, docling, reducto]\n"
        "      escalate_when: looks_bad\n"
        "    contracts:\n"
        "      compare: [docling, aws-textract]\n"
        "      then: reducto\n"
        "\n"
        "Commands find ./openreading.yaml automatically; use --config PATH to point elsewhere.\n"
        "Full guide: the openreading.strategies.plain docstring"
    )
    strategy = sub.add_parser(
        "strategy",
        parents=[common],
        help="inspect openreading.yaml strategies",
        description=strategy_desc,
        epilog=strategy_epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    strat_sub = strategy.add_subparsers(dest="strategy_command", required=True)

    st_show = strat_sub.add_parser("show", help="dump a strategy or preset (body as written)")
    st_show.add_argument("name", help="strategy or built-in preset name")
    st_show.add_argument("--config", default=None, help="path to an openreading.yaml")
    st_show.add_argument(
        "--longhand", action="store_true", help="print the canonical (normalized) tree instead"
    )
    st_show.set_defaults(func=cmd_strategy_show)

    st_list = strat_sub.add_parser("list", help="list built-in presets and configured strategies")
    st_list.add_argument("--config", default=None, help="path to an openreading.yaml")
    st_list.set_defaults(func=cmd_strategy_list)

    st_val = strat_sub.add_parser(
        "validate", help="check + explain every strategy in plain English"
    )
    st_val.add_argument("--config", default=None, help="path to an openreading.yaml")
    st_val.add_argument("--policy", default=None, help="policy.json — flag steps unreachable in it")
    st_val.set_defaults(func=cmd_strategy_validate)

    st_norm = strat_sub.add_parser("normalize", help="print the config's strategies as longhand")
    st_norm.add_argument("--config", default=None, help="path to an openreading.yaml")
    st_norm.set_defaults(func=cmd_strategy_normalize)

    st_plan = strat_sub.add_parser("plan", help="pruned tree for a document (no execution)")
    st_plan.add_argument("file", help="path or http(s):// URL")
    st_plan.add_argument("--strategy", required=True, help="strategy or preset name")
    st_plan.add_argument("--config", default=None, help="path to an openreading.yaml")
    st_plan.add_argument("--policy", default=None, help="policy.json compliance context")
    st_plan.set_defaults(func=cmd_strategy_plan)

    compare = sub.add_parser(
        "compare",
        parents=[common],
        help="compare backends' outputs and show the delta (fields/text/blocks)",
    )
    compare.add_argument(
        "inputs",
        nargs="*",
        default=[],
        help="≥2 response JSON files, or one document with --backends (omit with --from)",
    )
    compare.add_argument(
        "--from",
        dest="from_response",
        default=None,
        help="a saved strategy response (run parse with --keep-candidates): compare its winner "
        "against the retained orchestration.candidates[]",
    )
    compare.add_argument(
        "--backends", default=None, help="comma-separated backend ids to fan out over one document"
    )
    compare.add_argument(
        "--all-ready", action="store_true", help="fan out over every ready backend"
    )
    compare.add_argument(
        "--save-dir", default=None, help="write each fan-out response envelope into this dir"
    )
    compare.add_argument(
        "--format",
        choices=["json", "table", "diff", "diffs", "md"],
        default="json",
        help="json | table | md | diff (2-way git-style text diff) | diffs (N-way content deltas + payload diff). default: json",
    )
    compare.add_argument(
        "--show-agreements",
        action="store_true",
        help="in human formats, also list agreeing fields (hidden by default)",
    )
    compare.add_argument(
        "--baseline",
        default=None,
        help="sign deltas against this subject (a label, or a response JSON added as a subject)",
    )
    compare.add_argument(
        "--truth",
        default=None,
        help="score each subject against a golden.json (evals `expected` shape)",
    )
    compare.add_argument(
        "--deadline",
        type=float,
        default=None,
        dest="deadline_s",
        help="absolute time budget override, in seconds, applied to every fanned-out backend "
        "(BL-169) — raise this for a long-running hosted async job. Default, when omitted, is "
        "the generic 120s single-document deadline. A non-positive value (0 or negative) means "
        "fail fast: don't wait at all",
    )
    compare.set_defaults(func=cmd_compare)

    explain = sub.add_parser(
        "explain", parents=[common], help="render a response's orchestration block or a comparison"
    )
    explain.add_argument("response", help="path to a saved response or comparison-report JSON")
    explain.set_defaults(func=cmd_explain)

    replay = sub.add_parser(
        "replay", parents=[common], help="re-run a strategy taking a trace's logged decisions"
    )
    replay.add_argument("file", help="path or http(s):// URL of the document")
    replay.add_argument(
        "--trace", required=True, help="a saved response/orchestration JSON to replay"
    )
    replay.add_argument("--strategy", default=None, help="strategy name (default: from the trace)")
    replay.add_argument("--config", default=None, help="path to an openreading.yaml")
    replay.add_argument("--policy", default=None, help="policy.json compliance context")
    replay.set_defaults(func=cmd_replay)

    calibrate = sub.add_parser(
        "calibrate",
        parents=[common],
        help="derive gate thresholds from a sample of documents, labels optional (§5)",
    )
    calibrate.add_argument(
        "dataset",
        help="a dataset dir of */case.json documents (unlabeled cases are fine, "
        "excluded from scorer_agreement)",
    )
    calibrate.add_argument("--strategy", required=True, help="the strategy to tune")
    calibrate.add_argument("--config", default=None, help="path to an openreading.yaml")
    calibrate.add_argument("--policy", default=None, help="policy.json compliance context")
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

    leaderboard = sub.add_parser(
        "leaderboard",
        parents=[common],
        help="rank registered backends on one dataset — measured, not vendor-claimed",
    )
    leaderboard.add_argument(
        "dataset", help="a dataset dir of */case.json documents (evals.dataset shape)"
    )
    leaderboard.add_argument(
        "--backends", default=None, help="comma-separated backend ids to rank (>=2)"
    )
    leaderboard.add_argument(
        "--all-ready", action="store_true", help="rank every backend the environment is ready for"
    )
    leaderboard.add_argument("--policy", default=None, help="policy.json compliance context")
    leaderboard.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        help="table (default, human) | json (schema-valid BenchmarkReport)",
    )
    leaderboard.set_defaults(func=cmd_leaderboard)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(getattr(args, "env_file", None))  # ./.env or --env-file; never overrides set env
    return args.func(args)
