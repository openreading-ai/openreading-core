"""BL-170: the shared real-tesseract skip-guard must degrade to "unavailable" on any failure —
never raise, never let a broken binary turn a `skip` into a red test."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from openreading.testing import tesseract_probe


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    tesseract_probe.tesseract_ocr_works.cache_clear()
    yield
    tesseract_probe.tesseract_ocr_works.cache_clear()


def test_probe_true_on_a_working_binary():
    pytest.importorskip("pytesseract")
    pytest.importorskip("PIL")
    result = tesseract_probe.tesseract_ocr_works()
    if not result:
        pytest.skip("no working system tesseract on this machine")
    assert result is True


def test_probe_false_when_binary_present_but_errors(monkeypatch):
    pytesseract = pytest.importorskip("pytesseract")
    pytest.importorskip("PIL")

    def _boom(*_a, **_kw):
        raise RuntimeError("tesseract failed: 'utf-8' codec can't decode byte 0x89")

    monkeypatch.setattr(pytesseract, "image_to_data", _boom)
    assert tesseract_probe.tesseract_ocr_works() is False


def test_probe_false_when_fixture_setup_raises(monkeypatch):
    # BL-170 review: the fixture-image/font setup must be inside the same guard as the
    # OCR call itself — an old Pillow without a size-aware `ImageFont.load_default` must degrade
    # to "unavailable", not escape the probe's own never-raises contract.
    pytest.importorskip("pytesseract")
    image_font = pytest.importorskip("PIL.ImageFont")

    def _boom(*_a, **_kw):
        raise TypeError("load_default() got an unexpected keyword argument 'size'")

    monkeypatch.setattr(image_font, "load_default", _boom)
    assert tesseract_probe.tesseract_ocr_works() is False


def test_probe_false_on_empty_output(monkeypatch):
    pytesseract = pytest.importorskip("pytesseract")
    pytest.importorskip("PIL")

    monkeypatch.setattr(pytesseract, "image_to_data", lambda *_a, **_kw: {"text": ["", "  ", "\n"]})
    assert tesseract_probe.tesseract_ocr_works() is False


def test_probe_false_without_pytesseract_or_pil(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *a, **kw):
        if name in ("pytesseract", "PIL"):
            raise ImportError(name)
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    assert tesseract_probe.tesseract_ocr_works() is False


def test_broken_binary_on_path_yields_skip_not_fail(tmp_path):
    """BL-170 review : the tests above only assert the probe FUNCTION's return value —
    none proves the actual `pytest.mark.skipif`-gated real-tesseract tests report SKIPPED rather
    than FAILED/ERROR when a present-but-broken `tesseract` sits on PATH, which is the worklist's
    own named acceptance criterion (internal/runs/ledger-defect-worklist.md: "A test asserts the guard
    skips rather than fails when the binary is present but errors"). Runs the gated tests in a
    real subprocess with a fake `tesseract` on PATH that `which` finds but that exits non-zero on
    every invocation — one of the two "present but broken" symptoms the worklist's bug reports
    describe (the other, an exit-0-with-undecodable-output failure, is exercised at the unit level
    by test_probe_false_when_binary_present_but_errors above; the probe has a single unconditional
    except path with no branching by exception type, so both symptoms take the same code path)."""
    pytest.importorskip("pytesseract")
    pytest.importorskip("PIL")

    fake_tesseract = tmp_path / "tesseract"
    fake_tesseract.write_text("#!/bin/sh\nexit 1\n")
    fake_tesseract.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}{os.pathsep}{env.get('PATH', '')}"

    repo_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-v",
            "tests/test_tesseract.py::test_real_tesseract_recovers_title_text",
            "tests/test_tesseract.py::test_real_tesseract_conforms",
            "tests/test_compare_corpus.py::test_live_pymupdf_vs_tesseract_structural_invariants",
        ],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 skipped" in result.stdout, result.stdout
    assert "FAILED" not in result.stdout
    assert "ERROR" not in result.stdout
