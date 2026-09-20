"""Input grants follow opened directory identity, not aliases or replaced path strings."""

import os

import pytest

from openreading.artifacts.limits import ArtifactError, ProfileConfig
from openreading.artifacts.store import Store


def test_selected_symlink_alias_has_one_canonical_grant(tmp_path):
    root = tmp_path / "input"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    first = Store(ProfileConfig(root, tmp_path / "store"))
    second = Store(ProfileConfig(alias, tmp_path / "store"))
    assert first.grant == second.grant


def test_replacing_input_directory_changes_grant_and_refuses_old_handle(tmp_path):
    root = tmp_path / "input"
    root.mkdir()
    (root / "file.pdf").write_bytes(b"original")
    first = Store(ProfileConfig(root, tmp_path / "store"))
    root.rename(tmp_path / "old")
    root.mkdir()
    (root / "file.pdf").write_bytes(b"replacement")
    second = Store(ProfileConfig(root, tmp_path / "store"))
    assert first.grant != second.grant
    with pytest.raises(ArtifactError, match="access_denied"), first.source("file.pdf"):
        pass
    with second.source("file.pdf") as fd:
        assert os.read(fd, 20) == b"replacement"
        assert not os.get_inheritable(fd)


def test_alias_cannot_hide_overlapping_artifact_directory(tmp_path):
    root = tmp_path / "input"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ArtifactError, match="configuration_required"):
        Store(ProfileConfig(alias, root / "store"))


def test_unicode_root_aliases_follow_filesystem_identity(tmp_path):
    import unicodedata

    from openreading.artifacts.intake import InputGrant

    root = tmp_path / "Dócuments"
    root.mkdir()
    alternate = tmp_path / unicodedata.normalize("NFD", root.name)
    if not alternate.exists():
        alternate.mkdir()
    first, second = InputGrant(root), InputGrant(alternate)
    try:
        assert (first.identity == second.identity) == root.samefile(alternate)
        if root.samefile(alternate):
            assert first.path == second.path
    finally:
        first.close()
        second.close()


def test_permission_errors_are_distinct_from_traversal(tmp_path, monkeypatch):
    import pytest

    from openreading.artifacts import intake
    from openreading.artifacts.limits import ArtifactError

    root = intake.InputGrant(tmp_path)
    original = intake.os.open

    def denied(path, *args, **kwargs):
        if path == "private.pdf":
            raise PermissionError("secret path")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(intake.os, "open", denied)
    try:
        with (
            pytest.raises(ArtifactError, match="os_permission_denied"),
            root.source("private.pdf"),
        ):
            pass
    finally:
        root.close()
