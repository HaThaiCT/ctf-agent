"""Cancellation-safe Docker sandbox lifecycle tests using fake Docker handles."""

import asyncio
from types import SimpleNamespace

import pytest

from backend.sandbox import DockerSandbox


@pytest.mark.asyncio
async def test_cancelled_start_waits_for_partial_container_removal(tmp_path, monkeypatch):
    start_entered = asyncio.Event()
    delete_release = asyncio.Event()
    deleted = asyncio.Event()

    class FakeContainer:
        id = "container-id"

        async def start(self):
            start_entered.set()
            await asyncio.Future()

        async def delete(self, *, force):
            assert force is True
            await delete_release.wait()
            deleted.set()

    container = FakeContainer()

    class FakeDocker:
        containers = SimpleNamespace(create=lambda config: async_value(container))

        async def close(self):
            pass

    async def async_value(value):
        return value

    monkeypatch.setattr("backend.sandbox.aiodocker.Docker", FakeDocker)
    sandbox = DockerSandbox("image", str(tmp_path))
    task = asyncio.create_task(sandbox.start())
    await asyncio.wait_for(start_entered.wait(), 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    delete_release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert deleted.is_set()
    assert sandbox._container is None
    assert sandbox._docker is None
    assert sandbox.workspace_dir == ""


@pytest.mark.asyncio
async def test_start_failure_removes_partial_container_and_workspace(tmp_path, monkeypatch):
    deleted = asyncio.Event()

    class FakeContainer:
        id = "container-id"

        async def start(self):
            raise RuntimeError("start failed")

        async def delete(self, *, force):
            assert force is True
            deleted.set()

    container = FakeContainer()

    class FakeDocker:
        containers = SimpleNamespace(create=lambda config: async_value(container))

        async def close(self):
            pass

    async def async_value(value):
        return value

    monkeypatch.setattr("backend.sandbox.aiodocker.Docker", FakeDocker)
    sandbox = DockerSandbox("image", str(tmp_path))

    with pytest.raises(RuntimeError, match="start failed"):
        await sandbox.start()

    assert deleted.is_set()
    assert sandbox._container is None
    assert sandbox._docker is None
    assert sandbox.workspace_dir == ""


@pytest.mark.asyncio
async def test_docker_client_close_failure_does_not_leave_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class FakeDocker:
        async def close(self):
            raise RuntimeError("client close failed")

    sandbox = DockerSandbox("image", str(tmp_path), workspace_dir=str(workspace))
    sandbox._docker = FakeDocker()

    await sandbox.stop()

    assert sandbox._docker is None
    assert sandbox.workspace_dir == ""
    assert not workspace.exists()


@pytest.mark.asyncio
async def test_failed_delete_keeps_container_handle_and_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    class FakeContainer:
        id = "container-id"

        async def delete(self, *, force):
            raise RuntimeError("daemon unavailable")

    class FakeDocker:
        async def close(self):
            pass

    sandbox = DockerSandbox("image", str(tmp_path), workspace_dir=str(workspace))
    sandbox._container = FakeContainer()
    sandbox._docker = FakeDocker()
    with pytest.raises(RuntimeError, match="daemon unavailable"):
        await sandbox.stop()
    assert sandbox._container is not None
    assert sandbox.workspace_dir == str(workspace)
    assert workspace.exists()
