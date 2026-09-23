"""Persist bounded selection receipts under the current grant for continuation after restart.

Each immutable page holds at most sixteen references and 60 KiB of serialized JSON.
Cursors bind the random selection identifier, page index and exact page digest. They grant
no access outside the service's configured input grant. Only trusted provider copies become
references. Folder paths and original hierarchy never enter the retained selection receipt.
A failed or cancelled handoff deletes its receipt pages before provider copy rollback.
Successful selections persist until their private selection directory is explicitly removed.
Publication uses a private random directory; callers receive its cursor only after validation.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import uuid
from contextlib import suppress
from pathlib import PurePosixPath

from openreading.artifacts.intake import directory
from openreading.artifacts.models import json_bytes
from openreading.artifacts.retained import RetainedService as ArtifactService
from openreading.artifacts.store import safe_read
from openreading.types.selection import SelectionBatch, SelectionPage, SelectionReceipt

_CURSOR = re.compile(r"(s1_[0-9a-f]{32}):([0-9]{1,16}):([0-9a-f]{64})")


class SelectionPages:
    def __init__(self, service: ArtifactService):
        self.service = service
        self.root = service.config.artifact_root / "selections" / service.store.grant
        self.identifier = "s1_" + uuid.uuid4().hex

    def publish(self, batch: SelectionBatch, *, cancelled=lambda: False) -> SelectionPage:
        def check():
            if cancelled():
                raise ValueError("Selection cancelled.")

        check()
        if not isinstance(batch.references, (tuple, list)):
            raise ValueError("Invalid selected references.")
        receipts = []
        seen = set()
        for reference in batch.references:
            check()
            if not isinstance(reference, str) or reference in seen:
                raise ValueError("Invalid selected reference.")
            seen.add(reference)
            with self.service.store.source(reference) as opened:
                size = os.fstat(opened).st_size
            cap = self.service.config.limits.source_bytes
            if size <= 0 or (cap is not None and size > cap):
                raise ValueError("Invalid selected size.")
            receipts.append(
                SelectionReceipt(
                    path=reference, display_name=PurePosixPath(reference).name, source_bytes=size
                )
            )
        common = dict(
            selection_id=self.identifier,
            total_files=len(receipts),
            total_bytes=sum(r.source_bytes for r in receipts),
            skipped=batch.skipped,
        )
        if any(type(n) is not int or n < 0 for n in batch.skipped.values()):
            raise ValueError("Invalid skipped count.")
        pages: list[list[SelectionReceipt]] = [[]]
        size = 0
        for receipt in receipts:
            length = len(json_bytes(receipt.wire())) + 1
            if pages[-1] and (len(pages[-1]) == 16 or size + length > 48 * 1024):
                pages.append([])
                size = 0
            pages[-1].append(receipt)
            size += length
        with directory(self.root, create=True) as parent:
            os.mkdir(self.identifier, 0o700, dir_fd=parent)
        cursor = None
        with directory(self.root / self.identifier) as parent:
            for index in reversed(range(len(pages))):
                check()
                result = SelectionPage.model_validate(
                    {"items": pages[index], "next_cursor": cursor, **common}
                )
                data = json_bytes(result.wire())
                if len(data) > 60 * 1024:
                    raise ValueError("Selection receipt exceeds its response budget.")
                fd = os.open(
                    str(index) + ".json",
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent,
                )
                with os.fdopen(fd, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                cursor = f"{self.identifier}:{index}:{hashlib.sha256(data).hexdigest()}"
            os.fsync(parent)
        return result

    def rollback(self):
        with directory(self.root, create=True) as parent, suppress(FileNotFoundError):
            shutil.rmtree(self.identifier, dir_fd=parent)

    def read(self, cursor: str) -> SelectionPage:
        match = _CURSOR.fullmatch(cursor)
        if match is None:
            raise ValueError("Invalid selection cursor.")
        identifier, index, digest = match.groups()
        data = safe_read(self.root / identifier / (index + ".json"), 60 * 1024)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Changed selection receipt.")
        result = SelectionPage.model_validate_json(data)
        if result.selection_id != identifier:
            raise ValueError("Invalid selection binding.")
        return result
