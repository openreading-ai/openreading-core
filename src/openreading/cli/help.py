r"""`openreading help [TOPIC]`: the long-form manual, served from this package's own docstring.

What the reader gets: one chapter of the CLI reference printed on stdout, at the prompt they are
already standing at, with no browser and no `pydoc` scroll. `openreading help` with no topic
prints the index.

The chapters are not written here. Every one of them is a section of the `openreading.cli`
package docstring, located by its heading and printed verbatim. That is the whole design, and it
buys the one property a manual needs: there is a single source, so `help` cannot disagree with
`pydoc`, and a fact corrected in one place is corrected in both. The alternative, a second copy
of the prose in this module, is the authoritative-and-wrong document that AGENTS.md exists to
prevent, one import away from the thing it contradicts.

The contract
------------
A HEADING is a flush-left non-blank line followed immediately by a flush-left RULE line of three
or more characters, every one of them `-` (a level-1 section) or every one of them `.` (a
level-2 subsection). The rule's length is not required to match the title's, because three
headings in the docstring already differ by a character and a strict rule would silently find
eleven sections out of twenty-three.

A section's BODY runs from the line after its rule to the line before the next heading whose
level is less than or equal to its own. Containment falls out of that: `help parse` prints the
`parse` section INCLUDING its level-2 `Batch:` subsection, and `help batch` prints the
subsection alone. Nothing else spans two sections. A topic that seems to need two means the
docstring is wrong and the sections should merge, so this module never concatenates.

Slugs are DECLARED in `TOPICS`, never derived from heading text. Derivation cannot work here:
headings carry usage signatures (`parse <file|url|dir|glob ...>`), and the slug a reader reaches
for is often not the heading's first word. Somebody holding a document with patient data types
`backends-policy`, and the heading says `route`. `TOPICS` order is the index order.

Rendering is VERBATIM. `help` prints lines; it does not reflow them. The docstring interleaves
flush-left prose, bullets, aligned flag tables, an exit ladder and literal output samples with no
blank line between forms, so any runtime rewrapper mangles at least one of them. The docstring is
kept at 79 columns instead, which `tests/test_cli_help.py` enforces.

Two kinds of line are dropped on the way out. A `Provenance:` line names files in the private
company repository, which a reader of this package cannot open. And the docstring's own
`internal/` pointers are gone for the same reason.

There is no pager. A pager would break `openreading help exit-codes | grep 143`, and a reader who
wants one pipes to `less`.

Environment variables this module reads
---------------------------------------
None. It reads `openreading.cli.__doc__`, which `python -OO` discards; `help` then exits 3 rather
than printing an empty chapter.
"""

from __future__ import annotations

import difflib
import re
import sys
from dataclasses import dataclass

import openreading.cli

# A rule line: three or more of `-` (level 1) or `.` (level 2), nothing else on it.
_RULE = re.compile(r"^(-{3,}|\.{3,})$")
# Maintainer breadcrumbs. A reader of this package cannot open either one.
_PRIVATE = re.compile(r"^\s*Provenance:|internal/")


@dataclass(frozen=True)
class Topic:
    """One chapter: the slug a reader types, the docstring heading it resolves to, the other
    names that reach it, and whether the index lists it."""

    slug: str
    heading: str
    aliases: tuple[str, ...] = ()
    listed: bool = True


# The order here is the index's order. Grouped by what the reader is trying to do, because a
# reader with a folder of documents does not know which verb owns their problem.
TOPICS: tuple[Topic, ...] = (
    # start here
    Topic("quickstart", "Quickstart", ("start", "tutorial")),
    Topic("response", "Understanding the response JSON", ("envelope", "json")),
    Topic("help", "help [TOPIC]", ("manual",)),
    Topic("output", "What lands on stdout, on stderr, and in the exit code", ("stdout", "stderr")),
    Topic("chaining", "Chaining one verb into the next", ("chain", "pipeline", "compose")),
    # do one job
    Topic(
        "batch",
        "Batch: a directory, a glob, or two or more sources",
        ("folder", "folders", "directory", "glob", "many"),
    ),
    Topic("backends-policy", "route <file|url> [--config FILE] [--run]", ("route", "policy")),
    Topic(
        "gates",
        "Gates: exact checks behind strategy shorthand",
        ("gate", "looks_bad", "low_confidence", "missing", "disagree", "escalate_when"),
    ),
    Topic("usage", "What a run uses, and how to use less", ("cost", "money", "spend", "billing")),
    Topic("env", "Environment variables this module reads", ("environment", "keys", "credentials")),
    Topic("datasets", "Datasets for calibrate, leaderboard and rules", ("dataset", "labels")),
    # when something stops
    Topic("exit-codes", "Exit codes", ("exits", "exit", "exitcodes")),
    Topic("signals", "Signals, and what a stopped run leaves behind", ("ctrl-c", "sigterm")),
    # one command at a time
    Topic("parse", "parse <file|url|dir|glob ...>"),
    Topic("backends", "backends [--check SLUG[,SLUG...]|all] [--timeout SECONDS]", ("check",)),
    Topic("resume", "resume RUN_ID", ("ledger", "journal")),
    Topic("compare", "compare <subjects...>", ("diff", "delta")),
    Topic("strategy", "strategy <verb>: list, show, validate, normalize, plan", ("strategies",)),
    Topic("explain", "explain <response.json | batch-result.json | comparison-report.json>"),
    Topic("replay", "replay <file|url> --trace <response.json>"),
    Topic("calibrate", "calibrate <dataset> --strategy NAME"),
    Topic("benchmark", "benchmark <list|show|prepare|estimate|run|report>"),
    Topic(
        "benchmark-usage",
        "What a benchmark run uses, and how to use less",
        (),
        listed=False,  # a child of `benchmark`; an index that lists every subsection is not one
    ),
    Topic("leaderboard", "leaderboard <dataset_dir>", ("rank",)),
    Topic("rules", "rules <dataset>"),
    Topic("serve", "serve", ("http", "server", "api")),
    # every command
    Topic("invariants", "Invariants shared by every subcommand", ("common", "always")),
)

# Which heading each group of the index prints under, in index order.
_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("START HERE", ("quickstart", "response", "help", "output", "chaining")),
    ("DO ONE JOB", ("batch", "backends-policy", "gates", "usage", "env", "datasets")),
    ("WHEN SOMETHING STOPS", ("exit-codes", "signals")),
    (
        "ONE COMMAND AT A TIME",
        (
            "parse",
            "backends",
            "resume",
            "compare",
            "strategy",
            "explain",
            "replay",
            "calibrate",
            "benchmark",
            "leaderboard",
            "rules",
            "serve",
        ),
    ),
    ("EVERY COMMAND", ("invariants",)),
)

# One line per topic for the index. Written in the reader's terms, not the module's.
_BLURBS: dict[str, str] = {
    "quickstart": "four commands, from a clone to parsed JSON, with no key",
    "response": "read the JSON: content, tables, fields, warnings, and provenance",
    "help": "find a chapter, its aliases, or one command's flags",
    "output": "what goes to stdout, what goes to stderr, what the code says",
    "chaining": "which verb's output feeds which verb's input",
    "batch": "a folder, a glob, or many files as one run and one JSON",
    "backends-policy": "set the default backend chain, in preference order",
    "gates": "write escalation checks, inspect defaults, and understand skipped signals",
    "usage": "what a run consumes, in the units each backend meters in",
    "env": "where keys come from, and every variable this CLI reads",
    "datasets": "case.json inputs and expectations for calibration and scoring",
    "exit-codes": "every exit code, what caused it, and whether to retry",
    "signals": "Ctrl-C, SIGTERM, and what a stopped run leaves behind",
    "parse": "read one document, a folder, or a glob",
    "backends": "list backends and whether this machine can run them",
    "resume": "pick an interrupted run back up from its journal",
    "compare": "show where two or more backends disagree",
    "strategy": "plans over several backends, and the openreading.yaml grammar",
    "explain": "read a saved run's trace, gate by gate",
    "replay": "re-run a strategy taking a saved trace's choices",
    "calibrate": "derive gate thresholds from a sample of your documents",
    "benchmark": "run a public benchmark and read the publisher's own numbers",
    "leaderboard": "rank backends on documents you labeled yourself",
    "rules": "seed publisher rules from a dataset's own expectations",
    "serve": "run the HTTP API on this machine",
    "invariants": "what holds no matter which verb you type",
}


def _doc_lines() -> list[str]:
    """The package docstring as lines. Empty under `python -OO`, which discards docstrings."""
    return (openreading.cli.__doc__ or "").splitlines()


def sections(lines: list[str] | None = None) -> dict[str, list[str]]:
    """Every heading in the docstring mapped to its body, honoring level containment.

    A level-1 body swallows the level-2 subsections beneath it, so `parse` carries `Batch:` and
    `Batch:` still resolves on its own.
    """
    lines = _doc_lines() if lines is None else lines
    found: list[tuple[int, str, int]] = []  # (line index of the title, title, level)
    for i, line in enumerate(lines):
        if i == 0 or not _RULE.match(line):
            continue
        title = lines[i - 1]
        if not title.strip() or title.startswith(" "):
            continue
        found.append((i - 1, title.strip(), 1 if line[0] == "-" else 2))

    out: dict[str, list[str]] = {}
    for n, (start, title, level) in enumerate(found):
        end = len(lines)
        for later_start, _, later_level in found[n + 1 :]:
            if later_level <= level:
                end = later_start
                break
        body = lines[start + 2 : end]
        while body and not body[-1].strip():
            body.pop()
        out[title] = body
    return out


def resolve(name: str) -> Topic | None:
    """A slug or an alias to its topic, case-insensitively."""
    key = name.strip().lower()
    for topic in TOPICS:
        if key == topic.slug or key in topic.aliases:
            return topic
    return None


def _near(name: str) -> str | None:
    """The closest slug or alias to a name that resolved to nothing, or None."""
    every = [t.slug for t in TOPICS] + [a for t in TOPICS for a in t.aliases]
    hits = difflib.get_close_matches(name.strip().lower(), every, n=1, cutoff=0.6)
    if not hits:
        return None
    found = resolve(hits[0])
    return found.slug if found else None


def render(topic: Topic) -> list[str]:
    """One chapter: its heading, its rule, its body, and the nav line, private lines dropped."""
    body = sections().get(topic.heading)
    if body is None:  # pragma: no cover - the bijection test makes this unreachable
        return []
    kept = [line for line in body if not _PRIVATE.search(line)]
    listed = [t for t in TOPICS if t.listed]
    nav = ""
    if topic.listed:
        at = listed.index(topic)
        parts = []
        if at:
            parts.append(f"Prev: openreading help {listed[at - 1].slug}")
        if at + 1 < len(listed):
            parts.append(f"Next: openreading help {listed[at + 1].slug}")
        nav = "  ".join(parts)
    out = [topic.heading, "-" * len(topic.heading), *kept]
    return [*out, "", nav] if nav else out


def index() -> list[str]:
    """The topic index, grouped by what the reader is trying to do."""
    out = [
        "openreading help TOPIC prints one chapter of the manual on stdout. Pipe it",
        "to a pager or to grep. Every command also answers `openreading <cmd> --help`.",
    ]
    for heading, slugs in _GROUPS:
        out += ["", heading]
        out += [f"  {slug:<14} {_BLURBS[slug]}" for slug in slugs]
    out += [
        "",
        "  openreading help batch             one chapter",
        "  openreading parse --help           one command's flags",
        "  python -m pydoc openreading.cli    all of it, in source order",
    ]
    return out


def cmd_help(args) -> int:
    """`openreading help [TOPIC]`. The index on stdout at exit 0, an unknown topic at exit 2."""
    if not _doc_lines():
        print(
            "[help] this interpreter discarded its docstrings (python -OO), so the manual is"
            " not available. Run openreading from a normal interpreter.",
            file=sys.stderr,
        )
        return 3
    name = getattr(args, "topic", None)
    if not name:
        print("\n".join(index()))
        return 0
    topic = resolve(name)
    if topic is None:
        near = _near(name)
        hint = f"; did you mean '{near}'?" if near else ""
        print(f"[help] unknown topic '{name}'{hint}", file=sys.stderr)
        print("\n".join(index()), file=sys.stderr)
        return 2
    print("\n".join(render(topic)))
    return 0
