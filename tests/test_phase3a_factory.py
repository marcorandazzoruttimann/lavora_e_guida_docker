"""Test Phase 3A: contratto BaseOrchestrator + factory lazy esclusiva."""

from __future__ import annotations

import builtins
import sys
from types import ModuleType
from typing import Any

import pytest

from lavora_e_guida.config import Settings
from lavora_e_guida.orchestrator.base import (
    BaseOrchestrator,
    OrchestratorConfigurationError,
    OrchestratorDependencyError,
)
from lavora_e_guida.orchestrator.factory import (
    SUPPORTED_FRAMEWORKS,
    create,
    validate_framework,
)


def _purge_orchestrator_adapter_modules() -> None:
    """Rimuove adapter dalla cache import per test di isolamento."""
    for name in list(sys.modules):
        if name.endswith(("crewai_adapter", "autogen_adapter")):
            del sys.modules[name]


@pytest.fixture(autouse=True)
def _clean_adapter_imports() -> None:
    """Ogni test parte senza moduli adapter già caricati."""
    _purge_orchestrator_adapter_modules()
    yield
    _purge_orchestrator_adapter_modules()


class _FakeCrewAIAdapter(BaseOrchestrator):
    """Stand-in locale: evita dipendenza pip crewai nei test factory."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings

    @property
    def framework_name(self) -> str:
        return "crewai"

    def run(self, user_text: str) -> str:
        return f"fake-crewai:{user_text}"


class _FakeAutoGenAdapter(BaseOrchestrator):
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings

    @property
    def framework_name(self) -> str:
        return "autogen"

    def run(self, user_text: str) -> str:
        return f"fake-autogen:{user_text}"


def test_supported_frameworks_match_literal() -> None:
    assert set(SUPPORTED_FRAMEWORKS) == {"crewai", "autogen"}


def test_validate_framework_accepts_case_insensitive() -> None:
    assert validate_framework("CrewAI") == "crewai"
    assert validate_framework(" AUTOGEN ") == "autogen"


def test_validate_framework_rejects_unknown() -> None:
    with pytest.raises(OrchestratorConfigurationError, match="non supportato"):
        validate_framework("langgraph")


def test_create_crewai_imports_only_crewai_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Factory crewai non deve mai importare autogen_adapter."""
    imported: list[str] = []
    original_import = builtins.__import__

    def tracking_import(
        name: str,
        globals_: Any = None,
        locals_: Any = None,
        fromlist: Any = (),
        level: int = 0,
    ):
        imported.append(name)
        return original_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", tracking_import)

    fake_module = ModuleType("lavora_e_guida.orchestrator.crewai_adapter")
    fake_module.CrewAIAdapter = _FakeCrewAIAdapter  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules,
        "lavora_e_guida.orchestrator.crewai_adapter",
        fake_module,
    )

    orch = create(Settings(orchestrator_framework="crewai"))
    assert isinstance(orch, _FakeCrewAIAdapter)
    assert orch.framework_name == "crewai"
    assert "lavora_e_guida.orchestrator.autogen_adapter" not in imported
    assert "lavora_e_guida.orchestrator.crewai_adapter" in imported


def test_create_autogen_imports_only_autogen_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    imported: list[str] = []
    original_import = builtins.__import__

    def tracking_import(
        name: str,
        globals_: Any = None,
        locals_: Any = None,
        fromlist: Any = (),
        level: int = 0,
    ):
        imported.append(name)
        return original_import(name, globals_, locals_, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", tracking_import)

    fake_module = ModuleType("lavora_e_guida.orchestrator.autogen_adapter")
    fake_module.AutoGenAdapter = _FakeAutoGenAdapter  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules,
        "lavora_e_guida.orchestrator.autogen_adapter",
        fake_module,
    )

    orch = create(Settings(orchestrator_framework="autogen"))
    assert isinstance(orch, _FakeAutoGenAdapter)
    assert orch.framework_name == "autogen"
    assert "lavora_e_guida.orchestrator.crewai_adapter" not in imported
    assert "lavora_e_guida.orchestrator.autogen_adapter" in imported


def test_create_without_optional_deps_raises_dependency_error() -> None:
    """Env di default (crewai) senza extra pip → messaggio installazione."""
    settings = Settings(orchestrator_framework="crewai")
    with pytest.raises(OrchestratorDependencyError, match="CrewAI non installato"):
        create(settings)


def test_crewai_adapter_run_stub_when_dep_mocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Con crewai mockato, run() rispetta contratto testo→testo."""
    fake_crewai = ModuleType("crewai")
    monkeypatch.setitem(sys.modules, "crewai", fake_crewai)

    from lavora_e_guida.orchestrator.crewai_adapter import CrewAIAdapter

    adapter = CrewAIAdapter(Settings(orchestrator_framework="crewai"))
    assert adapter.framework_name == "crewai"
    assert "Phase 3B" in adapter.run("ciao")
    assert adapter.run("   ") == "Non ho ricevuto testo da elaborare."


def test_autogen_adapter_run_stub_when_dep_mocked(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_autogen = ModuleType("autogen_agentchat")
    monkeypatch.setitem(sys.modules, "autogen_agentchat", fake_autogen)

    from lavora_e_guida.orchestrator.autogen_adapter import AutoGenAdapter

    adapter = AutoGenAdapter(Settings(orchestrator_framework="autogen"))
    assert adapter.framework_name == "autogen"
    assert "Phase 3B" in adapter.run("prova")
    assert adapter.run("") == "Non ho ricevuto testo da elaborare."


def test_create_reads_settings_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """create() senza argomenti usa get_settings() — smoke con mock adapter."""
    fake_module = ModuleType("lavora_e_guida.orchestrator.crewai_adapter")
    fake_module.CrewAIAdapter = _FakeCrewAIAdapter  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules,
        "lavora_e_guida.orchestrator.crewai_adapter",
        fake_module,
    )

    sentinel = Settings(orchestrator_framework="crewai", audio_driver="mock")
    monkeypatch.setattr(
        "lavora_e_guida.orchestrator.factory.get_settings",
        lambda: sentinel,
    )

    orch = create()
    assert isinstance(orch, _FakeCrewAIAdapter)
    assert orch._settings is sentinel
