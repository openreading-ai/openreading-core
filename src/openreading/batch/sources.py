"""Intake resolution (Manifest v0.6, invariants M1–M5). Resolve CLI/API source arguments —
files, directories, globs, http(s) URLs — into an ordered list of `ResolvedSource` records, each a
`types.batch.SourceRef` plus an honest per-file `skip_reason`. Pure: no network beyond stat/read.

- M1 deterministic expansion: arg order preserved; a directory expands recursively, files sorted
  by relative path (bytewise); hidden (dot-prefixed) files/dirs skipped; symlinks not followed.
- M2 `looks_batch`: a directory / glob / >=2 args ⇒ batch envelope; a single file/URL ⇒ single.
- M3 honest format filter: each file's extension-derived format is checked against a caller-
  supplied supported-format set → unsupported_format / unknown_format / kept. Nothing is dropped.
- M4 size guard: expansion beyond `max_items` is a hard, early error (before any bytes are read).
- M5 mixed sources: files, dirs, globs, and URLs may be mixed; URLs pass through (not read).
"""

from __future__ import annotations

import errno
import glob as _glob
import hashlib
import itertools
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from openreading.types.batch import SourceRef
from openreading.types.errors import SourceNotFoundError

SkipReason = Literal["unsupported_format", "unknown_format"]

# The document extensions the fleet knows about (union of every adapter's input_formats + common
# aliases). Used only to tell an UNSUPPORTED-but-known format (a real doc a backend can't take)
# from an UNKNOWN one (not a document at all) — M3's two honest skip reasons.
_KNOWN_DOC_FORMATS = frozenset(
    {
        "pdf",
        "docx",
        "doc",
        "pptx",
        "ppt",
        "xlsx",
        "xls",
        "odt",
        "txt",
        "html",
        "htm",
        "rtf",
        "md",
        "png",
        "jpg",
        "jpeg",
        "tif",
        "tiff",
        "bmp",
        "gif",
        "webp",
        "svg",
        "epub",
        "mobi",
        "xps",
        "cbz",
    }
)

_GLOB_CHARS = set("*?[")

# BL-132: the one owned default for the `max_items` ceiling below. `server/app.py`'s `POST
# /v1/batch` ceiling on `documents[]` (`MAX_BATCH_DOCUMENTS`) imports this constant rather than
# hardcoding its own copy of `200`, so the CLI's directory-expansion default and the server's
# request-body default cannot drift apart, which they repeatedly did while each surface owned
# its own number.
DEFAULT_MAX_ITEMS = 200


class SourceLimitError(Exception):
    """Expansion exceeded max_items (M4)."""


# SourceNotFoundError now lives in types.errors (BL-133), so api._document_dict can raise it too
# without api.py taking on a new api → batch layering dependency. Imported back above; re-exported
# from this module unchanged (both callers here, and every importer of `batch.sources
# .SourceNotFoundError` / `batch.SourceNotFoundError`, keep working with no import-site changes).


@dataclass
class ResolvedSource:
    ref: SourceRef
    skip_reason: SkipReason | None = None


def is_url(s: str) -> bool:
    return s.startswith(("http://", "https://"))


def _is_glob(s: str) -> bool:
    return any(c in s for c in _GLOB_CHARS)


def format_of(name: str) -> str:
    """Extension-derived format: lowercased, no leading dot, '' when there is none. Handles URLs
    (query/fragment stripped, path basename used)."""
    s = urlparse(name).path if is_url(name) else name
    ext = os.path.splitext(s)[1]
    return ext[1:].lower() if ext else ""


def normalize_input_format(entry: str) -> str:
    """A descriptor `input_formats` entry → its comparable format token: lowercased first word, so
    'pdf (rasterized)' → 'pdf', 'PNG' → 'png' (M3 normalization rule)."""
    entry = entry.strip()
    return entry.lower().split()[0] if entry else ""


def looks_batch(source_args: list[str]) -> bool:
    """M2: the envelope is decided by input FORM, not count. A directory, a glob pattern, or >=2
    arguments ⇒ batch; a single explicit file or URL ⇒ single-document."""
    if len(source_args) != 1:
        return len(source_args) >= 2
    arg = source_args[0]
    if is_url(arg):
        return False
    if _is_glob(arg):
        return True
    return Path(arg).is_dir()


# --- expansion (M1/M5) — raw (path|url, relpath, filename) tuples in resolution order ---------

_RawRef = tuple[str | None, str | None, str | None, str]  # (path, url, relpath, filename)


def _url_filename(url: str) -> str:
    return os.path.basename(urlparse(url).path) or "document"


def _expand_dir(root: Path) -> list[tuple[Path, str]]:
    """Recurse `root` (M1): (file, relpath) pairs sorted bytewise by relpath; hidden files/dirs and
    symlinks skipped; symlinked dirs not descended (loop safety)."""
    out: list[tuple[Path, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # prune hidden and symlinked subdirs in place (os.walk honors the mutation)
        dirnames[:] = [
            dn for dn in dirnames if not dn.startswith(".") and not Path(dirpath, dn).is_symlink()
        ]
        for fn in filenames:
            if fn.startswith("."):
                continue
            fp = Path(dirpath) / fn
            if fp.is_symlink():
                continue
            out.append((fp, fp.relative_to(root).as_posix()))
    out.sort(key=lambda t: t[1])
    return out


def _glob_root(pattern: str) -> Path:
    """The fixed directory a glob is anchored at: every leading component before the first one
    carrying a wildcard. `a/b/*/x.pdf` → `a/b`, `*.pdf` → `.`. It is what a match's `relpath` is
    measured from, so the directories the pattern itself wrote down do not reappear in every
    record while the ones it matched are kept."""
    parts = Path(pattern).parts
    fixed = list(itertools.takewhile(lambda part: not _is_glob(part), parts))
    return Path(*fixed) if fixed else Path(".")


def _expand_arg(arg: str) -> list[_RawRef]:
    if is_url(arg):
        return [(None, arg, None, _url_filename(arg))]
    if _is_glob(arg):
        # `recursive=True` is what gives `**` its meaning. Without it Python silently reads `**`
        # as a plain `*`, so a pattern over a nested corpus matches one level and the run reports
        # success over a fraction of the documents, with nothing on stderr to say so.
        matches = sorted(_glob.glob(arg, recursive=True))
        if not matches:
            # BL-141: errno-style OSError.__init__(errno, strerror, filename) construction — see
            # the identical note at the other SourceNotFoundError raise site below.
            # BL-143: `errno.ENOENT`, not `None` — see the identical note at the other raise site.
            raise SourceNotFoundError(errno.ENOENT, "glob matched no files", arg)
        # `relpath` is the cross-run pairing key (batch-result.v0.1.json) and the `--save-dir`
        # layout, so it has to stay unique per document. Measuring it from the pattern's fixed
        # root keeps the directories the wildcard walked; a bare basename would collapse
        # `x/invoice.pdf` and `y/invoice.pdf` into one record and one saved file.
        root = _glob_root(arg)
        raws: list[_RawRef] = []
        for m in matches:
            p = Path(m)
            if p.is_dir():
                raws += [
                    (str(fp), None, f"{p.relative_to(root).as_posix()}/{rel}", fp.name)
                    for fp, rel in _expand_dir(p)
                ]
            elif p.is_file():
                raws.append((str(p), None, p.relative_to(root).as_posix(), p.name))
        return raws
    p = Path(arg)
    if p.is_dir():
        return [(str(fp), None, rel, fp.name) for fp, rel in _expand_dir(p)]
    if p.is_file():
        # an explicitly named file (even a hidden one) is a deliberate choice → always included
        return [(str(p), None, p.name, p.name)]
    # BL-141: errno-style OSError.__init__(errno, strerror, filename) construction — populates
    # `.strerror` (just the reason, no path) the same way a real FileNotFoundError's is, so
    # cli/app.py's _describe_read_error helper treats this uniformly with no special case.
    # BL-143: `errno.ENOENT`, not `None` — a real errno so `str(e)` reads "[Errno 2] ..." like a
    # genuine FileNotFoundError instead of the literal "[Errno None] ..." the placeholder produced.
    raise SourceNotFoundError(errno.ENOENT, "no such file or directory", arg)


# --- M3 classification -------------------------------------------------------------------


_HASH_CHUNK_BYTES = 1024 * 1024  # 1 MB (internal/design/ledger.md §8's own streaming-hash number)


def _streaming_sha256(p: Path) -> str:
    """1 MB-chunked sha256 — bounds peak memory to one chunk regardless of file size, instead of
    loading the whole file into memory just to hash it (`hashlib.sha256(p.read_bytes())`'s prior
    shape). Output is byte-identical to the whole-file digest (L1: the hash itself never changes,
    only how it's computed).

    Known gap: the file is read once here and once more by `api._document_dict`, which needs the
    bytes themselves. Closing that second read means interleaving read-and-dispatch per item in
    `batch/runner.py`, which today resolves the whole list before dispatch begins. Holding every
    item's bytes at once instead would be a memory-scaling regression, so the second read stays."""
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK_BYTES), b""):
            h.update(chunk)
    return h.hexdigest()


def _skip_reason(fmt: str, supported: set[str] | None) -> SkipReason | None:
    if supported is None:
        return None
    if fmt in supported:
        return None
    return "unsupported_format" if fmt in _KNOWN_DOC_FORMATS else "unknown_format"


def resolve_intake(
    source_args: list[str],
    *,
    supported_formats: set[str] | None = None,
    max_items: int = DEFAULT_MAX_ITEMS,
) -> list[ResolvedSource]:
    """Resolve `source_args` into ordered `ResolvedSource` records (M1–M5). `supported_formats` (a
    set of normalized format tokens, e.g. {'pdf','png'}) drives the M3 filter; None disables it.
    Raises SourceLimitError past `max_items` and SourceNotFoundError for a missing arg."""
    raws: list[_RawRef] = []
    for arg in source_args:
        raws += _expand_arg(arg)

    if len(raws) > max_items:  # M4 — hard early guard, BEFORE any bytes are read
        raise SourceLimitError(
            f"batch expansion is {len(raws)} files, over the max-items limit ({max_items}); "
            f"raise it with --max-items / max_items= if this is intended"
        )

    out: list[ResolvedSource] = []
    for path, url, relpath, filename in raws:
        if url is not None:
            fmt = format_of(url)
            ref = SourceRef(filename=filename, format=fmt, url=url)
            out.append(ResolvedSource(ref=ref, skip_reason=_skip_reason(fmt, supported_formats)))
            continue
        assert path is not None
        p = Path(path)
        fmt = format_of(filename)
        skip = _skip_reason(fmt, supported_formats)
        sha256 = None
        if skip is None:  # only hash files we will actually process
            sha256 = _streaming_sha256(p)
        ref = SourceRef(
            filename=filename,
            format=fmt,
            path=path,
            relpath=relpath,
            size_bytes=p.stat().st_size,
            sha256=sha256,
        )
        out.append(ResolvedSource(ref=ref, skip_reason=skip))
    return out
