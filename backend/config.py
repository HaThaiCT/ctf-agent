"""Pydantic Settings — credentials from .env file + environment variables."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # CTFd
    ctfd_url: str = "http://localhost:8000"
    ctfd_user: str = "admin"
    ctfd_pass: str = "admin"
    ctfd_token: str = ""

    # API Keys
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""

    # Provider-specific (optional, for Bedrock/Azure/Zen fallback)
    aws_region: str = "us-east-1"
    aws_bearer_token: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    opencode_zen_api_key: str = ""

    # Optional per-solver gateway override; unset to inherit Claude Code's gateway.
    solver_router_url: str = ""
    solver_router_key: SecretStr = SecretStr("")
    solver_router_auth: Literal["bearer", "x-api-key"] = "bearer"
    solver_model: str = ""

    # Infra
    sandbox_image: str = "ctf-sandbox"
    max_concurrent_challenges: int = 10
    max_attempts_per_challenge: int = 3
    container_memory_limit: str = "16g"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @model_validator(mode="after")
    def validate_solver_router(self) -> Settings:
        if self.solver_router_url:
            url = urlsplit(self.solver_router_url)
            try:
                valid_port = url.port is None or url.port > 0
            except ValueError as exc:
                raise ValueError("SOLVER_ROUTER_URL has an invalid port") from exc
            if (
                url.scheme not in ("http", "https")
                or not url.hostname
                or not valid_port
                or "@" in url.netloc
                or url.query
                or url.fragment
                or any(char.isspace() for char in self.solver_router_url)
            ):
                raise ValueError(
                    "SOLVER_ROUTER_URL must be an HTTP(S) base URL without credentials or query"
                )
            if not self.solver_router_key.get_secret_value():
                raise ValueError("SOLVER_ROUTER_KEY is required when SOLVER_ROUTER_URL is set")
            if not self.solver_model:
                raise ValueError("SOLVER_MODEL is required when SOLVER_ROUTER_URL is set")
            parts = self.solver_model.split("/")
            if parts[0] != "claude-sdk" or len(parts) < 2 or any(not part for part in parts[1:]):
                raise ValueError("SOLVER_MODEL must be claude-sdk/<model-id>[/effort]")
        elif self.solver_router_key.get_secret_value():
            raise ValueError("SOLVER_ROUTER_URL is required when SOLVER_ROUTER_KEY is set")
        return self

    def selected_solver_models(self, cli_models: tuple[str, ...]) -> list[str]:
        """Choose CLI overrides, the configured solver, or the legacy model lineup."""
        from backend.models import DEFAULT_MODELS

        specs = (
            list(cli_models)
            if cli_models
            else [self.solver_model]
            if self.solver_model
            else list(DEFAULT_MODELS)
        )
        if self.solver_router_url and any(not spec.startswith("claude-sdk/") for spec in specs):
            import click

            raise click.UsageError("SOLVER_ROUTER_URL requires claude-sdk/* solver models")
        return specs

    def solver_sdk_env(self) -> dict[str, str]:
        """Environment overrides for the solver SDK subprocess, never the coordinator."""
        if not self.solver_router_url:
            return {}
        key = self.solver_router_key.get_secret_value()
        auth_var = (
            "ANTHROPIC_AUTH_TOKEN" if self.solver_router_auth == "bearer" else "ANTHROPIC_API_KEY"
        )
        base_url = self.solver_router_url.rstrip("/")
        # Claude Code appends /v1/messages itself. Accept the router's /v1
        # listing URL in settings, but pass its origin as the SDK base URL.
        if base_url.endswith("/v1"):
            base_url = base_url[:-3]
        return {
            "ANTHROPIC_BASE_URL": base_url,
            auth_var: key,
            # Override any inherited credential of the opposite header type.
            "ANTHROPIC_API_KEY"
            if auth_var == "ANTHROPIC_AUTH_TOKEN"
            else "ANTHROPIC_AUTH_TOKEN": "",
        }
