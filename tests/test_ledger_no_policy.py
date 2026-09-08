"""The ledger keeps no policy about the caller's own disk.

Three things left core in this change, all for one reason: a directory on the operator's own
machine is theirs.

**Retention.** A destructor whose only job was deleting the caller's data on a timer, defaulting
to a number its own source marked `# placeholder`. Its ceiling came from `min(max_retention_hours)`
across hosted descriptors, so an unverifiable claim about a vendor's servers decided when files on
this disk were shredded.

**Encryption at rest.** `keys/<run_id>.key` sat in the same directory tree as the ciphertext it
protected, so anyone who could read one could read the other. It bought one narrow scenario, a
backup that excludes `keys/`, at the cost of a native dependency on every install.

**The `zdr` storage branch.** A vendor compliance flag deciding what core wrote to the caller's
disk.

What stays is the ledger's actual job: the journal, the header, the blobs, replay and resume.
"""

from __future__ import annotations

import re

import pytest

from openreading import api


@pytest.fixture
def armed(tmp_path, monkeypatch):
    root = tmp_path / "ledger"
    monkeypatch.setenv("OPENREADING_LEDGER", str(root))
    return root


def _run(root):
    api.run("examples/schedule_a_2024.pdf", strategy="offline_first")
    return root


def test_an_armed_run_writes_no_retention_and_no_keys(armed):
    """The two directories that existed only to expire and to decrypt."""
    root = _run(armed)
    assert root.exists(), "the ledger still writes"
    assert not (root / "retention").exists()
    assert not (root / "keys").exists()


def test_a_blob_is_readable_without_a_key(armed):
    """The plainest possible statement of what the ledger stores. Reading a blob with `open()`
    and finding the document is what makes the disclosure below honest: `OPENREADING_LEDGER`
    means "copy every document and every response into this directory"."""
    root = _run(armed)
    blobs = sorted(root.glob("blobs/*/*.bin"))
    assert blobs, "blobs still written"
    payloads = [b.read_bytes() for b in blobs]
    assert any(p.startswith(b"%PDF") for p in payloads), "the input document, in the clear"
    assert any(p.lstrip().startswith(b"{") for p in payloads), "the response body, in the clear"


def test_the_journal_and_header_still_work(armed):
    root = _run(armed)
    assert list(root.glob("*.jsonl")), "the journal"
    assert list(root.glob("*.header.json")), "the run header"


def test_a_run_still_resumes(armed):
    """Replay and resume are the ledger's job and survive untouched."""
    import openreading

    root = _run(armed)
    run_id = next(root.glob("*.jsonl")).stem
    out = openreading.resume(run_id)
    assert out["status"]["state"] == "succeeded"


def test_retention_and_its_environment_variables_are_gone():
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("openreading.ledger.retention")

    from openreading.types import errors

    assert not hasattr(errors, "PayloadExpired")


def test_cryptography_is_no_longer_a_dependency():
    """Imported by exactly one file for one narrow scenario, and a native dependency on every
    install. Disk encryption is the operator's own, and their OS does it better."""
    import pathlib

    src = pathlib.Path("src/openreading")
    # An IMPORT, not the word: `localfs.py`'s docstring names the dependency to explain why it
    # went, and a prose mention is not a dependency.
    importing = [
        p
        for p in src.rglob("*.py")
        if re.search(r"^\s*(from|import)\s+cryptography", p.read_text(), re.M)
    ]
    assert not importing, f"cryptography still imported by {importing}"
    deps = pathlib.Path("pyproject.toml").read_text()
    assert '"cryptography' not in deps, "still a declared dependency"
