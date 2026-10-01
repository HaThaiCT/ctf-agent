"""Single-challenge CTFd polling behavior without network, Docker, or model calls."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from backend.cli import _run_single
from backend.config import Settings
from backend.ctfd import CTFdClient
from backend.poller import CTFdPoller
from backend.solver_base import FLAG_FOUND, SolverResult


def _write_metadata(tmp_path, name="a"):
    (tmp_path / "metadata.yml").write_text(
        f"name: {name}\ncategory: misc\nvalue: 100\ndescription: test\n"
    )


class FakeCTFd:
    def __init__(self):
        self.close = AsyncMock()
        self.submit_flag = AsyncMock()


@pytest.mark.asyncio
async def test_single_live_mode_skips_swarm_when_challenge_is_already_solved(tmp_path, monkeypatch):
    _write_metadata(tmp_path)
    ctfd = FakeCTFd()
    started_swarms = []

    class FakePoller:
        known_solved = {"a"}

        def __init__(self, *, ctfd, on_solved):
            self.ctfd = ctfd
            self.on_solved = on_solved

        async def start(self):
            pass

        async def stop(self):
            pass

    async def fake_run(self):
        started_swarms.append(self)
        return None

    monkeypatch.setattr("backend.ctfd.CTFdClient", lambda **kwargs: ctfd)
    monkeypatch.setattr("backend.poller.CTFdPoller", FakePoller)
    monkeypatch.setattr("backend.sandbox.cleanup_orphan_containers", AsyncMock())
    monkeypatch.setattr("backend.agents.swarm.ChallengeSwarm.run", fake_run)

    settings = Settings(ctfd_token="configured")
    await _run_single(settings, str(tmp_path), ["claude-sdk/test"], False, 1)

    assert not started_swarms
    ctfd.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_single_live_external_solve_kills_swarm_and_waits_for_cleanup(tmp_path, monkeypatch):
    _write_metadata(tmp_path)
    ctfd = FakeCTFd()
    poller_started = asyncio.Event()
    solve_now = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    cleanup_finished = False

    class FakePoller:
        known_solved = set()

        def __init__(self, *, ctfd, on_solved):
            self.ctfd = ctfd
            self.on_solved = on_solved
            self.task = None

        async def start(self):
            async def emit_solve():
                await solve_now.wait()
                self.on_solved("a")

            self.task = asyncio.create_task(emit_solve())
            poller_started.set()

        async def stop(self):
            if self.task:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)

    async def fake_run(self):
        nonlocal cleanup_finished
        assert not self.cancel_event.is_set()
        solve_now.set()
        await self.cancel_event.wait()
        cleanup_started.set()
        await cleanup_release.wait()
        cleanup_finished = True
        return None

    monkeypatch.setattr("backend.ctfd.CTFdClient", lambda **kwargs: ctfd)
    monkeypatch.setattr("backend.poller.CTFdPoller", FakePoller)
    monkeypatch.setattr("backend.sandbox.cleanup_orphan_containers", AsyncMock())
    monkeypatch.setattr("backend.agents.swarm.ChallengeSwarm.run", fake_run)

    settings = Settings(ctfd_token="configured")
    task = asyncio.create_task(_run_single(settings, str(tmp_path), ["claude-sdk/test"], False, 1))
    await asyncio.wait_for(poller_started.wait(), 1)
    await asyncio.wait_for(cleanup_started.wait(), 1)
    assert not task.done()
    cleanup_release.set()
    await asyncio.wait_for(task, 1)

    assert cleanup_finished
    ctfd.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("username", "password", "expect_poller"),
    [
        ("admin", "admin", False),
        ("user", "secret", True),
        ("user", "admin", False),
        ("admin", "secret", False),
    ],
)
async def test_single_live_mode_requires_complete_nondefault_login_credentials(
    tmp_path, monkeypatch, username, password, expect_poller
):
    _write_metadata(tmp_path)
    ctfd = FakeCTFd()
    created = []

    class FakePoller:
        known_solved = set()

        def __init__(self, *, ctfd, on_solved):
            created.append(self)

        async def start(self):
            pass

        async def stop(self):
            pass

    async def fake_run(self):
        return None

    monkeypatch.setattr("backend.ctfd.CTFdClient", lambda **kwargs: ctfd)
    monkeypatch.setattr("backend.poller.CTFdPoller", FakePoller)
    monkeypatch.setattr("backend.sandbox.cleanup_orphan_containers", AsyncMock())
    monkeypatch.setattr("backend.agents.swarm.ChallengeSwarm.run", fake_run)

    settings = Settings(ctfd_user=username, ctfd_pass=password)
    await _run_single(settings, str(tmp_path), ["claude-sdk/test"], False, 1)

    assert bool(created) is expect_poller


@pytest.mark.asyncio
async def test_single_no_submit_does_not_start_ctfd_poller(tmp_path, monkeypatch):
    _write_metadata(tmp_path)
    ctfd = FakeCTFd()
    poller_created = False

    class FailingPoller:
        def __init__(self, **kwargs):
            nonlocal poller_created
            poller_created = True
            raise AssertionError("--no-submit must not start CTFd polling")

    async def fake_run(self):
        assert self.no_submit is True
        return SolverResult("flag{test}", FLAG_FOUND, "", 1, 0.0, "")

    monkeypatch.setattr("backend.ctfd.CTFdClient", lambda **kwargs: ctfd)
    monkeypatch.setattr("backend.poller.CTFdPoller", FailingPoller)
    monkeypatch.setattr("backend.sandbox.cleanup_orphan_containers", AsyncMock())
    monkeypatch.setattr("backend.agents.swarm.ChallengeSwarm.run", fake_run)

    await _run_single(Settings(), str(tmp_path), ["claude-sdk/test"], True, 1)

    assert not poller_created
    ctfd.submit_flag.assert_not_awaited()
    ctfd.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_poller_calls_callback_and_emits_event_for_external_solve():
    callback_names = []

    ctfd = CTFdClient()
    ctfd.fetch_challenge_stubs = AsyncMock(  # type: ignore[method-assign]
        return_value=[{"name": "a"}, {"name": "b"}]
    )
    ctfd.fetch_solved_names = AsyncMock(  # type: ignore[method-assign]
        side_effect=[set(), {"b"}]
    )

    poller = CTFdPoller(ctfd, on_solved=callback_names.append)
    await poller._seed()
    await poller._poll_once()

    events = poller.drain_events()
    assert callback_names == ["b"]
    assert [(event.kind, event.challenge_name) for event in events] == [("challenge_solved", "b")]
