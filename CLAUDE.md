# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Setup and commands

- Python 3.14+, `uv`, and Docker are required. Install the package and dev tools with `uv sync --group dev`; build the wheel with `uv build`.
- Build the solver image before running a challenge: `docker build -f sandbox/Dockerfile.sandbox -t ctf-sandbox .`. Copy `.env.example` to `.env` and configure the CTFd connection and relevant provider credentials. The Claude and Codex backends also need their respective CLIs available.
- Run the continuous coordinator: `uv run ctf-solve --ctfd-url https://ctf.example.com --ctfd-token TOKEN --challenges-dir challenges --max-challenges 10 -v`. Use `--coordinator claude` (default) or `--coordinator codex` and `--no-submit` to avoid submitting flags.
- Run one downloaded challenge: `uv run ctf-solve --challenge challenges/CHALLENGE_SLUG --no-submit`; the directory must contain `metadata.yml`. Override the model lineup with repeated `--models provider/model` options.
- Send an operator message to a running coordinator: `uv run ctf-msg --port PORT "message"`; use the port logged by the coordinator, or specify `--msg-port PORT` when starting it.
- Lint: `uv run ruff check .`; check formatting: `uv run ruff format --check .`; type-check: `uv run ty check backend`; tests: `uv run pytest`. Run a single test with `uv run pytest path/to/test_file.py::test_name`. `pytest` and `pytest-asyncio` are declared in the dev group, but the repository currently has no tracked tests.

## Architecture

- `backend/cli.py` is the entry point for `ctf-solve` and `ctf-msg`. Without `--challenge`, it starts a long-running Claude or Codex coordinator; with `--challenge`, it runs one `ChallengeSwarm` directly. `backend/config.py` loads environment and `.env` settings, while `backend/models.py` defines the default model specs and resolves API-backed models.
- `backend/agents/coordinator_loop.py` owns the shared async competition loop: `backend/poller.py` polls CTFd for new/solved challenges, the loop auto-spawns or stops swarms, feeds events and solver/operator messages to the coordinator, and tracks results. `claude_coordinator.py` and `codex_coordinator.py` supply backend-specific LLM turns; `coordinator_core.py` implements their shared operations, including challenge retrieval, swarm management, and guidance. `backend/deps.py` carries shared runtime state.
- `backend/ctfd.py` handles CTFd authentication, fetching, challenge downloads, and flag submission. Challenge directories contain `metadata.yml` and optional `distfiles/`; `backend/prompts.py` parses metadata and builds solver prompts. The separate `pull_challenges.py` can pre-download this layout; it imports `aiohttp` and `bs4`, which are not direct dependencies in `pyproject.toml`.
- `backend/agents/swarm.py` races one solver per model spec on each challenge, shares findings via `backend/message_bus.py`, and centralizes deduplicated/cooldown-gated flag submission. A confirmed flag cancels competing solvers; a quota-exhausted subscription solver can hand its existing sandbox to an API-backed fallback. Provider-specific solvers are `claude_solver.py` (Claude Agent SDK), `codex_solver.py` (Codex App Server), and `solver.py` (Pydantic AI for API-backed providers); they share the result protocol in `backend/solver_base.py`.
- Each solver uses `backend/sandbox.py` to start a Docker container with challenge distfiles and metadata mounted read-only and a writable workspace. `backend/tools/` exposes execution, files, web, vision, and flag tools; `sandbox/Dockerfile.sandbox` and `sandbox/sandbox-tools.txt` define the image. `backend/prompts.py` rewrites localhost challenge endpoints to `host.docker.internal` for container access. `backend/tracing.py`, `backend/loop_detect.py`, and `backend/cost_tracker.py` handle traces, repeated behavior, and usage accounting.
