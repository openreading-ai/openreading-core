"""How big a benchmark run is, before it runs.

A benchmark target is one backend or strategy the run parses the corpus with. This module counts
what a planned run will actually do: how many documents it will touch, how many pages those
documents hold, and how many billed calls each target turns that into. It prints the count and,
above a threshold, asks before proceeding.

It used to print dollars. Each hosted target's page count was multiplied by a per-page range read
off `descriptor.cost`, and the total decided whether the run stopped to ask. Those rates were
numbers this package wrote down about someone else's rate card, unverifiable here and silently
stale the day a vendor repriced. The page count they multiplied was
the honest half: it comes from reading the documents.

Three honesties the count keeps:

- **Pages, not documents.** Every hosted backend in the catalog bills per page, and the two
  differ by a lot. ExtractBench is 370 documents and 4,869 pages, so a document count understates
  the work by roughly thirteen times.
- **A strategy target is one or more calls per document, not one.** A strategy escalates, so one
  document can become two or three calls across different backends. Nothing here knows how many
  rungs will fire, so a strategy target reports its documents and says the call count is a floor.
- **A document whose pages cannot be read is counted as unknown, not as one page.** A corrupt or
  missing file is reported as uncounted rather than folded in at a made-up size.

A hosted run asks before it starts when core cannot bound what it is about to do: either the
corpus is above `CONFIRM_ABOVE_PAGES` pages, or a target's call count is not knowable at all,
which is what a strategy target is. A hosted target bills the caller's own account, and a count
core cannot state is not a count a caller can consent to. A session with no terminal attached
never blocks on the question, because a run left waiting on stdin in CI is worse than one that
refuses: it fails with an exit code and the flag that answers it.

Environment variables this module reads
---------------------------------------
None. The confirmation gate takes its answer from the CLI flag or the terminal, never from the
environment, so no variable can quietly turn confirmation off for a whole machine.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from openreading.evals.subset import SubsetPlan

# Above this many pages, a hosted run asks first. Chosen so the default handful-of-documents smoke
# run passes without a prompt, while a full-corpus run of a hosted backend stops to ask.
CONFIRM_ABOVE_PAGES = 25

# A non-PDF source is one page. The publisher corpora carry single images alongside PDFs, and a
# page is the unit of work either way.
_SINGLE_PAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".jfif", ".tiff", ".tif", ".webp"})


@dataclass(frozen=True)
class TargetScope:
    """One target's share of a planned run."""

    reference: str
    hosted: bool
    calls: int | None  # None when the target escalates and the count is not knowable here
    note: str = ""

    def render(self) -> str:
        where = "hosted, bills your account" if self.hosted else "runs on this machine"
        if self.calls is None:
            return f"  {self.reference}: {self.note} ({where})"
        return f"  {self.reference}: {self.calls} call(s) ({where})"


@dataclass(frozen=True)
class RunScope:
    """Everything known about a run's size before it starts."""

    documents: int
    total_available: int
    pages: int
    pages_unknown: int
    targets: tuple[TargetScope, ...]

    @property
    def hosted(self) -> tuple[str, ...]:
        """The targets that will bill the caller's own account."""
        return tuple(t.reference for t in self.targets if t.hosted)

    @property
    def unbounded(self) -> tuple[str, ...]:
        """The hosted targets whose call count this preflight cannot state."""
        return tuple(t.reference for t in self.targets if t.hosted and t.calls is None)

    @property
    def needs_confirmation(self) -> bool:
        """True when a hosted target is about to be handed work core cannot bound: more than a
        smoke run's worth of pages, or a call count it cannot state at all."""
        if self.unbounded:
            return True
        return bool(self.hosted) and self.pages > CONFIRM_ABOVE_PAGES

    def render(self) -> str:
        """The block printed before a run, and the whole basis of the decision to continue."""
        left = self.total_available - self.documents
        scope = f"{self.documents} document(s)"
        if left > 0:
            scope += f" of {self.total_available} prepared ({left} not run)"
        pages = f"{self.pages} page(s)"
        if self.pages_unknown:
            pages += f" (+{self.pages_unknown} document(s) whose page count could not be read)"
        lines = [f"scope: {scope}, {pages}, {len(self.targets)} target(s)"]
        lines += [t.render() for t in self.targets]
        if self.hosted:
            lines.append(
                "  a hosted target bills your own account per page; the rates are on your invoice"
            )
        return "\n".join(lines)


def count_pages(plan: SubsetPlan) -> tuple[int, int]:
    """Return the planned corpus's page count, and how many documents could not be counted."""

    from openreading.derive.pages import pdf_page_count

    pages = 0
    unknown = 0
    # One FILE is one call, even when two categories assert against it. ParseBench shares
    # inference between `text_content` and `text_formatting`, so the same PDF appears as two
    # documents and is parsed once. Counting it twice would overstate the work.
    seen: set[Path] = set()
    for document in plan.documents:
        path = Path(document.path)
        if path in seen:
            continue
        seen.add(path)
        if not path.is_file():
            unknown += 1
            continue
        if path.suffix.lower() in _SINGLE_PAGE_SUFFIXES:
            pages += 1
            continue
        counted = pdf_page_count(path.read_bytes())
        if counted is None:
            unknown += 1
        else:
            pages += counted
    return pages, unknown


def _backend_target_scope(reference: str, slug: str, documents: int) -> TargetScope:
    from openreading.adapters.registry import make_adapter

    try:
        descriptor = make_adapter(slug).descriptor
    except KeyError:
        return TargetScope(reference, hosted=False, calls=None, note=f"unknown backend {slug!r}")
    # `type` is the one fact here core owns: a hosted_api reaches someone else's endpoint with the
    # caller's key, an oss_library runs in this process. Nothing about the vendor's terms is read.
    return TargetScope(
        reference, hosted=descriptor.type.value == "hosted_api", calls=documents
    )


def scope_run(plan: SubsetPlan, targets) -> RunScope:
    """Size one planned run across its targets, counting pages as well as documents."""

    pages, unknown = count_pages(plan)
    documents = len(plan.documents)
    scoped: list[TargetScope] = []
    for target in targets:
        if target.kind == "strategy":
            scoped.append(
                TargetScope(
                    target.reference,
                    hosted=True,  # a strategy may reach any backend it names, so assume hosted
                    calls=None,
                    note=(
                        f"at least {documents} call(s) — a strategy escalates, so one document "
                        "is one or more calls"
                    ),
                )
            )
        else:
            scoped.append(_backend_target_scope(target.reference, target.name, documents))
    return RunScope(
        documents=documents,
        total_available=plan.total_available,
        pages=pages,
        pages_unknown=unknown,
        targets=tuple(scoped),
    )


def confirm(scope: RunScope, *, assume_yes: bool, stream=None) -> bool:
    """Ask before running, unless the caller already answered with `--yes`.

    Returns True to proceed. A session with no terminal never blocks: it returns False so the
    caller can exit with the flag that answers the question, because a CI job hung on stdin is a
    worse failure than one that stops and says what to pass.
    """

    if assume_yes or not scope.needs_confirmation:
        return True
    out = stream or sys.stderr
    if not sys.stdin.isatty():
        print(
            "[benchmark] this run needs confirmation and no terminal is attached; pass --yes",
            file=out,
        )
        return False
    print("[benchmark] this run will bill your accounts. Continue? [y/N] ", end="", file=out)
    out.flush()
    return sys.stdin.readline().strip().lower() in {"y", "yes"}
