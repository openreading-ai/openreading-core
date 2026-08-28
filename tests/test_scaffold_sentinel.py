"""BL-161 — fails `make verify` while any `scripts/new_adapter.py`-generated scaffold is
unfinished. Every placeholder the generator writes carries the literal marker below; once real
research replaces it (a sourced descriptor value, a wired fixture, an un-skipped test) this test —
and `make verify` — goes green. See the openreading.adapters docstring (src/openreading/adapters/__init__.py)."""

from __future__ import annotations

from pathlib import Path

MARKER = "TODO-SCAFFOLD"
REPO_ROOT = Path(__file__).resolve().parent.parent

# Exactly the files scripts/new_adapter.py itself is allowed to create/edit (the openreading.adapters runbook
# §2's "Files to CREATE"/"Files to EDIT" tables) — scanning only these keeps this test fast and
# keeps an unrelated TODO anywhere else in the repo from ever being mistaken for scaffold
# incompleteness. `scripts/` itself is deliberately NOT scanned: the generator's own source has to
# name the marker to be able to write it, and that is not an unfinished scaffold.
_SCAN_ROOTS = [
    REPO_ROOT / "src" / "openreading" / "adapters",
    REPO_ROOT / "tests",
    REPO_ROOT / ".env.example",
    REPO_ROOT / "README.md",
    REPO_ROOT / "pyproject.toml",
]

# Files allowed to name the marker in prose without being flagged: this test itself, and
# the openreading.adapters runbook (which documents what the marker means as part of naming the generator as
# its own first step).
_SELF_REFERENCING = {"test_scaffold_sentinel.py", "the openreading.adapters runbook"}


def _iter_files() -> list[Path]:
    out: list[Path] = []
    for root in _SCAN_ROOTS:
        if root.is_file():
            out.append(root)
            continue
        out.extend(p for p in sorted(root.rglob("*")) if p.is_file())
    return out


def _offending_lines() -> list[str]:
    hits: list[str] = []
    for path in _iter_files():
        if path.name in _SELF_REFERENCING:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if MARKER in line:
                hits.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    return hits


def test_no_unfinished_scaffold_markers():
    offenders = _offending_lines()
    assert offenders == [], (
        "unfinished scripts/new_adapter.py scaffold(s) found — resolve every "
        f"{MARKER} marker (sourced descriptor values, wired fixtures, un-skipped tests) before "
        "merging, per the openreading.adapters docstring (src/openreading/adapters/__init__.py):\n"
        + "\n".join(offenders)
    )
