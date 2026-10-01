"""ChallengeSwarm — Parallel solvers racing on one challenge."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from backend.agents.solver import Solver
from backend.cost_tracker import CostTracker
from backend.ctfd import CTFdClient
from backend.message_bus import ChallengeMessageBus
from backend.models import DEFAULT_MODELS, provider_from_spec
from backend.prompts import ChallengeMeta
from backend.solver_base import (
    CANCELLED,
    ERROR,
    FLAG_FOUND,
    GAVE_UP,
    QUOTA_ERROR,
    SolverProtocol,
    SolverResult,
)

if TYPE_CHECKING:
    from backend.config import Settings

logger = logging.getLogger(__name__)


# Quota fallback: map subscription-backed providers to API-backed equivalents
QUOTA_FALLBACK: dict[str, str] = {
    "claude-sdk/claude-opus-4-6": "bedrock/us.anthropic.claude-opus-4-6-v1",
    "codex/gpt-5.4": "azure/gpt-5.4",
    "codex/gpt-5.4-mini": "azure/gpt-5.4-mini",
    "codex/gpt-5.3-codex-spark": "zen/gpt-5.3-codex-spark",
}


def _quota_fallback_spec(model_spec: str) -> str | None:
    return QUOTA_FALLBACK.get(model_spec)


@dataclass
class ChallengeSwarm:
    """Parallel solvers racing on one challenge."""

    challenge_dir: str
    meta: ChallengeMeta
    ctfd: CTFdClient
    cost_tracker: CostTracker
    settings: Settings
    model_specs: list[str] = field(default_factory=lambda: list(DEFAULT_MODELS))
    no_submit: bool = False
    coordinator_inbox: asyncio.Queue | None = None

    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    solvers: dict[str, SolverProtocol] = field(default_factory=dict)
    findings: dict[str, str] = field(default_factory=dict)
    winner: SolverResult | None = None
    confirmed_flag: str | None = None
    _flag_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _submit_count: dict[str, int] = field(default_factory=dict)  # per-model wrong submission count
    _submitted_flags: set[str] = field(default_factory=set)  # dedup exact flags
    _last_submit_time: dict[str, float] = field(
        default_factory=dict
    )  # per-model last submit timestamp
    message_bus: ChallengeMessageBus = field(default_factory=ChallengeMessageBus)
    _solver_tasks: set[asyncio.Task] = field(default_factory=set, repr=False)
    _stopping_tasks: set[asyncio.Task] = field(default_factory=set, repr=False)
    _confirmed_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _confirmed_result: SolverResult | None = field(default=None, repr=False)

    def _create_solver(self, model_spec: str):
        """Create the right solver type based on provider.

        - claude-sdk/* → ClaudeSolver (Claude Agent SDK, subscription-first)
        - codex/* → CodexSolver (Codex App Server, subscription-first)
        - bedrock/*, azure/*, zen/*, google/* → Pydantic AI Solver (API)
        """
        provider = provider_from_spec(model_spec)

        def _submit_fn(flag):
            return self.try_submit_flag(flag, model_spec)

        _notify = self._make_notify_fn(model_spec)

        if provider == "claude-sdk":
            from backend.agents.claude_solver import ClaudeSolver

            return ClaudeSolver(
                model_spec=model_spec,
                challenge_dir=self.challenge_dir,
                meta=self.meta,
                ctfd=self.ctfd,
                cost_tracker=self.cost_tracker,
                settings=self.settings,
                cancel_event=self.cancel_event,
                no_submit=self.no_submit,
                submit_fn=_submit_fn,
                message_bus=self.message_bus,
                notify_coordinator=_notify,
            )

        if provider == "codex":
            from backend.agents.codex_solver import CodexSolver

            return CodexSolver(
                model_spec=model_spec,
                challenge_dir=self.challenge_dir,
                meta=self.meta,
                ctfd=self.ctfd,
                cost_tracker=self.cost_tracker,
                settings=self.settings,
                cancel_event=self.cancel_event,
                no_submit=self.no_submit,
                submit_fn=_submit_fn,
                message_bus=self.message_bus,
                notify_coordinator=_notify,
            )

        return self._create_pydantic_solver(model_spec)

    def _make_notify_fn(self, model_spec: str):
        """Create a callback that pushes solver messages to the coordinator inbox."""

        async def _notify(message: str) -> None:
            if self.coordinator_inbox:
                self.coordinator_inbox.put_nowait(f"[{self.meta.name}/{model_spec}] {message}")

        return _notify

    def _create_pydantic_solver(
        self, model_spec: str, sandbox=None, owns_sandbox: bool | None = None
    ) -> Solver:
        """Create a Pydantic AI solver. Pass sandbox to reuse an existing container (quota fallback)."""
        solver = Solver(
            model_spec=model_spec,
            challenge_dir=self.challenge_dir,
            meta=self.meta,
            ctfd=self.ctfd,
            cost_tracker=self.cost_tracker,
            settings=self.settings,
            cancel_event=self.cancel_event,
            sandbox=sandbox,
            owns_sandbox=owns_sandbox,
        )
        solver.deps.message_bus = self.message_bus
        solver.deps.model_spec = model_spec
        solver.deps.no_submit = self.no_submit
        solver.deps.submit_fn = lambda flag: self.try_submit_flag(flag, model_spec)
        solver.deps.notify_coordinator = self._make_notify_fn(model_spec)
        return solver

    def _gather_sibling_insights(self, exclude_model: str) -> str:
        parts: list[str] = []
        for model, finding in self.findings.items():
            if model != exclude_model and finding:
                parts.append(f"[{model}]: {finding}")
        return "\n\n".join(parts) if parts else "No sibling insights available yet."

    # Escalating cooldowns after incorrect submissions (per model)
    SUBMISSION_COOLDOWNS = [0, 30, 120, 300, 600]  # 0s, 30s, 2min, 5min, 10min

    async def try_submit_flag(self, flag: str, model_spec: str) -> tuple[str, bool]:
        """Cooldown-gated, deduplicated flag submission. Returns (display, is_confirmed)."""
        async with self._flag_lock:
            if self.confirmed_flag:
                return "ALREADY SOLVED — challenge already confirmed", True

            normalized = flag.strip()

            # Dedup exact flags across all models
            if normalized in self._submitted_flags:
                return "INCORRECT — already tried this exact flag.", False

            # Escalating cooldown after incorrect submissions
            wrong_count = self._submit_count.get(model_spec, 0)
            cooldown_idx = min(wrong_count, len(self.SUBMISSION_COOLDOWNS) - 1)
            cooldown = self.SUBMISSION_COOLDOWNS[cooldown_idx]
            if cooldown > 0:
                last_time = self._last_submit_time.get(model_spec, 0)
                elapsed = time.monotonic() - last_time
                if elapsed < cooldown:
                    remaining = int(cooldown - elapsed)
                    return (
                        f"COOLDOWN — wait {remaining}s before submitting again. "
                        f"You have {wrong_count} incorrect submissions. "
                        "Use this time to do deeper analysis and verify your flag.",
                        False,
                    )

            self._submitted_flags.add(normalized)

            try:
                submission = await self.ctfd.submit_flag(self.meta.name, normalized)
                display = submission.display
                is_confirmed = submission.status in ("correct", "already_solved")
            except Exception as exc:
                display, is_confirmed = f"submit_flag error: {exc}", False

            if is_confirmed:
                # An already-solved response confirms the challenge, not this candidate flag.
                correct_flag = normalized if submission.status == "correct" else None
                self.confirmed_flag = correct_flag or "<already solved>"
                self._confirmed_result = SolverResult(
                    flag=correct_flag,
                    status=FLAG_FOUND,
                    findings_summary=display,
                    step_count=0,
                    cost_usd=0.0,
                    log_path="",
                )
                self._confirmed_event.set()
            else:
                self._submit_count[model_spec] = wrong_count + 1
                self._last_submit_time[model_spec] = time.monotonic()
            return display, is_confirmed

    async def _run_solver(self, model_spec: str) -> SolverResult | None:
        if self.cancel_event.is_set():
            return None
        solver = self._create_solver(model_spec)
        self.solvers[model_spec] = solver

        try:
            result, final_solver = await self._run_solver_loop(solver, model_spec)
            solver = final_solver
            return result
        except Exception as e:
            logger.error(f"[{self.meta.name}/{model_spec}] Fatal: {e}", exc_info=True)
            return None
        finally:
            await self._stop_solver_task(model_spec)

    async def _stop_solver_task(self, model_spec: str) -> None:
        """Finish solver shutdown even if this task is cancelled during cleanup."""
        task = asyncio.current_task()
        if task:
            self._stopping_tasks.add(task)
        try:
            await self._finish_cleanup(self.solvers[model_spec].stop())
        finally:
            if task:
                self._stopping_tasks.discard(task)

    @staticmethod
    async def _finish_cleanup(awaitable: Coroutine[Any, Any, None]) -> None:
        """Shield one cleanup operation and re-raise cancellation after it finishes."""
        cleanup = asyncio.create_task(awaitable)
        interrupted = False
        while not cleanup.done():
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                interrupted = True
        await cleanup
        if interrupted:
            raise asyncio.CancelledError from None

    async def _run_solver_loop(
        self, solver: SolverProtocol, model_spec: str
    ) -> tuple[SolverResult, SolverProtocol]:
        """Inner loop: start → run → bump → run → ..."""
        bump_count = 0
        consecutive_errors = 0
        result = SolverResult(
            flag=None,
            status=CANCELLED,
            findings_summary="",
            step_count=0,
            cost_usd=0.0,
            log_path="",
        )
        if self.cancel_event.is_set():
            return result, solver
        await solver.start()

        while not self.cancel_event.is_set():
            result = await solver.run_until_done_or_gave_up()

            # Only broadcast useful findings — skip errors and broken solvers
            if (
                result.status not in (ERROR, QUOTA_ERROR)
                and not (result.step_count == 0 and result.cost_usd == 0)
                and result.findings_summary
                and not result.findings_summary.startswith(("Error:", "Turn failed:"))
            ):
                self.findings[model_spec] = result.findings_summary
                await self.message_bus.post(model_spec, result.findings_summary[:500])

            if result.status == FLAG_FOUND:
                logger.info(f"[{self.meta.name}] Flag found by {model_spec}: {result.flag}")
                return result, solver

            if result.status == CANCELLED:
                break

            # Quota exhaustion: fall back to API-backed Pydantic AI solver
            if result.status == QUOTA_ERROR:
                fallback_spec = _quota_fallback_spec(model_spec)
                if fallback_spec:
                    logger.warning(
                        f"[{self.meta.name}/{model_spec}] Quota exhausted — falling back to {fallback_spec}"
                    )
                    existing_sandbox = solver.sandbox
                    if existing_sandbox is None:
                        raise RuntimeError("Quota fallback has no sandbox to transfer")
                    # Transfer ownership before stopping the old solver. Store the
                    # fallback in shared state first so cancellation cleanup targets it.
                    fallback = cast(
                        SolverProtocol,
                        self._create_pydantic_solver(
                            fallback_spec, sandbox=existing_sandbox, owns_sandbox=True
                        ),
                    )
                    self.solvers[model_spec] = fallback
                    try:
                        solver.sandbox = None  # type: ignore[assignment]
                        await self._finish_cleanup(solver.stop())
                    except BaseException:
                        await self._finish_cleanup(fallback.stop())
                        raise
                    solver = fallback
                    if self.cancel_event.is_set():
                        break
                    await solver.start()
                    continue
                # No fallback available, treat as error
                break

            if result.status in (GAVE_UP, ERROR):
                if result.step_count == 0 and result.cost_usd == 0:
                    logger.warning(
                        f"[{self.meta.name}/{model_spec}] Broken (0 steps, $0) — not bumping"
                    )
                    break

                # Track consecutive errors — stop after 3 in a row
                if result.status == ERROR:
                    consecutive_errors += 1
                    if consecutive_errors >= 3:
                        logger.warning(
                            f"[{self.meta.name}/{model_spec}] {consecutive_errors} consecutive errors — giving up"
                        )
                        break
                else:
                    consecutive_errors = 0

                bump_count += 1
                # Cooldown between bumps — check cancellation during wait
                try:
                    await asyncio.wait_for(
                        self.cancel_event.wait(),
                        timeout=min(bump_count * 30, 300),
                    )
                    break  # cancelled during cooldown
                except TimeoutError:
                    pass  # cooldown elapsed, proceed with bump
                insights = self._gather_sibling_insights(model_spec)
                solver.bump(insights)
                logger.info(f"[{self.meta.name}/{model_spec}] Bumped ({bump_count}), resuming")
                continue

        return result, solver

    async def run(self) -> SolverResult | None:
        """Run solvers until CTFd confirms a solve or one returns a dry-run flag."""
        if self.cancel_event.is_set():
            return None
        self._solver_tasks = {
            asyncio.create_task(self._run_solver(spec), name=f"solver-{spec}")
            for spec in self.model_specs
        }
        confirmed_waiter = asyncio.create_task(self._confirmed_event.wait())
        pending = set(self._solver_tasks)
        try:
            while pending and not self.cancel_event.is_set():
                done, pending_with_signal = await asyncio.wait(
                    pending | {confirmed_waiter}, return_when=asyncio.FIRST_COMPLETED
                )
                pending = pending_with_signal - {confirmed_waiter}
                if self._confirmed_event.is_set():
                    self.winner = self._confirmed_result
                    break
                for task in done - {confirmed_waiter}:
                    try:
                        result = task.result()
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        logger.exception("[%s] Solver task failed", self.meta.name)
                        continue
                    solver_result = cast(SolverResult | None, result)
                    if solver_result and solver_result.status == FLAG_FOUND:
                        self.winner = solver_result
                        break
                if self.winner:
                    break
            return self.winner
        finally:
            self.kill()
            confirmed_waiter.cancel()
            drain = asyncio.create_task(self._drain_solver_tasks(confirmed_waiter))
            interrupted = False
            while not drain.done():
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    interrupted = True
                    self.kill()
            await drain
            if interrupted:
                raise asyncio.CancelledError from None

    async def _drain_solver_tasks(self, confirmed_waiter: asyncio.Task) -> None:
        await asyncio.gather(confirmed_waiter, return_exceptions=True)
        results = await asyncio.gather(*self._solver_tasks, return_exceptions=True)
        failures = [
            result
            for result in results
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError)
        ]
        if failures:
            raise RuntimeError(f"{self.meta.name}: solver cleanup failed") from failures[0]
        self._solver_tasks.clear()

    def kill(self) -> None:
        """Stop running solver work; run() waits for cleanup before returning."""
        self.cancel_event.set()
        for task in self._solver_tasks - self._stopping_tasks:
            if not task.done():
                task.cancel()

    def get_status(self) -> dict:
        """Get per-agent progress and findings."""
        return {
            "challenge": self.meta.name,
            "cancelled": self.cancel_event.is_set(),
            "winner": self.winner.flag if self.winner else None,
            "agents": {
                spec: {
                    "findings": self.findings.get(spec, ""),
                    "status": "running"
                    if spec in self.solvers and not self.cancel_event.is_set()
                    else ("won" if self.winner and self.winner.flag else "finished"),
                }
                for spec in self.model_specs
            },
        }
