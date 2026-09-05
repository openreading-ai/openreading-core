"""What a benchmark run will cost, in the unit the vendor actually bills.

Every hosted backend in the catalog prices per PAGE. The publisher's own corpus figures are per
DOCUMENT, and the two differ by a lot: ExtractBench is 370 documents and 4,869 pages, so a
document count understates the bill by roughly thirteen times. This module counts the pages of the
documents a run will really touch, multiplies by each backend's declared per-page range, and says
the number out loud before anything runs.

Three honesties the estimate keeps:

- **It is a range, not a price.** A descriptor carries a low and a high per-page rate, and which
  end you land on depends on the features a request asks for. Both ends are shown. The low end
  alone would read as a quote.
- **A strategy target is unpriced, not free.** A strategy escalates, so one document can be two or
  three billed calls across different backends at different rates. Nothing here knows how many
  rungs will fire, so it says so rather than guessing low.
- **A backend that publishes no rate is unpriced, not zero.** Token-billed backends
  (`google-gemini`, `nuextract`) have no per-page equivalent, and inventing one would be the same
  fabrication the channel contract forbids everywhere else.

Anything unpriced or above `CONFIRM_ABOVE_USD` asks before it spends. A session with no terminal
attached never blocks on that question, because a run left waiting on stdin in CI is worse than
one that refuses: it fails with an exit code and the flag that answers it.

Environment variables this module reads
---------------------------------------
None. The confirmation gate takes its answer from the CLI flag or the terminal, never from the
environment, so no variable can quietly turn spending confirmation off for a whole machine.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from openreading.evals.subset import SubsetPlan

# Above this, a run asks first. Chosen so the default two-document run of a hosted backend (cents)
# passes without a prompt, while any full-corpus run of one stops to ask.
CONFIRM_ABOVE_USD = 1.0

# A non-PDF source is one page. The publisher corpora carry single images alongside PDFs, and a
# page count is the billing unit either way.
_SINGLE_PAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".jfif", ".tiff", ".tif", ".webp"})


@dataclass(frozen=True)
class TargetCost:
    """One target's projected spend over the planned pages."""

    reference: str
    low_usd: float | None
    high_usd: float | None
    note: str = ""

    @property
    def priced(self) -> bool:
        """True when both ends of the range are known, so the target contributes to a total."""

        return self.low_usd is not None and self.high_usd is not None


@dataclass(frozen=True)
class CostEstimate:
    """Everything known about a run's size and spend before it starts."""

    documents: int
    total_available: int
    pages: int
    pages_unknown: int
    targets: tuple[TargetCost, ...]

    @property
    def low_usd(self) -> float:
        return sum(t.low_usd or 0.0 for t in self.targets if t.priced)

    @property
    def high_usd(self) -> float:
        return sum(t.high_usd or 0.0 for t in self.targets if t.priced)

    @property
    def unpriced(self) -> tuple[str, ...]:
        return tuple(t.reference for t in self.targets if not t.priced)

    @property
    def needs_confirmation(self) -> bool:
        """True when the run may cost real money that this estimate cannot bound."""

        return bool(self.unpriced) or self.high_usd > CONFIRM_ABOVE_USD

    def render(self) -> str:
        """The block printed before a run, and the whole basis of the decision to continue."""

        left = self.total_available - self.documents
        scope = f"{self.documents} document(s)"
        if left > 0:
            scope += f" of {self.total_available} prepared ({left} not run)"
        pages = f"{self.pages} page(s)"
        if self.pages_unknown:
            pages += f" (+{self.pages_unknown} document(s) whose page count could not be read)"
        lines = [f"estimate: {scope}, {pages}, {len(self.targets)} target(s)"]
        for target in self.targets:
            if target.priced:
                lines.append(
                    f"  {target.reference}: ${target.low_usd:.2f} to ${target.high_usd:.2f}"
                )
            else:
                lines.append(f"  {target.reference}: not priced ({target.note})")
        if any(t.priced for t in self.targets):
            lines.append(f"  total (priced targets): ${self.low_usd:.2f} to ${self.high_usd:.2f}")
        lines.append("  a range from each backend's declared per-page rates, not a quote")
        return "\n".join(lines)


def count_pages(plan: SubsetPlan) -> tuple[int, int]:
    """Return the planned corpus's page count, and how many documents could not be counted."""

    from openreading.derive.pages import pdf_page_count

    pages = 0
    unknown = 0
    for document in plan.documents:
        path = Path(document.path)
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


def _backend_target_cost(reference: str, slug: str, pages: int) -> TargetCost:
    from openreading.adapters.registry import make_adapter

    try:
        descriptor = make_adapter(slug).descriptor
    except KeyError:
        return TargetCost(reference, None, None, note=f"unknown backend {slug!r}")
    cost = descriptor.to_schema_dict().get("cost") or {}
    low = cost.get("usd_per_page_equiv_low")
    high = cost.get("usd_per_page_equiv_high")
    if low is None:
        return TargetCost(reference, None, None, note="publishes no per-page rate")
    # A local backend declares a low of 0 and no high. Its bill is genuinely zero, so a range of
    # zero to zero is the honest answer rather than an unpriced one.
    if high is None:
        high = low
    return TargetCost(reference, low * pages, high * pages)


def estimate_cost(plan: SubsetPlan, targets) -> CostEstimate:
    """Price one planned run across its targets, counting pages rather than documents."""

    pages, unknown = count_pages(plan)
    priced: list[TargetCost] = []
    for target in targets:
        if target.kind == "strategy":
            priced.append(
                TargetCost(
                    target.reference,
                    None,
                    None,
                    note="a strategy escalates, so one document is one or more billed calls",
                )
            )
        else:
            priced.append(_backend_target_cost(target.reference, target.name, pages))
    return CostEstimate(
        documents=len(plan.documents),
        total_available=plan.total_available,
        pages=pages,
        pages_unknown=unknown,
        targets=tuple(priced),
    )


def confirm(estimate: CostEstimate, *, assume_yes: bool, stream=None) -> bool:
    """Ask before spending, unless the caller already answered with `--yes`.

    Returns True to proceed. A session with no terminal never blocks: it returns False so the
    caller can exit with the flag that answers the question, because a CI job hung on stdin is a
    worse failure than one that stops and says what to pass.
    """

    if assume_yes or not estimate.needs_confirmation:
        return True
    out = stream or sys.stderr
    if not sys.stdin.isatty():
        print(
            "[benchmark] this run needs confirmation and no terminal is attached; pass --yes",
            file=out,
        )
        return False
    print("[benchmark] this run may bill your accounts. Continue? [y/N] ", end="", file=out)
    out.flush()
    return sys.stdin.readline().strip().lower() in {"y", "yes"}
