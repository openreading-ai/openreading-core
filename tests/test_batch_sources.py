"""Phase A (Manifest v0.6) — intake resolution (batch/sources.py), invariants M1–M5.

Pure (no network beyond stat/read): expands files/dirs/globs/URLs into an ordered list of
`ResolvedSource` records with honest per-file skip reasons. Deterministic order; hidden/symlink
skips; format filter; the max-items guard."""

from __future__ import annotations

import pytest

from openreading.batch.sources import (
    ResolvedSource,
    SourceLimitError,
    SourceNotFoundError,
    format_of,
    is_url,
    looks_batch,
    normalize_input_format,
    resolve_intake,
)


def _mk(p, data: bytes = b"%PDF-1.4 x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


# --- format helpers ---------------------------------------------------------------------


def test_format_of_extension_lowercased():
    assert format_of("A.PDF") == "pdf"
    assert format_of("scan.PNG") == "png"
    assert format_of("no_ext") == ""
    assert format_of("https://x.test/doc.PDF?q=1") == "pdf"  # URL path extension


def test_normalize_input_format_takes_first_token():
    # descriptor entries like "pdf (rasterized)" / "png" — match on the first token, lowercased
    assert normalize_input_format("pdf (rasterized)") == "pdf"
    assert normalize_input_format("PNG") == "png"
    assert normalize_input_format("") == ""


def test_is_url():
    assert is_url("https://x.test/a.pdf") and is_url("http://x/a")
    assert not is_url("/local/a.pdf") and not is_url("a.pdf")


# --- M2: envelope decided by input form -------------------------------------------------


def test_looks_batch(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    f = _mk(tmp_path / "a.pdf")
    assert looks_batch([str(d)]) is True  # a directory → batch
    assert looks_batch([str(tmp_path / "*.pdf")]) is True  # a glob → batch
    assert looks_batch([str(f), str(f)]) is True  # >=2 args → batch
    assert looks_batch([str(f)]) is False  # single explicit file → single
    assert looks_batch(["https://x.test/a.pdf"]) is False  # single URL → single


# --- M1: deterministic recursive expansion ----------------------------------------------


def test_directory_expands_recursively_sorted_by_relpath(tmp_path):
    d = tmp_path / "corpus"
    # create in non-sorted order to prove the output is sorted, not creation-ordered
    _mk(d / "sub" / "z.pdf")
    _mk(d / "a.pdf")
    _mk(d / "sub" / "a.pdf")
    _mk(d / "m.pdf")
    got = resolve_intake([str(d)])
    assert [r.ref.relpath for r in got] == ["a.pdf", "m.pdf", "sub/a.pdf", "sub/z.pdf"]
    # relpath is directory-relative (the cross-run pairing key); filename is the basename
    assert got[2].ref.filename == "a.pdf" and got[2].ref.relpath == "sub/a.pdf"


def test_hidden_files_and_dirs_skipped_in_expansion(tmp_path):
    d = tmp_path / "c"
    _mk(d / "keep.pdf")
    _mk(d / ".hidden.pdf")
    _mk(d / ".git" / "config.pdf")
    got = [r.ref.relpath for r in resolve_intake([str(d)])]
    assert got == ["keep.pdf"]


def test_symlinks_not_followed(tmp_path):
    import os

    d = tmp_path / "c"
    _mk(d / "real.pdf")
    target = _mk(tmp_path / "outside.pdf")
    os.symlink(target, d / "link.pdf")  # a symlinked file inside the dir
    got = [r.ref.relpath for r in resolve_intake([str(d)])]
    assert got == ["real.pdf"]  # the symlink is not followed


@pytest.mark.parametrize("pattern", ["*", "**/*.pdf", "*/*.pdf"])
def test_globs_do_not_ingest_symlink_targets(tmp_path, pattern):
    corpus = tmp_path / "corpus"
    _mk(corpus / "kept" / "real.pdf")
    outside = _mk(tmp_path / "private" / "secret.pdf")
    (corpus / "file.pdf").symlink_to(outside)
    (corpus / "linked").symlink_to(outside.parent, target_is_directory=True)

    got = resolve_intake([str(corpus / pattern)])

    assert [item.ref.relpath for item in got] == ["kept/real.pdf"]


def test_explicit_hidden_file_arg_is_processed(tmp_path):
    # hidden-skip applies to DIRECTORY expansion, not to an explicitly named file
    f = _mk(tmp_path / ".secret.pdf")
    got = resolve_intake([str(f)])
    assert len(got) == 1 and got[0].ref.filename == ".secret.pdf"


# --- M5: mixed sources ------------------------------------------------------------------


def test_mixed_file_dir_url_preserves_arg_order(tmp_path):
    f = _mk(tmp_path / "first.pdf")
    d = tmp_path / "mid"
    _mk(d / "b.pdf")
    _mk(d / "a.pdf")
    url = "https://x.test/last.pdf"
    got = resolve_intake([str(f), str(d), url])
    # arg order preserved; dir contents sorted within their slot; url passes through
    assert [r.ref.filename for r in got] == ["first.pdf", "a.pdf", "b.pdf", "last.pdf"]
    last = got[-1]
    assert last.ref.url == url and last.ref.path is None
    assert last.ref.sha256 is None and last.ref.size_bytes is None  # not read at intake
    assert last.ref.format == "pdf"


def test_local_files_get_size_and_sha256(tmp_path):
    f = _mk(tmp_path / "x.pdf", b"hello bytes")
    (ref,) = [r.ref for r in resolve_intake([str(f)])]
    assert ref.size_bytes == len(b"hello bytes")
    import hashlib

    assert ref.sha256 == hashlib.sha256(b"hello bytes").hexdigest()


# --- M3: honest format filter -----------------------------------------------------------


def test_format_filter_marks_unsupported_and_unknown(tmp_path):
    d = tmp_path / "c"
    _mk(d / "good.pdf")
    _mk(d / "img.png")
    _mk(d / "doc.docx")
    _mk(d / "weird.xyzzy")
    got = {
        r.ref.filename: r.skip_reason
        for r in resolve_intake([str(d)], supported_formats={"pdf", "png"})
    }
    assert got["good.pdf"] is None and got["img.png"] is None
    assert got["doc.docx"] == "unsupported_format"  # known extension, backend can't take it
    assert got["weird.xyzzy"] == "unknown_format"  # not a recognized document extension


def test_no_supported_set_means_no_filtering(tmp_path):
    f = _mk(tmp_path / "x.docx")
    (r,) = resolve_intake([str(f)], supported_formats=None)
    assert r.skip_reason is None  # nothing filtered when the caller gives no format set


def test_streaming_sha256_matches_whole_file_hash_across_a_chunk_boundary(tmp_path):
    # Ledger T1 (§8): the sha256 line moved from a whole-file read to a 1 MB-chunked streaming
    # read — content spanning multiple chunks is the one case that could silently diverge from
    # the whole-file digest if the chunking were buggy (an off-by-one at a chunk boundary, e.g.).
    import hashlib

    data = bytes((i % 251) for i in range(2 * 1024 * 1024 + 137))  # > 2 chunks, not a round size
    f = _mk(tmp_path / "big.pdf", data)
    (ref,) = [r.ref for r in resolve_intake([str(f)])]
    assert ref.sha256 == hashlib.sha256(data).hexdigest()


def test_skipped_files_are_not_hashed(tmp_path):
    f = _mk(tmp_path / "x.docx")
    (r,) = resolve_intake([str(f)], supported_formats={"pdf"})
    assert r.skip_reason == "unsupported_format"
    assert r.ref.sha256 is None  # no point hashing a file we won't process


# --- M4: size guard ---------------------------------------------------------------------


def test_max_items_guard(tmp_path):
    d = tmp_path / "big"
    for i in range(5):
        _mk(d / f"f{i}.pdf")
    with pytest.raises(SourceLimitError) as exc:
        resolve_intake([str(d)], max_items=3)
    assert "3" in str(exc.value) and "max" in str(exc.value).lower()


# --- errors -----------------------------------------------------------------------------


def test_missing_path_raises(tmp_path):
    with pytest.raises(SourceNotFoundError):
        resolve_intake([str(tmp_path / "does_not_exist.pdf")])


def test_glob_expands_sorted(tmp_path):
    _mk(tmp_path / "b.png")
    _mk(tmp_path / "a.png")
    _mk(tmp_path / "c.pdf")
    got = [r.ref.filename for r in resolve_intake([str(tmp_path / "*.png")])]
    assert got == ["a.png", "b.png"]  # glob matches sorted; the .pdf is not matched


def test_glob_matching_a_directory_recurses_into_it(tmp_path):
    _mk(tmp_path / "d1" / "b.pdf")
    _mk(tmp_path / "d1" / "a.pdf")
    _mk(tmp_path / "d2" / "c.pdf")
    got = [r.ref.relpath for r in resolve_intake([str(tmp_path / "d*")])]
    # Each matched directory is expanded recursively and sorted, and its own name stays in the
    # relpath: the wildcard walked those directories, so they are part of a document's identity.
    assert got == ["d1/a.pdf", "d1/b.pdf", "d2/c.pdf"]


def test_glob_with_no_matches_raises(tmp_path):
    with pytest.raises(SourceNotFoundError):
        resolve_intake([str(tmp_path / "*.nope")])


def test_resolved_source_is_the_dataclass():
    assert ResolvedSource.__annotations__.keys() >= {"ref", "skip_reason"}


# --- glob depth and identity (BL-cli-help) ------------------------------------------------


def test_recursive_glob_matches_every_depth(tmp_path):
    # `**` is the pattern a reader reaches for over a nested corpus. Without `recursive=True`
    # Python treats it as a plain `*`, so it matches one level and drops the rest at exit 0.
    _mk(tmp_path / "top.pdf")
    _mk(tmp_path / "a" / "one.pdf")
    _mk(tmp_path / "a" / "b" / "two.pdf")
    _mk(tmp_path / "a" / "b" / "c" / "three.pdf")
    got = [r.ref.relpath for r in resolve_intake([str(tmp_path / "**" / "*.pdf")])]
    assert got == ["a/b/c/three.pdf", "a/b/two.pdf", "a/one.pdf", "top.pdf"]


@pytest.mark.parametrize("pattern", ["**", "**/*", "**/**/*.pdf"])
def test_recursive_glob_selects_each_file_once(tmp_path, pattern):
    _mk(tmp_path / "top.pdf")
    _mk(tmp_path / "a" / "one.pdf")
    _mk(tmp_path / "a" / "b" / "two.pdf")
    got = resolve_intake([str(tmp_path / pattern)], max_items=3)
    assert [r.ref.relpath for r in got] == ["a/b/two.pdf", "a/one.pdf", "top.pdf"]
    assert len({r.ref.path for r in got}) == 3


def test_separate_source_arguments_preserve_deliberate_repeats(tmp_path):
    source = _mk(tmp_path / "doc.pdf")
    got = resolve_intake([str(source), str(tmp_path / "**")])
    assert [r.ref.path for r in got] == [str(source), str(source)]
    assert [r.ref.relpath for r in got] == ["doc.pdf", "doc.pdf"]


def test_glob_relpath_keeps_the_directories_below_the_pattern(tmp_path):
    # relpath is the cross-run pairing key (batch-result.v0.1.json) and the --save-dir layout.
    # Two same-named files under different parents must stay two distinct records.
    _mk(tmp_path / "coll" / "x" / "invoice.pdf")
    _mk(tmp_path / "coll" / "y" / "invoice.pdf")
    got = [r.ref.relpath for r in resolve_intake([str(tmp_path / "coll" / "*" / "invoice.pdf")])]
    assert got == ["x/invoice.pdf", "y/invoice.pdf"]


def test_glob_matching_directories_keeps_each_match_distinct(tmp_path):
    # A glob that matches directories expands each one, and the matched directory's own name
    # stays in the relpath, so two dirs holding the same filename do not collapse.
    _mk(tmp_path / "d1" / "same.pdf")
    _mk(tmp_path / "d2" / "same.pdf")
    got = [r.ref.relpath for r in resolve_intake([str(tmp_path / "d*")])]
    assert got == ["d1/same.pdf", "d2/same.pdf"]
