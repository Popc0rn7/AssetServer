"""Environment setup helpers."""

import os

from pathlib import Path

from dotenv import load_dotenv


def load_project_env() -> None:
    """Load repository .env values without overriding the caller's environment."""
    repo_root = Path(__file__).resolve().parents[2]
    load_dotenv(repo_root / ".env", override=False)


def configure_openai_agents_defaults() -> None:
    """Set conservative Agents SDK defaults for OpenAI-compatible endpoints."""
    if os.environ.get("OPENAI_BASE_URL") and not os.environ.get("OPENAI_AGENTS_API"):
        os.environ["OPENAI_AGENTS_API"] = "chat_completions"

    if os.environ.get("OPENAI_BASE_URL") and not os.environ.get(
        "OPENAI_AGENTS_DISABLE_TRACING"
    ):
        os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"

    agents_api = os.environ.get("OPENAI_AGENTS_API")
    if agents_api:
        from agents import set_default_openai_api

        set_default_openai_api(agents_api)

    if os.environ.get("OPENAI_AGENTS_DISABLE_TRACING"):
        from agents.tracing import set_tracing_disabled

        set_tracing_disabled(True)
