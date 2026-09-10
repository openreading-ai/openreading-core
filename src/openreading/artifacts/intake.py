"""Open granted files through directory descriptors, refusing every symlink component.

The source descriptor stays open while bytes are copied and hashed into private staging.
This avoids the check-then-reopen race of resolving a pathname before parser dispatch.
Concurrent source edits produce a warning when descriptor metadata changes during copying.
"""

from __future__ import annotations

import hashlib
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from openreading.artifacts.limits import ArtifactError

__all__ = ["directory"]


def components(relative: str) -> list[str]:
    if not relative or len(relative) > 1024 or "\x00" in relative or relative.startswith("/"):
        raise ArtifactError("access_denied")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ArtifactError("access_denied")
    return parts


@contextmanager
def directory(path: Path, *, create: bool = False) -> Iterator[int]:
    if not path.is_absolute() or ".." in path.parts:
        raise ArtifactError("configuration_required")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, 0o700, dir_fd=fd)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    except OSError:
        raise ArtifactError("configuration_required") from None
    finally:
        os.close(fd)


@contextmanager
def source(root: Path, relative: str) -> Iterator[int]:
    parts = components(relative)
    with directory(root) as root_fd:
        fd = os.dup(root_fd)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            child = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            os.close(fd)
            fd = child
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ArtifactError("access_denied")
            yield fd
        except FileNotFoundError:
            raise ArtifactError("input_not_found") from None
        except OSError:
            raise ArtifactError("access_denied") from None
        finally:
            os.close(fd)


def copy_source(
    fd: int, destination: Path, cap: int, available: int, *, check: Callable[[], None] | None = None
) -> tuple[str, bool]:
    before = os.fstat(fd)
    if before.st_size > cap:
        raise ArtifactError("input_too_large")
    digest = hashlib.sha256()
    length = 0
    with destination.open("xb") as output:
        os.chmod(destination, 0o600)
        while True:
            if check is not None:
                check()
            data = os.read(fd, min(65536, cap - length + 1))
            if not data:
                break
            length += len(data)
            if length > cap:
                raise ArtifactError("input_too_large")
            if length > available:
                raise ArtifactError("storage_limit")
            output.write(data)
            digest.update(data)
        output.flush()
        os.fsync(output.fileno())
    after = os.fstat(fd)
    changed = (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    return digest.hexdigest(), changed
