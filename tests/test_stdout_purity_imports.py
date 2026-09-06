"""stdout carries only the JSON envelope, so no module may import a name that prints on import.

PyMuPDF's `fitz` alias prints `warning: The \\`fitz\\` API is deprecated ...` to STDOUT the first
time it is imported. Any code path that imports it outside the CLI's stdout guard puts that line
in front of the envelope, and `openreading compare DOC --all-ready --format json` stops being
JSON at all. `import pymupdf` is the same package under the name that stays quiet.

The guard is static because the leak is an import-time side effect: it fires once per process, so
a functional test only catches it when that process imported nothing else first."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_SRC = Path(__file__).parent.parent / "src" / "openreading"
# `import fitz`, `from fitz import x`, `import fitz as y` — but not `import pymupdf as fitz`,
# which is the sanctioned spelling and the reason the alias still appears throughout the tree.
_BAD = re.compile(r"^\s*(?:import\s+fitz\b|from\s+fitz\b)", re.MULTILINE)


@pytest.mark.parametrize("path", sorted(_SRC.rglob("*.py")), ids=lambda p: p.name)
def test_no_module_imports_the_noisy_pymupdf_alias(path: Path):
    hits = _BAD.findall(path.read_text(encoding="utf-8"))
    assert not hits, (
        f"{path.relative_to(_SRC.parent.parent)} imports the deprecated `fitz` alias, which "
        "prints to stdout on import. Use `import pymupdf as fitz` instead."
    )
