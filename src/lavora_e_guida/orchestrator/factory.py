"""Factory orchestratore: import lazy ed esclusivo di crewai **oppure** autogen.

``create()`` legge ``ORCHESTRATOR_FRAMEWORK`` da Settings e carica **solo**
il modulo adapter corrispondente — mai entrambi nello stesso processo.
"""

from __future__ import annotations

from typing import Final, Literal, get_args

from lavora_e_guida.config import Settings, get_settings
from lavora_e_guida.orchestrator.base import (
    BaseOrchestrator,
    OrchestratorConfigurationError,
)

# Literal condiviso con Settings: unica fonte per messaggi di errore parlanti.
OrchestratorFramework = Literal["crewai", "autogen"]
SUPPORTED_FRAMEWORKS: Final[tuple[str, ...]] = get_args(OrchestratorFramework)


def validate_framework(framework: str) -> OrchestratorFramework:
    """Normalizza e valida il nome framework prima dell'import lazy.

    Chiamata esplicita anche dai test: fail-fast con messaggio italiano
    se qualcuno estende Settings senza aggiornare la factory.
    """
    normalized = framework.strip().casefold()
    if normalized not in SUPPORTED_FRAMEWORKS:
        allowed = ", ".join(SUPPORTED_FRAMEWORKS)
        raise OrchestratorConfigurationError(
            f"ORCHESTRATOR_FRAMEWORK non supportato: {framework!r}. "
            f"Valori ammessi: {allowed}."
        )
    # Cast sicuro: casefold già verificato contro la tupla Literal.
    return normalized  # type: ignore[return-value]


def create(settings: Settings | None = None) -> BaseOrchestrator:
    """Istanzia l'adapter del framework configurato (import lazy ed esclusivo).

    Side-effect: importa un solo sotto-modulo ``*_adapter``; l'altro framework
    resta fuori da ``sys.modules`` finché non si cambia env e si riavvia.
    """
    cfg = settings if settings is not None else get_settings()
    # Validazione parlante anche se pydantic già vincola il Literal in Settings.
    framework = validate_framework(cfg.orchestrator_framework)

    if framework == "crewai":
        # Import differito: autogen_adapter non viene mai toccato.
        from lavora_e_guida.orchestrator.crewai_adapter import CrewAIAdapter

        return CrewAIAdapter(settings=cfg)

    if framework == "autogen":
        # Simmetrico: solo autogen_adapter, mai crewai_adapter.
        from lavora_e_guida.orchestrator.autogen_adapter import AutoGenAdapter

        return AutoGenAdapter(settings=cfg)

    # Guardia difensiva: validate_framework dovrebbe aver già bloccato.
    raise OrchestratorConfigurationError(
        f"Framework orchestratore non gestito: {framework!r}"
    )
