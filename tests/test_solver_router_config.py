"""Configuration behavior for Claude SDK solver model selection and gateway overrides."""

import click
import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from backend.config import Settings
from backend.models import effort_from_spec, model_id_from_spec


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    monkeypatch.setattr(Settings, "model_config", {"extra": "ignore"})
    for key in ("SOLVER_ROUTER_URL", "SOLVER_ROUTER_KEY", "SOLVER_ROUTER_AUTH", "SOLVER_MODEL"):
        monkeypatch.delenv(key, raising=False)


def test_router_config_selects_one_solver_and_keeps_secret_out_of_repr():
    settings = Settings(
        solver_router_url="http://127.0.0.1:20128/v1",
        solver_router_key="local-only-secret",
        solver_model="claude-sdk/ag/claude-sonnet-4-6",
    )

    assert settings.selected_solver_models(()) == ["claude-sdk/ag/claude-sonnet-4-6"]
    assert settings.solver_sdk_env() == {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:20128",
        "ANTHROPIC_AUTH_TOKEN": "local-only-secret",
        "ANTHROPIC_API_KEY": "",
    }
    assert "local-only-secret" not in repr(settings)


def test_cli_models_override_configured_default():
    settings = Settings(
        solver_router_url="http://127.0.0.1:20128/v1",
        solver_router_key="local-only-secret",
        solver_model="claude-sdk/ag/claude-sonnet-4-6",
    )

    assert settings.selected_solver_models(
        ("claude-sdk/ag/claude-sonnet-4-6", "claude-sdk/ag/claude-opus-4-6-thinking")
    ) == [
        "claude-sdk/ag/claude-sonnet-4-6",
        "claude-sdk/ag/claude-opus-4-6-thinking",
    ]
    with pytest.raises(click.UsageError, match="claude-sdk"):
        settings.selected_solver_models(("codex/gpt-5.4",))


@pytest.mark.parametrize(
    "url, key, model",
    [
        ("", "secret", "claude-sdk/ag/claude-sonnet-4-6"),
        ("http://127.0.0.1:20128/v1", "", "claude-sdk/ag/claude-sonnet-4-6"),
        ("http://127.0.0.1:20128/v1", "secret", ""),
        ("http://127.0.0.1:20128/v1", "secret", "codex/gpt-5.4"),
        ("http://user:pass@127.0.0.1:20128/v1", "secret", "claude-sdk/ag/claude-sonnet-4-6"),
        ("http://127.0.0.1:invalid/v1", "secret", "claude-sdk/ag/claude-sonnet-4-6"),
        ("http://@127.0.0.1:20128/v1", "secret", "claude-sdk/ag/claude-sonnet-4-6"),
    ],
)
def test_invalid_router_config_fails_before_startup(url, key, model):
    with pytest.raises(ValidationError):
        Settings(solver_router_url=url, solver_router_key=key, solver_model=model)


def test_router_model_id_survives_provider_prefix():
    assert model_id_from_spec("claude-sdk/ag/claude-sonnet-4-6") == "ag/claude-sonnet-4-6"
    assert model_id_from_spec("claude-sdk/claude-opus-4-6/max") == "claude-opus-4-6"


def test_three_solver_specs_keep_router_model_ids_and_independent_efforts():
    specs = (
        ("claude-sdk/ag/claude-sonnet-4-6/low", "ag/claude-sonnet-4-6", "low"),
        ("claude-sdk/ag/claude-sonnet-4-6/high", "ag/claude-sonnet-4-6", "high"),
        (
            "claude-sdk/ag/claude-opus-4-6-thinking/medium",
            "ag/claude-opus-4-6-thinking",
            "medium",
        ),
    )
    for spec, model_id, effort in specs:
        assert model_id_from_spec(spec) == model_id
        assert effort_from_spec(spec) == effort

    settings = Settings(solver_model="claude-sdk/ag/claude-sonnet-4-6")
    assert settings.selected_solver_models(tuple(spec for spec, _, _ in specs)) == [
        spec for spec, _, _ in specs
    ]


def test_x_api_key_router_does_not_inherit_bearer_credential():
    settings = Settings(
        solver_router_url="http://127.0.0.1:20128/v1/",
        solver_router_key="local-only-secret",
        solver_router_auth="x-api-key",
        solver_model="claude-sdk/ag/claude-sonnet-4-6",
    )

    assert settings.solver_sdk_env() == {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:20128",
        "ANTHROPIC_API_KEY": "local-only-secret",
        "ANTHROPIC_AUTH_TOKEN": "",
    }


def test_cli_uses_configured_solver_without_connecting_to_ctfd(monkeypatch):
    from backend.cli import main

    monkeypatch.setenv("SOLVER_ROUTER_URL", "http://127.0.0.1:20128/v1")
    monkeypatch.setenv("SOLVER_ROUTER_KEY", "local-only-secret")
    monkeypatch.setenv("SOLVER_MODEL", "claude-sdk/ag/claude-sonnet-4-6")

    async def fake_run_single(settings, challenge, model_specs, no_submit, max_challenges):
        assert model_specs == ["claude-sdk/ag/claude-sonnet-4-6"]
        assert no_submit is True

    monkeypatch.setattr("backend.cli._run_single", fake_run_single)
    result = CliRunner().invoke(main, ["--challenge", "challenges/example", "--no-submit"])

    assert result.exit_code == 0, result.output


def test_cli_runs_three_solver_specs_with_two_challenge_limit(monkeypatch):
    from backend.cli import main

    specs = [
        "claude-sdk/ag/claude-sonnet-4-6/low",
        "claude-sdk/ag/claude-sonnet-4-6/high",
        "claude-sdk/ag/claude-opus-4-6-thinking/medium",
    ]
    monkeypatch.setenv("SOLVER_MODEL", "claude-sdk/ag/claude-sonnet-4-6")
    received = []

    async def fake_run_coordinator(
        settings,
        model_specs,
        challenges_dir,
        no_submit,
        coordinator_model,
        coordinator,
        max_challenges,
        msg_port,
    ):
        received.append(
            (
                settings.max_concurrent_challenges,
                model_specs,
                coordinator_model,
                coordinator,
                max_challenges,
                no_submit,
            )
        )

    monkeypatch.setattr("backend.cli._run_coordinator", fake_run_coordinator)
    args = ["--coordinator-model", "ag/claude-sonnet-4-6", "--max-challenges", "2", "--no-submit"]
    for spec in specs:
        args.extend(["--models", spec])
    result = CliRunner().invoke(main, args)

    assert result.exit_code == 0, result.output
    assert received == [(2, specs, "ag/claude-sonnet-4-6", "claude", 2, True)]
