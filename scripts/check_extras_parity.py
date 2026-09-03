"""Extras parity gate (BL-158): keep the adapter registry and pyproject.toml's install extras
honest with each other.

`openreading.adapters.registry.BUILTIN_ADAPTERS` (what the router will dispatch to) and
`pyproject.toml`'s `[project.optional-dependencies]` (what `pip install openreading[<slug>]`
actually installs) describe the same relationship from two independent, hand-maintained sources.
Nothing else keeps them in sync. `[dependency-groups] dev` already carries every adapter's runtime
dependency for testing, so a build agent that adds an adapter, writes its tests, and forgets or
misspells the `pyproject.toml` extra sees a fully green `make verify` — the dev group already
supplies what the tests import — while the *shipped* package is broken for a real end user.
Eleven consecutive sprints of `internal/eng-council/reviews/sprint{15..25}-priya.md` hand-re-derived
"fifteen adapters, fifteen extras, one documented exception, clean" from scratch because nothing
mechanical produced that signal. This script is that mechanism.

Three checks, all offline, stdlib-only:

  1. Forward — every `BUILTIN_ADAPTERS` slug has a matching `pyproject.toml` extra, either by
     the same name or via the one explicit, named entry in `EXTRA_NAME_EXCEPTIONS` below.
  2. Reverse — every `pyproject.toml` extra outside `NON_ADAPTER_EXTRAS` maps back to a real
     registry slug (again allowing the exception map, applied in reverse).
  3. `all`-extra mirror — every package name that appears in a single-adapter extra (i.e. any
     extra outside `NON_ADAPTER_EXTRAS`) also appears, by name, in the `all` extra — the
     hand-maintained union extra, guarded against the identical drift one level up.

Reads exactly two things: `pyproject.toml` (stdlib `tomllib`) and the
`openreading.adapters.registry` module. No network call, no subprocess.

Exit codes: 0 parity holds · 1 one or more mismatches · 2 usage/environment error.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# One explicit, named exception per historical naming mismatch between a registry slug and its
# pyproject.toml extra name. Every exception this check tolerates must be visible here, in code —
# no wildcard, no silent skip. Today: `aws-textract`'s extra is `textract` (see
# the openreading.adapters runbook's own "Files to CREATE" note on why).
EXTRA_NAME_EXCEPTIONS: dict[str, str] = {
    "aws-textract": "textract",
}

# Documented non-adapter umbrella extras: install conveniences, not a registry slug's install
# path, and not subject to the `all`-extra package-mirror check either. Anything in
# `[project.optional-dependencies]` outside this set (and outside the reversed
# EXTRA_NAME_EXCEPTIONS values) must name a real registry slug.
# `server` is deliberately never folded into `all` (`all` is the adapter-runtime-deps union;
# pulling in fastapi/uvicorn there would misrepresent what "all" means).
NON_ADAPTER_EXTRAS: frozenset[str] = frozenset({"http", "server", "all"})

ALL_EXTRA_NAME = "all"

# The bare package name off the front of a PEP 508-ish requirement string, e.g.
# "google-cloud-documentai>=2.29" -> "google-cloud-documentai". No new dependency: this is a
# narrow prefix match, not a full requirement-string parser.
_PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")


class ParityError(RuntimeError):
    """Raised for usage/environment problems (bad pyproject.toml, unparsable requirement) —
    distinct from a parity mismatch, which is reported in a ParityReport instead of raised."""


def package_name(requirement: str) -> str:
    match = _PACKAGE_NAME_RE.match(requirement.strip())
    if not match:
        raise ParityError(f"cannot parse a package name out of requirement {requirement!r}")
    return match.group(0)


def load_registry_slugs() -> set[str]:
    """Import-time only. `registry.py` is import-safe with no extras installed (adapters
    lazy-import their runtime dep inside health/submit), so this never touches the network."""
    from openreading.adapters.registry import BUILTIN_ADAPTERS

    return set(BUILTIN_ADAPTERS)


def load_pyproject_extras(pyproject_path: Path) -> dict[str, list[str]]:
    """extra name -> its raw requirement strings, straight out of
    `[project.optional-dependencies]`. Preserves pyproject.toml's own list order."""
    try:
        with pyproject_path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ParityError(f"cannot read {pyproject_path}: {exc}") from exc
    extras = data.get("project", {}).get("optional-dependencies", {})
    return {name: list(reqs) for name, reqs in extras.items()}


@dataclass
class ParityReport:
    registry_slug_count: int
    extra_count: int
    exception_count: int
    allowlisted_extra_count: int
    missing_extras: list[str] = field(default_factory=list)
    """Registry slugs with no matching pyproject.toml extra, sorted."""
    orphaned_extras: list[str] = field(default_factory=list)
    """pyproject.toml extras that match no registry slug, sorted."""
    unmirrored: list[tuple[str, str]] = field(default_factory=list)
    """(package_name, source_extra) pairs missing from the `all` extra, sorted."""

    @property
    def ok(self) -> bool:
        return not (self.missing_extras or self.orphaned_extras or self.unmirrored)

    @property
    def matching_extra_count(self) -> int:
        return self.registry_slug_count - len(self.missing_extras)


def check_parity(
    registry_slugs: set[str],
    extras: dict[str, list[str]],
    *,
    exceptions: dict[str, str] | None = None,
    non_adapter_extras: frozenset[str] | None = None,
    all_extra_name: str = ALL_EXTRA_NAME,
) -> ParityReport:
    """Pure comparison over already-loaded data — no file or module I/O — so tests can drive
    every failure mode with constructed fixtures instead of the real pyproject.toml."""
    exceptions = EXTRA_NAME_EXCEPTIONS if exceptions is None else exceptions
    non_adapter_extras = NON_ADAPTER_EXTRAS if non_adapter_extras is None else non_adapter_extras
    extra_names = set(extras)

    missing_extras = sorted(
        slug for slug in registry_slugs if exceptions.get(slug, slug) not in extra_names
    )

    reverse_exceptions = {extra: slug for slug, extra in exceptions.items()}
    orphaned_extras = sorted(
        extra
        for extra in extra_names
        if extra not in non_adapter_extras
        and reverse_exceptions.get(extra, extra) not in registry_slugs
    )

    unmirrored: list[tuple[str, str]] = []
    if all_extra_name in extras:
        all_names = {package_name(req) for req in extras[all_extra_name]}
        for extra, reqs in extras.items():
            if extra in non_adapter_extras:
                continue
            for req in reqs:
                name = package_name(req)
                if name not in all_names:
                    unmirrored.append((name, extra))
    unmirrored.sort()

    return ParityReport(
        registry_slug_count=len(registry_slugs),
        extra_count=len(extra_names),
        exception_count=len(exceptions),
        allowlisted_extra_count=len(non_adapter_extras),
        missing_extras=missing_extras,
        orphaned_extras=orphaned_extras,
        unmirrored=unmirrored,
    )


def format_errors(report: ParityReport) -> list[str]:
    errors: list[str] = []
    for slug in report.missing_extras:
        expected = EXTRA_NAME_EXCEPTIONS.get(slug, slug)
        errors.append(
            f"registry slug {slug!r} has no matching pyproject.toml extra (expected extra "
            f"{expected!r} in [project.optional-dependencies] — add it, or if this is a "
            f"deliberate naming mismatch, add slug {slug!r} to EXTRA_NAME_EXCEPTIONS)"
        )
    for extra in report.orphaned_extras:
        errors.append(
            f"pyproject.toml extra {extra!r} matches no registry slug (remove the extra, "
            f"register the adapter in BUILTIN_ADAPTERS, or allowlist it in NON_ADAPTER_EXTRAS "
            f"if it's a deliberate non-adapter umbrella extra)"
        )
    for name, extra in report.unmirrored:
        errors.append(
            f"package {name!r} (from extra {extra!r}) is missing from the "
            f"{ALL_EXTRA_NAME!r} extra — add it there too"
        )
    return errors


def _print_report(report: ParityReport) -> None:
    if report.ok:
        print(
            f"extras-parity: OK — {report.registry_slug_count} registry slugs, "
            f"{report.matching_extra_count} matching extras, {report.exception_count} "
            f"exception(s), {report.allowlisted_extra_count} allowlisted non-adapter extra(s)"
        )
        return
    errors = format_errors(report)
    print(f"extras-parity: FAIL — {len(errors)} issue(s)", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_extras_parity",
        description=(
            "Fail unless every openreading.adapters.registry.BUILTIN_ADAPTERS slug has a "
            "matching pyproject.toml extra (both directions), and every adapter's package name "
            "is mirrored into the `all` extra."
        ),
    )
    parser.add_argument("--repo", type=Path, default=Path("."), help="repository root (default: .)")
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=None,
        help="path to pyproject.toml (default: <repo>/pyproject.toml)",
    )
    args = parser.parse_args(argv)

    repo: Path = args.repo.resolve()
    pyproject_path: Path = (args.pyproject or repo / "pyproject.toml").resolve()

    if not pyproject_path.is_file():
        print(f"check_extras_parity: pyproject.toml not found: {pyproject_path}", file=sys.stderr)
        return 2

    try:
        registry_slugs = load_registry_slugs()
        extras = load_pyproject_extras(pyproject_path)
    except ParityError as exc:
        print(f"check_extras_parity: {exc}", file=sys.stderr)
        return 2

    report = check_parity(registry_slugs, extras)
    _print_report(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
