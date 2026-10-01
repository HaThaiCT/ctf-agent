# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Development commands

- Requires Python 3.14+, `uv`, Docker, and the `codex` CLI when using Codex backends. Install the package and dev dependencies with `uv sync`.
- Build the Python package: `uv build`. Build the solver image separately: `docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .`.
- Lint: `uv run ruff check .`; format check: `uv run ruff format --check .`; type check: `uv run ty check`.
- Run tests: `uv run pytest`. Run one test: `uv run pytest tests/test_solver_concurrency.py::test_external_solve_interrupts_blocked_solvers_and_drains_cleanup`.
- Configure CTFd credentials in `.env` using `.env.example` as a template. Run all challenges with `uv run ctf-solve --challenges-dir challenges --max-challenges 10 -v`; use `--coordinator codex` for the alternate coordinator, `--no-submit` to avoid flag submissions, or `--challenge challenges/<slug>` for a single challenge. Prefer `.env` over credential-bearing CLI flags so tokens do not enter shell history or process listings. `uv run ctf-msg --port <port> 'message'` sends a hint to a running coordinator; `--msg-port` fixes its otherwise auto-selected port.
- `pull_challenges.py` is a separate CTFd downloader for creating local challenge directories; unlike the packaged backend it imports `aiohttp` and `bs4`, which are not listed in `pyproject.toml` dependencies.

## Architecture

- `backend/cli.py` selects single-challenge mode or the long-running coordinator, configures container concurrency, and cleans up orphan containers. `backend/config.py` loads settings from environment variables and `.env`; `backend/models.py` defines default solver specs, provider routing, and API-model settings.
- In full mode, either Claude Agent SDK or Codex app-server drives the coordinator. Both adapters in `backend/agents/{claude,codex}_coordinator.py` expose the same operations from `coordinator_core.py` and run through `coordinator_loop.py`. The shared loop polls CTFd every 30 seconds, auto-spawns swarms for unsolved/new challenges, handles solved/finished events and operator/solver messages, and shuts down swarms on exit. `backend/ctfd.py` handles CTFd authentication, fetching, flag submission, and downloading challenge files as `metadata.yml` plus `distfiles/`; `backend/prompts.py` reads that format to build solver prompts.
- `backend/agents/swarm.py` races one solver per configured model on a challenge. It routes `claude-sdk/*` to `ClaudeSolver`, `codex/*` to `CodexSolver`, and API-backed models to the Pydantic AI `Solver`. It owns the shared cancellation event, sibling-finding message bus, quota fallback, retry/bump loop, and lock-protected flag deduplication/cooldowns. A confirmed flag stops the other solvers; `--no-submit` treats solver output as sufficient without CTFd confirmation.
- Each solver uses its own `DockerSandbox` (`backend/sandbox.py`): challenge `distfiles/` and `metadata.yml` are mounted read-only, with a separate writable workspace. `backend/tools/core.py` contains SDK-neutral sandbox, web, image, and submission operations, adapted by the Pydantic AI tools and Codex dynamic tools. The Claude SDK solver instead redirects native Bash through a hook into its container and intercepts `submit_flag` commands. Solver traces, loop detection, and cost accounting live in `backend/tracing.py`, `backend/loop_detect.py`, and `backend/cost_tracker.py`; the coordinator reads traces to guide solvers.
