"""Contratti della config unica: Settings + alias di modulo (no orchestrator)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lavora_e_guida import config as config_mod
from lavora_e_guida.config import (
    GEMINI_MODEL,
    INDEX_ROOT,
    OLLAMA_MODEL,
    OLLAMA_URL,
    PROJECT_ROOT,
    TELEMETRY_DB,
    WORKSPACE_ROOT,
    Settings,
    get_settings,
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
    """Default runtime: Desktop Ollama_test, ollama_lab nel repo, Gemini flash."""
    monkeypatch.delenv("WORKSPACE_ROOT", raising=False)
    monkeypatch.delenv("INDEX_ROOT", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    settings = Settings(_env_file=None)
    assert settings.workspace_root == Path("/mnt/c/Users/User/Desktop/Ollama_test")
    assert settings.index_root == PROJECT_ROOT / "ollama_lab"
    assert settings.gemini_model == "gemini-3.5-flash"
    assert settings.telemetry_db == settings.index_root / "telemetry.db"


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
