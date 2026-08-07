"""Adapter CrewAI — Phase 3A: wiring + stub; Phase 3B: agenti e tool reali.

Il modulo viene importato **solo** se ``ORCHESTRATOR_FRAMEWORK=crewai``;
``crewai`` stesso si importa dentro ``__init__`` così l'extra pip mancante
produce un errore chiaro senza rompere chi usa autogen.
"""

from __future__ import annotations

from lavora_e_guida.config import Settings, get_settings
from lavora_e_guida.orchestrator.base import BaseOrchestrator, OrchestratorDependencyError

# Messaggio condiviso con autogen_adapter: stesso formato per TTS futuro.
_CREWAI_INSTALL_HINT = "pip install 'lavora_e_guida[crewai]'"


def _ensure_crewai_installed() -> None:
    """Verifica presenza extra opzionale; alza errore parlante se assente."""
    try:
        import crewai  # noqa: F401  # Phase 3B userà API concrete da qui.
    except ImportError as exc:
        raise OrchestratorDependencyError(
            f"CrewAI non installato. Esegui: {_CREWAI_INSTALL_HINT}"
        ) from exc


class CrewAIAdapter(BaseOrchestrator):
    """Implementazione BaseOrchestrator delegata a CrewAI (stub fino a 3B)."""

    def __init__(self, settings: Settings | None = None) -> None:
        # Settings conservati per modello, tool path, ecc. in Phase 3B.
        self._settings = settings if settings is not None else get_settings()
        # Fail-fast sul packaging: meglio ora che al primo ``run`` in auto.
        _ensure_crewai_installed()

    @property
    def framework_name(self) -> str:
        return "crewai"

    def run(self, user_text: str) -> str:
        """Stub Phase 3A: conferma wiring; Phase 3B sostituirà con crew.run."""
        cleaned = user_text.strip()
        if not cleaned:
            return "Non ho ricevuto testo da elaborare."
        # Risposta minima per validare il contratto testo→testo senza agenti.
        return (
            f"[CrewAI stub] Ho ricevuto la richiesta. "
            f"Phase 3B collegherà agenti e tool. Input: {cleaned!r}"
        )
