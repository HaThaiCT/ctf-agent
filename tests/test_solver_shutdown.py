"""Solver subprocess shutdown tests using in-memory fakes."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.agents.claude_solver import ClaudeSolver
from backend.agents.codex_solver import CodexSolver
from backend.config import Settings
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.prompts import ChallengeMeta


def _solver_args(tmp_path):
    return (
        "claude-sdk/test",
        str(tmp_path),
        ChallengeMeta(name="a"),
        MagicMock(spec=CTFdClient),
        CostTracker(),
        Settings(),
    )


@pytest.mark.asyncio
async def test_claude_client_close_error_still_stops_sandbox(tmp_path):
    solver = ClaudeSolver(*_solver_args(tmp_path))
    solver._client = MagicMock()
    solver._client.__aexit__ = AsyncMock(side_effect=RuntimeError("SDK close failed"))
    solver.sandbox.stop = AsyncMock()

    with pytest.raises(RuntimeError, match="SDK close failed"):
        await solver.stop()

    solver.sandbox.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_codex_stop_kills_process_after_terminate_timeout(tmp_path, monkeypatch):
    args = list(_solver_args(tmp_path))
    args[0] = "codex/test"
    solver = CodexSolver(*args)
    proc = MagicMock()
    proc.returncode = None
    proc.wait = AsyncMock()
    solver._proc = proc
    solver.sandbox.stop = AsyncMock()

    wait_calls = 0

    async def fake_wait_for(awaitable, timeout):
        nonlocal wait_calls
        wait_calls += 1
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr("backend.agents.codex_solver.asyncio.wait_for", fake_wait_for)

    await solver.stop()

    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
    assert proc.wait.call_count == 2
    assert proc.wait.await_count == 1
    assert wait_calls == 1
    solver.sandbox.stop.assert_awaited_once()
