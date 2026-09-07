"""Contratti della config unica: Settings + alias di modulo (no orchestrator)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lavora_e_guida import config as config_mod
from lavora_e_guida.config import (
    DEFAULT_INDEX_DIRNAME,
    EMAIL_ATTACHMENTS_DIRNAME,
    GEMINI_MODEL,
    INDEX_ROOT,
    OLLAMA_MODEL,
    OLLAMA_URL,
    PROJECT_ROOT,
    TELEMETRY_DB,
    WORKSPACE_ROOT,
    Settings,
    get_settings,
    is_index_skipped_rel,
)


def test_project_root_is_repo_root() -> None:
    """parents[2] da src/lavora_e_guida/config.py deve coincidere col repo."""
    assert PROJECT_ROOT == Path(config_mod.__file__).resolve().parents[2]
    assert (PROJECT_ROOT / "pyproject.toml").is_file()


def test_settings_has_no_orchestrator_framework() -> None:
    """Il campo è stato rimosso: gli adapter non vivono più in Settings."""
    assert "orchestrator_framework" not in Settings.model_fields
    settings = Settings(_env_file=None)
    assert not hasattr(settings, "orchestrator_framework")


def test_settings_fs_and_gemini_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default runtime: Desktop Ollama_test, runtime/ nel repo, Gemini flash."""
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("INDEX_ROOT", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.workspace_root == Path("/mnt/c/Users/User/Desktop/Ollama_test")
    assert settings.index_root == PROJECT_ROOT / DEFAULT_INDEX_DIRNAME
    assert settings.gemini_model == "gemini-3.5-flash"
    assert settings.telemetry_db == settings.index_root / "telemetry.db"


def test_audio_timeout_defaults_split_listen_and_speak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Listen 300s (wake+dettatura), speak 120s; non un tetto unico da 60s."""
    monkeypatch.delenv("AUDIO_LISTEN_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("AUDIO_SPEAK_TIMEOUT_SEC", raising=False)
    settings = Settings(_env_file=None)
    assert settings.audio_listen_timeout_sec == 300.0
    assert settings.audio_speak_timeout_sec == 120.0


def test_env_overrides_audio_timeouts(monkeypatch: pytest.MonkeyPatch) -> None:
    """AUDIO_LISTEN_TIMEOUT_SEC / AUDIO_SPEAK_TIMEOUT_SEC vincono sui default."""
    monkeypatch.setenv("AUDIO_LISTEN_TIMEOUT_SEC", "420")
    monkeypatch.setenv("AUDIO_SPEAK_TIMEOUT_SEC", "90")
    settings = Settings(_env_file=None)
    assert settings.audio_listen_timeout_sec == 420.0
    assert settings.audio_speak_timeout_sec == 90.0


def test_module_aliases_match_settings_singleton() -> None:
    """I tool importano gli alias: devono coincidere col singleton all'import."""
    cfg = get_settings()
    assert WORKSPACE_ROOT == cfg.workspace_root
    assert INDEX_ROOT == cfg.index_root
    assert TELEMETRY_DB == cfg.telemetry_db
    assert OLLAMA_URL == cfg.ollama_base_url
    assert OLLAMA_MODEL == cfg.ollama_model
    assert GEMINI_MODEL == cfg.gemini_model


def test_env_overrides_workspace_and_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """WORKSPACE_ROOT / INDEX_ROOT da env vincono sui default (senza .env)."""
    data_ws = tmp_path / "desktop"
    index = tmp_path / "index"
    monkeypatch.setenv("WORKSPACE_ROOT", str(data_ws))
    monkeypatch.setenv("INDEX_ROOT", str(index))
    settings = Settings(_env_file=None)
    assert settings.workspace_root == data_ws
    assert settings.index_root == index
    assert settings.telemetry_db == index / "telemetry.db"


def test_env_overrides_gemini_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_MODEL", "gemini-2.0-flash")
    settings = Settings(_env_file=None)
    assert settings.gemini_model == "gemini-2.0-flash"


def test_gmail_settings_default_token_under_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Senza GMAIL_TOKEN_FILE il refresh token vive in INDEX_ROOT/gmail_token.json."""
    monkeypatch.delenv("GMAIL_CLIENT_ID", raising=False)
    monkeypatch.delenv("GMAIL_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_TOKEN_FILE", raising=False)
    monkeypatch.delenv("INDEX_ROOT", raising=False)
    settings = Settings(_env_file=None)
    assert settings.gmail_client_id is None
    assert settings.gmail_client_secret is None
    assert settings.gmail_user is None
    assert settings.gmail_token_file == settings.index_root / "gmail_token.json"


def test_gmail_token_file_follows_index_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Se cambia INDEX_ROOT e GMAIL_TOKEN_FILE è vuoto, il token segue l'indice."""
    index = tmp_path / "index"
    monkeypatch.setenv("INDEX_ROOT", str(index))
    monkeypatch.setenv("GMAIL_TOKEN_FILE", "")
    settings = Settings(_env_file=None)
    assert settings.gmail_token_file == index / "gmail_token.json"


def test_gmail_env_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Client, mailbox e path token da env vincono sui default (senza .env)."""
    token = tmp_path / "custom_gmail_token.json"
    monkeypatch.setenv("GMAIL_CLIENT_ID", "desktop-client-id")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "desktop-client-secret")
    monkeypatch.setenv("GMAIL_USER", "account@gmail.com")
    monkeypatch.setenv("GMAIL_TOKEN_FILE", str(token))
    settings = Settings(_env_file=None)
    assert settings.gmail_client_id == "desktop-client-id"
    assert settings.gmail_client_secret == "desktop-client-secret"
    assert settings.gmail_user == "account@gmail.com"
    assert settings.gmail_token_file == token


def test_email_attachments_dir_is_index_skipped() -> None:
    """Solo la top-level Gmail è skip RAG: una nota omonima resta indicizzabile."""
    # Identificatore inglese condiviso con save_attachments (stesso nome cartella).
    assert EMAIL_ATTACHMENTS_DIRNAME == "email_attachments"
    # Path relativo tipico del tool: giorno civile + filename sanitizzato.
    assert is_index_skipped_rel("email_attachments/2026-08-19/fattura.pdf")
    assert is_index_skipped_rel(Path("email_attachments/x.txt"))
    # Note e inbox restano nel rglob: non è uno skip globale sul token nel nome.
    assert not is_index_skipped_rel("notes/spesa.txt")
    assert not is_index_skipped_rel("notes/email_attachments.txt")
    assert not is_index_skipped_rel("inbox/fattura.pdf")
    assert not is_index_skipped_rel("")
