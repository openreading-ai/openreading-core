r"""`openreading help [TOPIC]` serves the `openreading.cli` docstring, so these guard the seam.

The chapters are not written in `openreading.cli.help`; they are located in the package docstring
and printed verbatim. That is what keeps one source of truth, and it is also what can rot
silently: rename a heading and a topic resolves to nothing, add a section and no slug reaches it,
write a 99-column sentence and a chapter wraps in an 80-column terminal. Nothing about any of
those fails a normal test run, so they fail here instead.

`tests/conftest.py`'s docstring is the runbook for the suite as a whole.
"""

from __future__ import annotations

import argparse
import re

import pytest

import openreading.cli
from openreading.cli.app import build_parser, main
from openreading.cli.help import _PRIVATE, _RULE, TOPICS, render, resolve, sections

_DOC = (openreading.cli.__doc__ or "").splitlines()
_SLUGS = [t.slug for t in TOPICS]
_LINE_BUDGET = 79


def _headings() -> list[str]:
    """Every heading the contract in `openreading.cli.help` recognizes, in docstring order."""
    out = []
    for i, line in enumerate(_DOC):
        if i and _RULE.match(line) and _DOC[i - 1].strip() and not _DOC[i - 1].startswith(" "):
            out.append(_DOC[i - 1].strip())
    return out


def test_every_heading_has_exactly_one_slug_and_the_reverse():
    """The bijection. A heading with no slug is a chapter nobody can open; a slug with no heading
    is a topic that resolves to nothing at the prompt."""
    headings = _headings()
    declared = [t.heading for t in TOPICS]
    assert sorted(headings) == sorted(declared), (
        "the docstring's sections and TOPICS have drifted apart. Missing a slug: "
        f"{sorted(set(headings) - set(declared))}. Naming a heading that is gone: "
        f"{sorted(set(declared) - set(headings))}."
    )
    assert len(set(declared)) == len(declared), "two slugs point at one heading"
    assert len(set(_SLUGS)) == len(_SLUGS), "a slug is declared twice"


def test_every_subcommand_resolves_to_a_topic():
    """A verb that ships with no chapter is the gap this whole verb exists to close."""
    sub = next(
        a for a in build_parser()._actions if getattr(a, "choices", None) and a.dest == "command"
    )
    for name in sub.choices:
        if name == "help":
            continue
        assert resolve(name) is not None, f"`openreading {name}` has no `openreading help` topic"


def test_the_index_names_every_listed_slug_once():
    from openreading.cli.help import index

    text = "\n".join(index())
    for topic in TOPICS:
        hits = len(re.findall(rf"^  {re.escape(topic.slug)} ", text, re.M))
        assert hits == (1 if topic.listed else 0), f"{topic.slug} appears {hits} times in the index"


@pytest.mark.parametrize("name", _SLUGS + [a for t in TOPICS for a in t.aliases])
def test_every_slug_and_alias_resolves(name):
    assert resolve(name) is not None


def test_rules_are_well_formed_and_nothing_else_looks_like_one():
    """A dashed separator inside a literal output sample would invent a phantom heading."""
    for i, line in enumerate(_DOC):
        if not _RULE.match(line):
            continue
        assert i, "the docstring cannot open with a rule line"
        assert _DOC[i - 1].strip(), f"line {i + 1} rules over a blank line"
        assert not _DOC[i - 1].startswith(" "), f"line {i + 1} rules over an indented line"


@pytest.mark.parametrize("topic", TOPICS, ids=lambda t: t.slug)
def test_every_topic_renders_something(topic):
    body = sections()[topic.heading]
    assert len([line for line in body if line.strip()]) >= 3, f"{topic.slug} is nearly empty"


@pytest.mark.parametrize("topic", TOPICS, ids=lambda t: t.slug)
def test_rendering_is_verbatim(topic):
    """No runtime rewrapper. The rendered body is a subsequence of the docstring's own lines, in
    order, with only private lines removed."""
    rendered = render(topic)
    body = [line for line in sections()[topic.heading] if not _PRIVATE.search(line)]
    assert rendered[0] == topic.heading
    assert rendered[2 : 2 + len(body)] == body


def test_the_docstring_fits_an_eighty_column_terminal():
    """`help` prints lines rather than reflowing them, so the source has to fit."""
    over = [(i + 1, len(line)) for i, line in enumerate(_DOC) if len(line) > _LINE_BUDGET]
    assert not over, f"{len(over)} docstring lines exceed {_LINE_BUDGET} columns: {over[:8]}"


@pytest.mark.parametrize("topic", TOPICS, ids=lambda t: t.slug)
def test_no_chapter_names_a_file_the_reader_cannot_open(topic):
    for line in render(topic):
        assert "internal/" not in line, f"{topic.slug} points at the private repo: {line!r}"
        assert not line.startswith("Provenance:"), f"{topic.slug} leaks a provenance line"


def test_bare_help_prints_the_index_and_succeeds(capsys):
    assert main(["help"]) == 0
    out = capsys.readouterr().out
    assert "START HERE" in out and "quickstart" in out


def test_a_topic_prints_its_chapter(capsys):
    assert main(["help", "batch"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Batch: a directory, a glob, or two or more sources")


def test_an_alias_reaches_the_same_chapter(capsys):
    assert main(["help", "folder"]) == 0
    assert "Batch: a directory" in capsys.readouterr().out


def test_a_level_one_topic_contains_its_subsection(capsys):
    """`help parse` carries the batch chapter; `help batch` still stands alone."""
    assert main(["help", "parse"]) == 0
    assert "Batch: a directory, a glob, or two or more sources" in capsys.readouterr().out


def test_an_unknown_topic_is_usage_and_suggests(capsys):
    assert main(["help", "exitcode"]) == 2
    err = capsys.readouterr().err
    assert "unknown topic 'exitcode'" in err and "did you mean 'exit-codes'" in err


def test_an_unknown_topic_with_no_near_match_still_lists(capsys):
    assert main(["help", "zzzzzz"]) == 2
    err = capsys.readouterr().err
    assert "did you mean" not in err and "START HERE" in err


def test_a_stripped_interpreter_refuses_rather_than_printing_nothing(monkeypatch, capsys):
    """`python -OO` discards docstrings. An empty chapter would look like a missing feature."""
    monkeypatch.setattr(openreading.cli, "__doc__", None)
    assert main(["help", "batch"]) == 3
    assert "python -OO" in capsys.readouterr().err


# --- the argparse pages -------------------------------------------------------------------


def _commands() -> dict[str, argparse.ArgumentParser]:
    """Every addressable command, keyed by what the reader types after `openreading`."""
    out: dict[str, argparse.ArgumentParser] = {}

    def walk(parser, prefix=""):
        for action in parser._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            for name, sub in action.choices.items():
                key = f"{prefix} {name}".strip()
                out[key] = sub
                walk(sub, key)

    walk(build_parser())
    return out


_COMMANDS = _commands()
_EPILOG_WIDTH = 79
_EPILOG_LINES = 24


@pytest.mark.parametrize("name", sorted(_COMMANDS))
def test_every_command_says_what_the_reader_gets(name):
    """A `--help` page with no description is a usage line and a flag list, which tells a reader
    what the command accepts and never what it does."""
    text = (_COMMANDS[name].description or "").strip()
    assert text, f"`openreading {name}` has no description"
    assert text.endswith("."), f"`openreading {name}`'s description is not a sentence"


@pytest.mark.parametrize("name", sorted(_COMMANDS))
def test_every_command_carries_a_worked_epilog(name):
    """The four parts every page owes a reader: something to paste, what to run next, the codes
    this command can actually return, and where the long form lives."""
    epilog = _COMMANDS[name].epilog or ""
    assert "Examples:" in epilog, f"`openreading {name}` shows no example"
    assert "openreading " in epilog, f"`openreading {name}`'s epilog has no runnable line"
    assert "Exits:" in epilog, f"`openreading {name}` never names an exit code"
    assert "More: openreading help " in epilog, f"`openreading {name}` points at no chapter"


@pytest.mark.parametrize("name", sorted(_COMMANDS))
def test_every_epilog_fits_one_screen(name):
    """A flag page a reader has to scroll is a flag page a reader stops reading. The long form
    already has a home in `openreading help`."""
    lines = (_COMMANDS[name].epilog or "").splitlines()
    assert len(lines) <= _EPILOG_LINES, f"`openreading {name}`'s epilog runs {len(lines)} lines"
    over = [line for line in lines if len(line) > _EPILOG_WIDTH]
    assert not over, f"`openreading {name}`'s epilog exceeds {_EPILOG_WIDTH} columns: {over}"


@pytest.mark.parametrize("name", sorted(_COMMANDS))
def test_no_help_page_sends_a_reader_to_a_private_repo_or_to_uv(name):
    """`internal/` names a file the reader cannot open, and a reader who installed the package
    from an index has no `uv`."""
    page = _COMMANDS[name].format_help()
    assert "internal/" not in page, f"`openreading {name}` points at the private repo"
    assert "uv run" not in page, f"`openreading {name}` assumes uv is installed"


def test_the_front_door_names_folders_and_chaining():
    """The two things the CLI never said out loud, and the reasons this verb set was rewritten."""
    page = build_parser().format_help()
    assert "FOLDERS AND GLOBS" in page
    assert "THINGS CHAIN." in page
    assert "openreading help" in page


def test_the_quickstart_is_above_the_verb_list():
    """argparse renders description, then subcommands, then epilog. A thirteen-verb listing
    pushes an epilog past the fold of an ordinary terminal, so the four commands a first-time
    reader can paste have to sit in the description."""
    page = build_parser().format_help().splitlines()
    quickstart = next(i for i, line in enumerate(page) if line.startswith("QUICKSTART"))
    verbs = next(i for i, line in enumerate(page) if line.startswith("positional arguments"))
    assert quickstart < verbs
    assert quickstart < 24, f"the quickstart starts at line {quickstart + 1}, below the fold"
