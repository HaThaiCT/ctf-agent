"""Solver identity and challenge capacity at the orchestration seams."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest

from backend.agents.claude_solver import ClaudeSolver
from backend.agents.coordinator_core import do_spawn_swarm, do_submit_flag, mark_challenge_solved
from backend.agents.coordinator_loop import (
    _auto_spawn_one,
    _auto_spawn_unsolved,
    _run_coordinator_turns,
    _watch_capacity,
)
from backend.agents.swarm import ChallengeSwarm
from backend.config import Settings
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient, SubmitResult
from backend.deps import CoordinatorDeps
from backend.prompts import ChallengeMeta
from backend.solver_base import CANCELLED, FLAG_FOUND, QUOTA_ERROR, SolverResult


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    monkeypatch.setattr(Settings, "model_config", {"extra": "ignore"})


def test_sonnet_efforts_have_separate_traces_and_cost_entries(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    meta = ChallengeMeta(name="Example")
    tracker = CostTracker()
    settings = Settings()
    ctfd = create_autospec(CTFdClient, instance=True)
    specs = [
        "claude-sdk/ag/claude-sonnet-4-6/low",
        "claude-sdk/ag/claude-sonnet-4-6/high",
    ]
    solvers = [ClaudeSolver(spec, str(tmp_path), meta, ctfd, tracker, settings) for spec in specs]
    try:
        for solver, tokens in zip(solvers, (100, 200), strict=True):
            tracker.record_tokens(solver.agent_name, solver.model_id, input_tokens=tokens)
            solver.tracer.event("test", model_spec=solver.model_spec)

        assert solvers[0].model_id == solvers[1].model_id == "ag/claude-sonnet-4-6"
        assert solvers[0].agent_name != solvers[1].agent_name
        assert len(tracker.by_agent) == 2
        assert {tracker.by_agent[solver.agent_name].usage.input_tokens for solver in solvers} == {
            100,
            200,
        }
        assert solvers[0].tracer.path != solvers[1].tracer.path
        assert all(Path(solver.tracer.path).exists() for solver in solvers)
        third = ClaudeSolver(specs[0], str(tmp_path), meta, ctfd, tracker, settings)
        try:
            assert third.tracer.path != solvers[0].tracer.path
        finally:
            third.tracer.close()
    finally:
        for solver in solvers:
            solver.tracer.close()


@pytest.mark.asyncio
async def test_swarm_keeps_three_full_specs_as_independent_solvers(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    specs = [
        "claude-sdk/ag/claude-sonnet-4-6/low",
        "claude-sdk/ag/claude-sonnet-4-6/high",
        "claude-sdk/ag/claude-opus-4-6-thinking/medium",
    ]
    swarm = ChallengeSwarm(
        challenge_dir=str(tmp_path),
        meta=ChallengeMeta(name="Example"),
        ctfd=create_autospec(CTFdClient, instance=True),
        cost_tracker=CostTracker(),
        settings=Settings(),
        model_specs=specs,
        no_submit=True,
    )
    started = asyncio.Event()

    class FakeSolver:
        def __init__(self, spec):
            self.spec = spec
            self.tracer = SimpleNamespace(path=f"trace-{spec}")

        async def stop(self):
            pass

    def create_solver(spec):
        started.set()
        return FakeSolver(spec)

    async def run_loop(solver, spec):
        await swarm.cancel_event.wait()
        return None, solver

    monkeypatch.setattr(swarm, "_create_solver", create_solver)
    monkeypatch.setattr(swarm, "_run_solver_loop", run_loop)
    task = asyncio.create_task(swarm.run())
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert set(swarm.solvers) == set(specs)
        assert len(swarm.get_status()["agents"]) == 3
    finally:
        swarm.kill()
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_cancelled_running_swarm_still_occupies_challenge_slot(monkeypatch):
    finished = asyncio.Event()
    task = asyncio.create_task(finished.wait())
    deps = CoordinatorDeps(
        ctfd=create_autospec(CTFdClient, instance=True),
        cost_tracker=CostTracker(),
        settings=Settings(),
        swarms={
            "a": SimpleNamespace(cancel_event=asyncio.Event()),
            "b": SimpleNamespace(cancel_event=asyncio.Event()),
        },
        swarm_tasks={"a": task, "b": task},
        max_concurrent_challenges=2,
    )
    deps.swarms["a"].cancel_event.set()
    try:
        result = await do_spawn_swarm(deps, "c")
        assert result == "At capacity (2/2 challenges running). Wait for one to finish."
        assert set(deps.swarms) == {"a", "b"}
        await _auto_spawn_one(deps, "c")
        assert set(deps.swarms) == {"a", "b"}
    finally:
        finished.set()
        await task


@pytest.mark.asyncio
async def test_auto_spawn_starts_waiting_challenge_after_capacity_frees(monkeypatch):
    spawned = []
    deps = CoordinatorDeps(
        ctfd=create_autospec(CTFdClient, instance=True),
        cost_tracker=CostTracker(),
        settings=Settings(),
        max_concurrent_challenges=2,
    )
    deps.swarms.update({"a": object(), "b": object()})
    deps.swarm_tasks.update(
        {"a": asyncio.create_task(asyncio.sleep(30)), "b": asyncio.create_task(asyncio.sleep(30))}
    )
    attempted = {"a", "b"}
    poller = SimpleNamespace(known_challenges={"a", "b", "c"}, known_solved=set())

    async def fake_spawn(state, name):
        spawned.append(name)
        state.swarms[name] = object()
        return "spawned"

    monkeypatch.setattr("backend.agents.coordinator_core.do_spawn_swarm", fake_spawn)
    try:
        await _auto_spawn_unsolved(deps, poller, attempted)
        assert spawned == []
        deps.swarm_tasks["a"].cancel()
        await asyncio.gather(deps.swarm_tasks["a"], return_exceptions=True)
        deps.swarm_tasks.pop("a")
        deps.swarms.pop("a")
        await _auto_spawn_unsolved(deps, poller, attempted)
        await _auto_spawn_unsolved(deps, poller, attempted)
        assert spawned == ["c"]
        assert attempted == {"a", "b", "c"}
    finally:
        for task in deps.swarm_tasks.values():
            task.cancel()
        await asyncio.gather(*deps.swarm_tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_parallel_spawn_requests_cannot_exceed_two_challenges(tmp_path, monkeypatch):
    deps = CoordinatorDeps(
        ctfd=create_autospec(CTFdClient, instance=True),
        cost_tracker=CostTracker(),
        settings=Settings(),
        max_concurrent_challenges=2,
        model_specs=["claude-sdk/ag/claude-sonnet-4-6/medium"],
        challenge_dirs={name: str(tmp_path) for name in ("a", "b", "c")},
        challenge_metas={name: ChallengeMeta(name=name) for name in ("a", "b", "c")},
    )
    release = asyncio.Event()

    async def fake_run(self):
        await release.wait()
        return None

    monkeypatch.setattr(ChallengeSwarm, "run", fake_run)
    try:
        results = await asyncio.gather(*(do_spawn_swarm(deps, name) for name in ("a", "b", "c")))
        assert sum(result.startswith("Swarm spawned") for result in results) == 2
        assert sum(result.startswith("At capacity (2/2") for result in results) == 1
        assert len(deps.swarm_tasks) == len(deps.swarms) == 2
    finally:
        release.set()
        await asyncio.gather(*deps.swarm_tasks.values())


@pytest.mark.asyncio
async def test_external_solve_interrupts_blocked_solvers_and_drains_cleanup(tmp_path, monkeypatch):
    specs = ["claude-sdk/one", "claude-sdk/two", "claude-sdk/three"]
    swarm = ChallengeSwarm(
        str(tmp_path),
        ChallengeMeta(name="a"),
        create_autospec(CTFdClient, instance=True),
        CostTracker(),
        Settings(),
        model_specs=specs,
    )
    started = asyncio.Event()
    cleanup_release = asyncio.Event()
    stopped = set()

    class FakeSolver:
        def __init__(self, spec):
            self.spec = spec

        async def start(self):
            if len(swarm.solvers) == len(specs):
                started.set()

        async def run_until_done_or_gave_up(self):
            await asyncio.Future()

        async def stop(self):
            await cleanup_release.wait()
            stopped.add(self.spec)

    monkeypatch.setattr(swarm, "_create_solver", FakeSolver)
    task = asyncio.create_task(swarm.run())
    try:
        await asyncio.wait_for(started.wait(), 1)
        swarm.kill()
        await asyncio.sleep(0)
        assert not task.done()
        cleanup_release.set()
        assert await asyncio.wait_for(task, 1) is None
        assert stopped == set(specs)
    finally:
        cleanup_release.set()
        swarm.kill()
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("submission", "expected_flag"),
    [
        (SubmitResult("correct", "", "CORRECT"), "flag{candidate}"),
        (SubmitResult("already_solved", "", "ALREADY SOLVED"), None),
    ],
)
async def test_solver_confirmation_interrupts_model_awaits_and_preserves_flag_semantics(
    tmp_path, monkeypatch, submission, expected_flag
):
    ctfd = create_autospec(CTFdClient, instance=True)
    ctfd.submit_flag.return_value = submission
    swarm = ChallengeSwarm(
        str(tmp_path),
        ChallengeMeta(name="a"),
        ctfd,
        CostTracker(),
        Settings(),
        model_specs=["claude-sdk/one", "claude-sdk/two"],
    )
    started = asyncio.Event()
    stopped = set()

    class FakeSolver:
        def __init__(self, spec):
            self.spec = spec

        async def start(self):
            started.set()

        async def run_until_done_or_gave_up(self):
            await asyncio.Future()

        async def stop(self):
            stopped.add(self.spec)

    monkeypatch.setattr(swarm, "_create_solver", FakeSolver)
    task = asyncio.create_task(swarm.run())
    try:
        await asyncio.wait_for(started.wait(), 1)
        display, confirmed = await swarm.try_submit_flag("flag{candidate}", "claude-sdk/one")
        assert confirmed and display == submission.display
        result = await asyncio.wait_for(task, 1)
        assert result is not None
        assert result.status == FLAG_FOUND
        assert result.flag == expected_flag
        assert stopped == {"claude-sdk/one", "claude-sdk/two"}
    finally:
        swarm.kill()
        await task


@pytest.mark.asyncio
async def test_coordinator_submit_marks_solved_and_kills_only_on_confirmation():
    ctfd = create_autospec(CTFdClient, instance=True)
    swarm = SimpleNamespace(kill=lambda: killed.append(True))
    killed = []
    deps = CoordinatorDeps(
        ctfd=ctfd, cost_tracker=CostTracker(), settings=Settings(), swarms={"a": swarm}
    )
    ctfd.submit_flag.return_value = SubmitResult("incorrect", "", "INCORRECT")
    assert await do_submit_flag(deps, "a", "guess") == "INCORRECT"
    assert not killed and not deps.solved_challenges
    ctfd.submit_flag.return_value = SubmitResult("already_solved", "", "ALREADY SOLVED")
    assert await do_submit_flag(deps, "a", "guess") == "ALREADY SOLVED"
    assert killed == [True]
    assert deps.solved_challenges == {"a"}
    assert deps.results["a"]["flag"] is None


@pytest.mark.asyncio
async def test_coordinator_no_submit_does_not_submit_mark_solved_or_kill():
    ctfd = create_autospec(CTFdClient, instance=True)
    killed = []
    deps = CoordinatorDeps(
        ctfd=ctfd,
        cost_tracker=CostTracker(),
        settings=Settings(),
        no_submit=True,
        swarms={"a": SimpleNamespace(kill=lambda: killed.append(True))},
    )

    assert await do_submit_flag(deps, "a", "flag{candidate}") == (
        'DRY RUN — would submit "flag{candidate}" for a'
    )
    ctfd.submit_flag.assert_not_awaited()
    assert not killed
    assert not deps.solved_challenges
    assert "a" not in deps.results


@pytest.mark.asyncio
async def test_quota_fallback_is_stopped_when_swarm_is_cancelled(tmp_path, monkeypatch):
    spec = "claude-sdk/claude-opus-4-6"
    swarm = ChallengeSwarm(
        str(tmp_path),
        ChallengeMeta(name="a"),
        create_autospec(CTFdClient, instance=True),
        CostTracker(),
        Settings(),
        model_specs=[spec],
    )
    fallback_running = asyncio.Event()
    old_stopped = asyncio.Event()
    fallback_stopped = asyncio.Event()
    sandbox = object()

    class OldSolver:
        model_spec = spec
        agent_name = "a/original"

        def __init__(self):
            self.sandbox = sandbox

        async def start(self):
            pass

        async def run_until_done_or_gave_up(self):
            return SolverResult(None, QUOTA_ERROR, "quota", 1, 0.0, "")

        def bump(self, insights):
            pass

        async def stop(self):
            old_stopped.set()

    class FallbackSolver:
        model_spec = "bedrock/fallback"
        agent_name = "a/fallback"

        def __init__(self):
            self.sandbox = sandbox

        async def start(self):
            fallback_running.set()

        async def run_until_done_or_gave_up(self):
            await asyncio.Future()

        def bump(self, insights):
            pass

        async def stop(self):
            fallback_stopped.set()

    old_solver = OldSolver()
    fallback_solver = FallbackSolver()
    monkeypatch.setattr(swarm, "_create_solver", lambda _: old_solver)
    monkeypatch.setattr(swarm, "_create_pydantic_solver", lambda *args, **kwargs: fallback_solver)

    task = asyncio.create_task(swarm.run())
    try:
        await asyncio.wait_for(fallback_running.wait(), 1)
        swarm.kill()
        await asyncio.wait_for(task, 1)
        assert old_stopped.is_set()
        assert fallback_stopped.is_set()
    finally:
        swarm.kill()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_quota_handoff_waits_for_old_solver_shutdown_when_cancelled(tmp_path, monkeypatch):
    spec = "claude-sdk/claude-opus-4-6"
    swarm = ChallengeSwarm(
        str(tmp_path),
        ChallengeMeta(name="a"),
        create_autospec(CTFdClient, instance=True),
        CostTracker(),
        Settings(),
        model_specs=[spec],
    )
    old_stop_entered = asyncio.Event()
    old_stop_release = asyncio.Event()
    old_stop_finished = asyncio.Event()
    fallback_stopped = asyncio.Event()
    sandbox = object()

    class OldSolver:
        model_spec = spec
        agent_name = "a/original"

        def __init__(self):
            self.sandbox = sandbox

        async def start(self):
            pass

        async def run_until_done_or_gave_up(self):
            return SolverResult(None, QUOTA_ERROR, "quota", 1, 0.0, "")

        def bump(self, insights):
            pass

        async def stop(self):
            old_stop_entered.set()
            await old_stop_release.wait()
            old_stop_finished.set()

    class FallbackSolver:
        model_spec = "bedrock/fallback"
        agent_name = "a/fallback"

        def __init__(self):
            self.sandbox = sandbox

        async def start(self):
            raise AssertionError("cancelled handoff must not start fallback")

        async def run_until_done_or_gave_up(self):
            return SolverResult(None, CANCELLED, "", 0, 0.0, "")

        def bump(self, insights):
            pass

        async def stop(self):
            fallback_stopped.set()

    monkeypatch.setattr(swarm, "_create_solver", lambda _: OldSolver())
    monkeypatch.setattr(swarm, "_create_pydantic_solver", lambda *args, **kwargs: FallbackSolver())

    task = asyncio.create_task(swarm.run())
    try:
        await asyncio.wait_for(old_stop_entered.wait(), 1)
        swarm.kill()
        await asyncio.sleep(0)
        assert not task.done()
        old_stop_release.set()
        assert await asyncio.wait_for(task, 1) is None
        assert old_stop_finished.is_set()
        assert fallback_stopped.is_set()
    finally:
        old_stop_release.set()
        swarm.kill()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cleanup_failure_keeps_slot_and_does_not_start_waiting_challenge(
    tmp_path, monkeypatch
):
    deps = CoordinatorDeps(
        ctfd=create_autospec(CTFdClient, instance=True),
        cost_tracker=CostTracker(),
        settings=Settings(),
        max_concurrent_challenges=1,
        model_specs=["claude-sdk/one"],
        challenge_dirs={name: str(tmp_path) for name in ("a", "b")},
        challenge_metas={name: ChallengeMeta(name=name) for name in ("a", "b")},
    )

    async def fail_cleanup(self):
        raise RuntimeError("container delete failed")

    monkeypatch.setattr(ChallengeSwarm, "run", fail_cleanup)
    await do_spawn_swarm(deps, "a")
    poller = SimpleNamespace(known_challenges={"a", "b"}, known_solved=set())
    worker = asyncio.create_task(_watch_capacity(deps, poller))
    try:
        await asyncio.wait_for(deps.swarm_tasks["a"], 1)
    except RuntimeError:
        pass
    try:
        for _ in range(10):
            await asyncio.sleep(0)
        assert set(deps.swarms) == {"a"}
        assert set(deps.swarm_tasks) == {"a"}
        assert "b" not in deps.auto_attempted
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_solved_during_pull_does_not_start_swarm(tmp_path):
    ctfd = create_autospec(CTFdClient, instance=True)
    ctfd.fetch_all_challenges.return_value = [{"name": "a"}]
    deps = CoordinatorDeps(ctfd=ctfd, cost_tracker=CostTracker(), settings=Settings())

    async def pull(*args):
        mark_challenge_solved(deps, "a")
        return str(tmp_path)

    ctfd.pull_challenge.side_effect = pull
    (tmp_path / "metadata.yml").write_text("name: a\n")
    assert "already solved" in await do_spawn_swarm(deps, "a")
    assert not deps.swarms and not deps.swarm_tasks


@pytest.mark.asyncio
async def test_external_solve_releases_slot_then_starts_waiting_challenge(tmp_path, monkeypatch):
    deps = CoordinatorDeps(
        ctfd=create_autospec(CTFdClient, instance=True),
        cost_tracker=CostTracker(),
        settings=Settings(),
        max_concurrent_challenges=1,
        model_specs=["claude-sdk/one"],
        challenge_dirs={name: str(tmp_path) for name in ("a", "b")},
        challenge_metas={name: ChallengeMeta(name=name) for name in ("a", "b")},
    )
    started = {name: asyncio.Event() for name in ("a", "b")}
    cleanup_release = asyncio.Event()

    async def fake_run(self):
        started[self.meta.name].set()
        if self.meta.name == "a":
            await self.cancel_event.wait()
            await cleanup_release.wait()
        return None

    monkeypatch.setattr(ChallengeSwarm, "run", fake_run)
    await do_spawn_swarm(deps, "a")
    poller = SimpleNamespace(known_challenges={"a", "b"}, known_solved=set())
    worker = asyncio.create_task(_watch_capacity(deps, poller))
    try:
        await asyncio.wait_for(started["a"].wait(), 1)
        mark_challenge_solved(deps, "a")
        await asyncio.sleep(0)
        assert set(deps.swarms) == {"a"}
        assert not started["b"].is_set()

        cleanup_release.set()
        await asyncio.wait_for(started["b"].wait(), 1)
        assert set(deps.swarms) == {"b"}
        assert "a" in deps.solved_challenges
    finally:
        cleanup_release.set()
        if "b" in deps.swarms:
            deps.swarms["b"].kill()
        worker.cancel()
        await asyncio.gather(worker, *deps.swarm_tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_cleanup_releases_slot_then_starts_waiting_challenge(tmp_path, monkeypatch):
    deps = CoordinatorDeps(
        ctfd=create_autospec(CTFdClient, instance=True),
        cost_tracker=CostTracker(),
        settings=Settings(),
        max_concurrent_challenges=1,
        model_specs=["claude-sdk/one"],
        challenge_dirs={name: str(tmp_path) for name in ("a", "b")},
        challenge_metas={name: ChallengeMeta(name=name) for name in ("a", "b")},
    )
    releases = {name: asyncio.Event() for name in ("a", "b")}

    async def fake_run(self):
        await releases[self.meta.name].wait()
        return None

    monkeypatch.setattr(ChallengeSwarm, "run", fake_run)
    await do_spawn_swarm(deps, "a")
    poller = SimpleNamespace(known_challenges={"a", "b"}, known_solved=set())
    worker = asyncio.create_task(_watch_capacity(deps, poller))
    try:
        await _auto_spawn_unsolved(deps, poller, deps.auto_attempted)
        assert set(deps.swarms) == {"a"}
        releases["a"].set()
        for _ in range(10):
            if set(deps.swarms) == {"b"}:
                break
            await asyncio.sleep(0)
        assert set(deps.swarms) == {"b"}
    finally:
        releases["b"].set()
        worker.cancel()
        await asyncio.gather(worker, *deps.swarm_tasks.values(), return_exceptions=True)


@pytest.mark.asyncio
async def test_slow_coordinator_turn_does_not_block_followup_messages():
    entered = asyncio.Event()
    release = asyncio.Event()
    seen = []

    async def turn(message):
        seen.append(message)
        entered.set()
        await release.wait()

    inbox = asyncio.Queue()
    worker = asyncio.create_task(_run_coordinator_turns(turn, inbox))
    try:
        inbox.put_nowait("initial")
        await asyncio.wait_for(entered.wait(), 1)
        inbox.put_nowait("solved")
        await asyncio.sleep(0)
        assert seen == ["initial"]
        assert inbox.qsize() == 1
        release.set()
        for _ in range(10):
            if seen == ["initial", "solved"]:
                break
            await asyncio.sleep(0)
        assert seen == ["initial", "solved"]
    finally:
        release.set()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
