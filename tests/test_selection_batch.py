"""Multiple selections retain bounded, grant-scoped receipts without broadening file access."""

from contextlib import asynccontextmanager

import pytest

from openreading.artifacts.limits import ProfileConfig
from openreading.artifacts.service import ArtifactService
from openreading.mcp_server.selection import SelectionCoordinator
from openreading.types import selection as models


class Provider:
    def __init__(self, value):
        self.value = value
        self.calls = 0
        self.rollback = False

    @asynccontextmanager
    async def select(self):
        self.calls += 1
        try:
            yield self.value
        except BaseException:
            self.rollback = True
            raise


@pytest.fixture
def service(tmp_path):
    root = tmp_path.resolve() / "input"
    root.mkdir()
    result = ArtifactService(ProfileConfig(root, tmp_path.resolve() / "artifacts"))
    yield result
    result.close()


@pytest.mark.asyncio
async def test_batch_pages_resume_after_restart_without_reopening_chooser(service):
    references = []
    for index in range(101):
        name = f"{index:03d}-" + "Résumé" * 20 + ".pdf"
        (service.config.input_root / name).write_bytes(b"%PDF-test")
        references.append(name)
    provider = Provider(models.SelectionBatch(tuple(references), {"hidden": 2}))
    coordinator = SelectionCoordinator(service, provider, None)
    result = await coordinator.select()
    assert result.schema_version == "0.2"
    assert result.total_files == 101 and result.total_bytes == 909
    assert result.skipped == {"hidden": 2}
    items = list(result.items)
    assert result.next_cursor is not None
    while result.next_cursor:
        coordinator = SelectionCoordinator(service, None, None)
        result = await coordinator.select(cursor=result.next_cursor)
        assert len(str(result.wire()).encode()) < 65536
        items.extend(result.items)
    assert [i.path for i in items] == references
    assert provider.calls == 1 and not provider.rollback


@pytest.mark.asyncio
async def test_invalid_batch_rolls_back_without_returning_partial_references(service):
    (service.config.input_root / "ok.pdf").write_bytes(b"%PDF-test")
    provider = Provider(models.SelectionBatch(("ok.pdf", "../secret.pdf"), {}))
    result = await SelectionCoordinator(service, provider, None).select()
    assert result.error.code == "selection_failed" and provider.rollback
    assert "secret" not in str(result.wire())


@pytest.mark.asyncio
async def test_empty_batch_discloses_skips_without_importing(service):
    provider = Provider(models.SelectionBatch((), {"unsupported": 3}))
    result = await SelectionCoordinator(service, provider, None).select()
    assert result.items == [] and result.total_files == 0
    assert result.skipped == {"unsupported": 3} and result.next_cursor is None


@pytest.mark.asyncio
async def test_cursor_is_hash_bound_and_grant_scoped(service, tmp_path):
    for index in range(18):
        (service.config.input_root / f"{index}.pdf").write_bytes(b"%PDF-test")
    provider = Provider(models.SelectionBatch(tuple(f"{i}.pdf" for i in range(18)), {}))
    first = await SelectionCoordinator(service, provider, None).select()
    cursor = first.next_cursor
    other_root = tmp_path.resolve() / "other"
    other_root.mkdir()
    other = ArtifactService(ProfileConfig(other_root, service.config.artifact_root))
    try:
        result = await SelectionCoordinator(other, None, None).select(cursor=cursor)
        assert result.error.code == "selection_failed"
    finally:
        other.close()
    identifier, index, _ = cursor.split(":")
    page = (
        service.config.artifact_root
        / "selections"
        / service.store.grant
        / identifier
        / (index + ".json")
    )
    page.write_bytes(page.read_bytes() + b" ")
    result = await SelectionCoordinator(service, None, None).select(cursor=cursor)
    assert result.error.code == "selection_failed"
    assert provider.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "references,skipped",
    [
        (("a.pdf", "a.pdf"), {}),
        (("empty.pdf",), {}),
        ((), {"hidden": -1}),
        ((), {"bad": 1}),
        ((), {"hidden": True}),
    ],
)
async def test_malformed_batch_is_refused_and_rolled_back(service, references, skipped):
    (service.config.input_root / "a.pdf").write_bytes(b"%PDF-test")
    (service.config.input_root / "empty.pdf").write_bytes(b"")
    provider = Provider(models.SelectionBatch(references, skipped))
    result = await SelectionCoordinator(service, provider, None).select()
    assert result.error.code == "selection_failed" and provider.rollback
    root = service.config.artifact_root / "selections" / service.store.grant
    assert not list(root.glob("s1_*"))


@pytest.mark.asyncio
async def test_published_pages_are_rolled_back_if_provider_exit_fails(service):
    class Broken(Provider):
        @asynccontextmanager
        async def select(self):
            yield self.value
            raise ValueError("private provider failure")

    result = await SelectionCoordinator(
        service, Broken(models.SelectionBatch((), {})), None
    ).select()
    assert result.error.code == "selection_failed"
    assert not list(
        (service.config.artifact_root / "selections" / service.store.grant).glob("s1_*")
    )


@pytest.mark.asyncio
async def test_batch_tool_validates_wire_and_continuation_arguments(service):
    from openreading.mcp_server.tools import create_server
    from tests.test_mcp_selection import call

    provider = Provider({"references": (), "skipped": {"hidden": 2}})
    result, failed = await call(create_server(service, selection_provider=provider))
    assert not failed and result["skipped"] == {"hidden": 2}
    result, failed = await call(create_server(service), {"cursor": "not-a-cursor"})
    assert failed and result["error"]["code"] == "selection_failed"


@pytest.mark.asyncio
async def test_cancel_waits_for_receipt_writer_before_provider_rollback(service, monkeypatch):
    import asyncio
    import threading

    import anyio

    from openreading.mcp_server.selection_pages import SelectionPages

    entered, release = threading.Event(), threading.Event()
    original = SelectionPages.publish

    def writing(self, batch, **kwargs):
        entered.set()
        release.wait(3)
        return original(self, batch, **kwargs)

    monkeypatch.setattr(SelectionPages, "publish", writing)
    provider = Provider(models.SelectionBatch((), {}))
    task = asyncio.create_task(SelectionCoordinator(service, provider, None).select())
    while not entered.is_set():
        await anyio.sleep(0.001)
    task.cancel()
    await anyio.sleep(0.01)
    try:
        assert not task.done(), "Receipt writer must finish before cleanup ownership is released"
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert provider.rollback
    assert not list(
        (service.config.artifact_root / "selections" / service.store.grant).glob("s1_*")
    )
