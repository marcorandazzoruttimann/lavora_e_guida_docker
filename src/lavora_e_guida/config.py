"""Settings di processo: ambiente / `.env` + alias di modulo per i tool.

Fonte unica dopo la fusione con il lab FS: workspace Desktop, indice RAG
nel repo, modelli Ollama/Gemini. I tool importano gli alias (`WORKSPACE_ROOT`,
…) così i test possono monkeypatchare l'attributo sul modulo tool, come oggi.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root: `src/lavora_e_guida/config.py` → parents[2].
# I path runtime (`ollama_lab/`, Desktop `Ollama_test`) restano invariati.
PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
    """Runtime configuration for the vocal agent (audio, LLM, FS/RAG roots)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

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
    # Cloud: `--llm gemini` (override CLI `--model` o env GEMINI_MODEL).
    gemini_model: str = "gemini-3.5-flash"

    # File utente (note, PDF, create/read/append) sul Desktop Windows montato in WSL.
    # Path traversal oltre questo root è rifiutato dagli tool FS.
    workspace_root: Path = Path("/mnt/c/Users/User/Desktop/Ollama_test")
    # SQLite + Chroma nel progetto Cursor (non sul Desktop).
    # I path in DB restano relativi a `workspace_root`.
    index_root: Path = PROJECT_ROOT / "ollama_lab"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def telemetry_db(self) -> Path:
        # File dedicato accanto a files.db; non mescolare i token STT con l'indice RAG.
        return Path(self.index_root) / "telemetry.db"

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


# Snapshot env/.env all'import: i tool fanno `from lavora_e_guida.config import WORKSPACE_ROOT`
# e legano il valore sul proprio modulo. I test monkeypatchano quello, non Settings.
_cfg = get_settings()
# Data workspace Desktop (Ollama_test) e indice RAG nel repo (ollama_lab/).
WORKSPACE_ROOT = _cfg.workspace_root
INDEX_ROOT = _cfg.index_root
# Telemetria token STT: file dedicato, non files.db.
TELEMETRY_DB = _cfg.telemetry_db
# Alias storici del lab: stessa URL/modello già in Settings.ollama_*.
OLLAMA_URL = _cfg.ollama_base_url
OLLAMA_MODEL = _cfg.ollama_model
GEMINI_MODEL = _cfg.gemini_model
