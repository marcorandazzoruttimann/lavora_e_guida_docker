"""Application settings loaded from environment / `.env`."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


def discover_windows_host() -> str:
    """Return the WSL2 Windows host IP from `/etc/resolv.conf`, or localhost."""
    resolv = Path("/etc/resolv.conf")
    try:
        for line in resolv.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("nameserver"):
                parts = stripped.split()
                if len(parts) >= 2:
                    return parts[1]
    except OSError:
        pass
    return "127.0.0.1"


class Settings(BaseSettings):
    """Runtime configuration for the vocal agent orchestrator."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    orchestrator_framework: Literal["crewai", "autogen"] = "crewai"
    audio_driver: Literal["mock", "http"] = "mock"
    ollama_model: str = "qwen2.5:3b"
    ollama_base_url: str = "http://127.0.0.1:11434"
    windows_host: str | None = Field(
        default=None,
        description="Override for Windows host IP; empty/None → resolv.conf discovery",
    )
    windows_audio_port: int = 8765
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def resolved_windows_host(self) -> str:
        host = (self.windows_host or "").strip()
        return host if host else discover_windows_host()

    @computed_field  # type: ignore[prop-decorator]
    @property
    def audio_bridge_url(self) -> str:
        return f"http://{self.resolved_windows_host}:{self.windows_audio_port}"


@lru_cache
def get_settings() -> Settings:
    """Cached settings singleton for the process."""
    return Settings()
