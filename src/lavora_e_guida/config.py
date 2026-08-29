"""Settings di processo: ambiente / `.env` + alias di modulo per i tool.

Fonte unica dopo la fusione con il lab FS: workspace Desktop, indice RAG
nel repo, modelli Ollama/Gemini. I tool importano gli alias (`WORKSPACE_ROOT`,
…) così i test possono monkeypatchare l'attributo sul modulo tool, come oggi.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Repo root: `src/lavora_e_guida/config.py` → parents[2].
# Stato locale nel repo (`runtime/`: indice RAG, telemetria, token Gmail).
# I file utente restano sul Desktop (`Ollama_test`).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Nome cartella INDEX_ROOT di default: non è più `ollama_lab` (troppo legato a un solo LLM).
DEFAULT_INDEX_DIRNAME = "runtime"

# Cartella Gmail sul Desktop (`WORKSPACE_ROOT/email_attachments/YYYY-MM-DD/`).
# Non riusare `inbox/`: quella resta per i PDF importati a mano dal master.
EMAIL_ATTACHMENTS_DIRNAME = "email_attachments"

# Directory di primo livello sotto il workspace: rglob RAG e resolver RapidFuzz
# le ignorano, così `find_file` non mescola fatture scaricate e note.
INDEX_SKIP_DIRNAMES: frozenset[str] = frozenset({EMAIL_ATTACHMENTS_DIRNAME})


def is_index_skipped_rel(rel: Path | str) -> bool:
    """True se il path relativo al workspace sta sotto una cartella non indicizzabile.

    Solo il primo segmento: `email_attachments/2026-08-19/fattura.pdf` è skip;
    `notes/email_attachments.txt` resta visibile (è una nota, non la cartella Gmail).
    """
    # Path() accetta stringa posix o Path già relativo; parts[0] è la top-level dir.
    parts = Path(rel).parts
    return bool(parts) and parts[0] in INDEX_SKIP_DIRNAMES


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
    # Ricerca web (`--agent web`): chiave `tvly-…` di app.tavily.com.
    # Come GEMINI_API_KEY resta opzionale nel modello: il fail-fast è nella CLI,
    # così importare Settings non esplode per gli altri agenti che non cercano sul web.
    tavily_api_key: str | None = None

    # File utente (note, PDF, create/read/append) sul Desktop Windows montato in WSL.
    # Path traversal oltre questo root è rifiutato dagli tool FS.
    workspace_root: Path = Path("/mnt/c/Users/User/Desktop/Ollama_test")
    # Stato locale nel repo (non sul Desktop): SQLite + Chroma, telemetry.db, gmail_token.json.
    # I path in DB restano relativi a `workspace_root`.
    index_root: Path = PROJECT_ROOT / DEFAULT_INDEX_DIRNAME

    # Gmail OAuth (consenso a tavolino, mai nel loop vocale).
    # Id/secret del client Desktop e mailbox attesa: stesso pattern di GEMINI_API_KEY (.env).
    gmail_client_id: str | None = None
    gmail_client_secret: str | None = None
    # Indirizzo che deve coincidere col profile Gmail dopo il consenso (account sbagliato → errore).
    gmail_user: str | None = None
    # Refresh token su disco: Google riscrive il JSON al refresh, quindi non va nel .env.
    # None / stringa vuota → INDEX_ROOT/gmail_token.json; override con GMAIL_TOKEN_FILE.
    gmail_token_file: Path | None = None

    @field_validator("gmail_token_file", mode="before")
    @classmethod
    def _blank_gmail_token_file(cls, value: object) -> object:
        # GMAIL_TOKEN_FILE= (vuoto nel .env) deve usare il default, non Path("").
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _default_gmail_token_file(self) -> Settings:
        # Un solo file token per il client Desktop; vive nell'indice accanto a files.db.
        if self.gmail_token_file is None:
            self.gmail_token_file = Path(self.index_root) / "gmail_token.json"
        return self

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
# Data workspace Desktop (Ollama_test) e stato locale nel repo (runtime/).
WORKSPACE_ROOT = _cfg.workspace_root
INDEX_ROOT = _cfg.index_root
# Telemetria token STT: file dedicato, non files.db.
TELEMETRY_DB = _cfg.telemetry_db
# Alias storici del lab: stessa URL/modello già in Settings.ollama_*.
OLLAMA_URL = _cfg.ollama_base_url
OLLAMA_MODEL = _cfg.ollama_model
GEMINI_MODEL = _cfg.gemini_model
