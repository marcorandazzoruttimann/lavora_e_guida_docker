"""Contratto unificato per gli orchestratori multi-framework (Phase 3).

Gli adapter CrewAI e AutoGen espongono solo testo in/out: il loop vocale
e il TTS non devono conoscere dettagli del framework scelto via env.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class OrchestratorDependencyError(ImportError):
    """Dipendenza opzionale del framework assente (extra pip non installato).

    Messaggio pensato per TTS / log: indica il comando pip da eseguire
    senza stack trace tecnico verso l'utente hands-free.
    """


class OrchestratorConfigurationError(ValueError):
    """Valore ``ORCHESTRATOR_FRAMEWORK`` non riconosciuto o incoerente.

    Separato da DependencyError: qui il problema è config, non packaging.
    """


class BaseOrchestrator(ABC):
    """Esegue un turno utente → risposta testuale pronta per TTS.

    Phase 3A definisce solo il contratto; Phase 3B arricchisce gli adapter
    con agenti, tool binding e gestione errori user-friendly.
    """

    @property
    @abstractmethod
    def framework_name(self) -> str:
        """Identificatore stabile del backend: ``crewai`` o ``autogen``."""
        ...

    @abstractmethod
    def run(self, user_text: str) -> str:
        """Elabora l'utterance utente e restituisce testo da pronunciare.

        Contratto:
        - Input: testo già trascritto (STT a monte); può essere vuoto.
        - Output: sempre ``str``; errori recuperabili → messaggio parlante,
          non eccezioni verso il loop (Phase 4 affinerà i callback status).
        """
        ...
