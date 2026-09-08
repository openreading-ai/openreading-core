"""Documentation lives in code (AGENTS.md). This test is what makes that rule enforceable.

Tracked markdown is limited to the root project files, the GitHub templates under `.github/`,
one `README.md` per directory, and design
records for work that is **not built yet** under `design/` and `product/specs/`. Everything else — reference docs, research packs, run logs —
belongs in a module docstring next to the code it describes, or in the private
`openreading` company repo (which checks this repo out as `core/`). A separate
markdown file that contradicts the code looks authoritative and is wrong, and nothing forces
anyone to notice; a docstring that contradicts the module below it is caught in review.

The `design/` carve-out does not weaken that: a proposal for unbuilt work has no code to
contradict. The risk starts the day it ships, which is why AGENTS.md requires the file to be
deleted — its durable facts moved into the module docstrings — in the PR that finishes the
implementation. Nothing here can enforce that; a reviewer has to.

Two more drift traps close here: every relative link in every tracked markdown file must resolve
(a README that points at a file that moved is the "shredded docs" failure the rule exists to
prevent), and the README's coverage badge must state the Makefile's enforced floor, so the badge
can never claim a number the gate does not guarantee.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# The only markdown allowed at the repo root. CLAUDE.md is a one-line `@AGENTS.md` shim.
ALLOWED_ROOT_MD = frozenset(
    {
        "README.md",
        "AGENTS.md",
        "CLAUDE.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CODE_OF_CONDUCT.md",
        "CHANGELOG.md",
    }
)


# Design records and product specs for proposed-but-unbuilt work. Reviewed in the open, next to
# the code they propose to change, and deleted when that code lands (AGENTS.md).
DESIGN_DIRS = frozenset({"design", "product"})

# Explicit exceptions remain empty so adding a standalone document requires a deliberate edit.
# Website and tutorial sources belong in openreading-web, which owns their validation.
ALLOWED_PATHS: frozenset[str] = frozenset()


def _tracked(pathspec: str) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--", pathspec],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(line for line in out.splitlines() if line)


def _allowed(rel: str) -> bool:
    if rel in ALLOWED_PATHS:
        return True
    p = Path(rel)
    if p.parent == Path("."):
        return p.name in ALLOWED_ROOT_MD
    if p.parts[0] == ".github":
        return True
    if p.parts[0] in DESIGN_DIRS:
        return True
    return p.name == "README.md"


def test_only_allowlisted_markdown_is_tracked():
    bad = [f for f in _tracked("*.md") if not _allowed(f)]
    assert bad == [], (
        "documentation lives in code (see AGENTS.md). Move the content of these files into the "
        "module docstring they describe, or -- if this is a design record for work that is not "
        "built yet -- into design/ or product/specs/, then git rm them: " + ", ".join(bad)
    )


def test_no_docs_directory_is_tracked():
    assert _tracked("docs/*") == [], (
        "docs/ is a gitignored working directory; a durable fact goes in a module docstring, a "
        "design spec or run log goes in internal/"
    )


@pytest.mark.parametrize("pathspec", ["website/*", "tutorial/*"])
def test_website_and_tutorial_live_in_web_repository(pathspec):
    assert _tracked(pathspec) == [], (
        "the marketing site, OSS pages, and tutorial belong in openreading-web: " + pathspec
    )


# Relative link targets only — external URLs, mailto:, and in-page anchors are not checked.
_LINK = re.compile(r"\]\((?!https?://|mailto:|#)([^)\s]+)\)")
# Every tracked markdown file. Links resolve relative to the file's own directory and against the
# git index, not the filesystem: a link into gitignored docs/ or an untracked local file would
# otherwise pass here and be dead in every clone and in CI.
_LINKED_DOCS = _tracked("*.md")


@pytest.mark.parametrize("rel", _LINKED_DOCS)
def test_relative_links_resolve(rel):
    path = ROOT / rel
    if not path.exists():
        pytest.skip(f"{rel} not present")
    tracked = set(_tracked("."))
    tracked_dirs = {str(Path(t).parent) for t in tracked} | {"."}
    missing = []
    for target in _LINK.findall(path.read_text(encoding="utf-8")):
        resolved = (path.parent / target.split("#", 1)[0]).resolve().relative_to(ROOT).as_posix()
        if resolved not in tracked and resolved not in tracked_dirs:
            missing.append(target)
    assert missing == [], f"{rel} links to paths that are not tracked in git: {missing}"


def test_readme_coverage_badge_matches_makefile_floor():
    floors = re.findall(r"--cov-fail-under=(\d+)", (ROOT / "Makefile").read_text(encoding="utf-8"))
    assert len(floors) == 1, f"expected exactly one --cov-fail-under in the Makefile, got {floors}"
    badge = re.search(
        r"img\.shields\.io/badge/coverage-%E2%89%A5(\d+)%25",
        (ROOT / "README.md").read_text(encoding="utf-8"),
    )
    assert badge, (
        "README has no coverage-floor badge (img.shields.io/badge/coverage-%E2%89%A5NN%25)"
    )
    assert badge.group(1) == floors[0], (
        f"README badge says coverage ≥{badge.group(1)}%, but the Makefile gate is {floors[0]}% — "
        "the badge states the enforced floor; change both together"
    )
