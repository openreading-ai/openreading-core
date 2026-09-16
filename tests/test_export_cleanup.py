"""Export recovery removes abandoned writes without touching active or completed files."""

import multiprocessing
import os
import stat
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from openreading.artifacts.delivery import save_export


def paused_export(root, ready, release, checkpoint):
    import openreading.artifacts.delivery as module

    original_sync, original_open = os.fsync, os.open

    def wait():
        ready.touch()
        until = time.monotonic() + 20
        while not release.exists():
            if time.monotonic() > until:
                raise RuntimeError("Test did not release the writer")
            time.sleep(0.01)

    def pause_sync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            wait()
        original_sync(fd)

    def pause_open(path, *args, **kwargs):
        fd = original_open(path, *args, **kwargs)
        if str(path).startswith(".openreading-export-"):
            wait()
        return fd

    if checkpoint == "before_lock":
        module.os.open = pause_open
    else:
        module.os.fsync = pause_sync
    save_export(root, "grant", b"first writer")


def start_paused(root, checkpoint="before_publish"):
    context = multiprocessing.get_context("fork")
    ready, release = root.parent / "writer-ready", root.parent / "writer-release"
    child = context.Process(target=paused_export, args=(root, ready, release, checkpoint))
    child.start()
    until = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < until:
        time.sleep(0.01)
    if not ready.exists():
        child.kill()
        child.join(5)
        pytest.fail("Writer never reached the pre-publication checkpoint")
    return child, release


def test_export_recovers_crashed_write_without_removing_completed_content(tmp_path):
    root = tmp_path.resolve() / "exports"
    complete = save_export(root, "grant", b"keep completed content")
    child, release = start_paused(root)
    try:
        pending = list((root / "grant").glob("*.tmp"))
        assert len(pending) == 1
        child.kill()
        child.join(5)
        assert not child.is_alive()
        replacement = save_export(root, "grant", b"next export")
        assert replacement.read_bytes() == b"next export"
        assert complete.read_bytes() == b"keep completed content"
        assert not pending[0].exists()
    finally:
        release.touch()
        if child.is_alive():
            child.kill()
        child.join(5)


def test_cleanup_does_not_remove_or_wait_for_an_active_export(tmp_path):
    root = tmp_path.resolve() / "exports"
    child, release = start_paused(root)
    try:
        pending = list((root / "grant").glob("*.tmp"))
        assert len(pending) == 1
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(save_export, root, "grant", b"second writer")
            try:
                path = future.result(timeout=3)
                assert path.read_bytes() == b"second writer"
                assert pending[0].exists()
            finally:
                release.touch()
        child.join(5)
        assert child.exitcode == 0
        assert not pending[0].exists()
        assert {p.read_bytes() for p in (root / "grant").glob("*.json")} == {
            b"first writer",
            b"second writer",
        }
    finally:
        release.touch()
        if child.is_alive():
            child.kill()
        child.join(5)


def test_cleanup_only_touches_its_reserved_regular_files(tmp_path):
    root = tmp_path.resolve() / "exports"
    directory = root / "grant"
    directory.mkdir(parents=True)
    old = directory / ("." + "a" * 32 + ".tmp")
    old.write_bytes(b"legacy writer has no lock protocol")
    unrelated = directory / ".notes.tmp"
    unrelated.write_bytes(b"user content")
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    link = directory / (".openreading-export-" + "b" * 32 + ".tmp")
    link.symlink_to(outside)
    folder = directory / (".openreading-export-" + "c" * 32 + ".tmp")
    folder.mkdir()
    abandoned = directory / (".openreading-export-" + "d" * 32 + ".tmp")
    abandoned.write_bytes(b"abandoned")
    save_export(root, "grant", b"published")
    assert old.read_bytes() == b"legacy writer has no lock protocol"
    assert unrelated.read_bytes() == b"user content"
    assert link.is_symlink() and outside.read_bytes() == b"keep"
    assert folder.is_dir()
    assert not abandoned.exists()


def test_new_temporary_file_cannot_be_swept_before_writer_locks_it(tmp_path):
    root = tmp_path.resolve() / "exports"
    child, release = start_paused(root, "before_lock")
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(save_export, root, "grant", b"second writer")
            try:
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.2)
            finally:
                release.touch()
            path = future.result(timeout=5)
            assert path.read_bytes() == b"second writer"
        child.join(5)
        assert child.exitcode == 0
        assert {p.read_bytes() for p in (root / "grant").glob("*.json")} == {
            b"first writer",
            b"second writer",
        }
    finally:
        release.touch()
        if child.is_alive():
            child.kill()
        child.join(5)
