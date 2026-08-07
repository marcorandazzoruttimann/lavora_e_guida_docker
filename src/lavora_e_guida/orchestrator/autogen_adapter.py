"""Adapter AutoGen — Phase 3A: wiring + stub; Phase 3B: agenti e tool reali.

Simmetrico a ``crewai_adapter``: importato solo con ``ORCHESTRATOR_FRAMEWORK=autogen``;
``autogen_agentchat`` si carica in ``__init__`` per non penalizzare chi usa CrewAI.
"""

from __future__ import annotations

from lavora_e_guida.config import Settings, get_settings
from lavora_e_guida.orchestrator.base import BaseOrchestrator, OrchestratorDependencyError

_AUTOGEN_INSTALL_HINT = "pip install 'lavora_e_guida[autogen]'"


def _ensure_autogen_installed() -> None:
    """Verifica extra autogen; errore user-friendly se manca il pacchetto."""
    try:
        import autogen_agentchat  # noqa: F401  # Phase 3B: agent chat API.
    except ImportError as exc:
        raise OrchestratorDependencyError(
            f"AutoGen non installato. Esegui: {_AUTOGEN_INSTALL_HINT}"
        ) from exc


class AutoGenAdapter(BaseOrchestrator):
    """Implementazione BaseOrchestrator delegata ad AutoGen (stub fino a 3B)."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings if settings is not None else get_settings()
        _ensure_autogen_installed()

    @property
    def framework_name(self) -> str:
        return "autogen"

    def run(self, user_text: str) -> str:
        """Stub Phase 3A: eco strutturato fino all'integrazione agent chat."""
        cleaned = user_text.strip()
        if not cleaned:
            return "Non ho ricevuto testo da elaborare."
        return (
            f"[AutoGen stub] Ho ricevuto la richiesta. "
            f"Phase 3B collegherà agenti e tool. Input: {cleaned!r}"
        )
