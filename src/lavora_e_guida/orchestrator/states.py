"""Stati della macchina a stati del loop vocale (Phase 2).

Ciclo nominale: Listening → Thinking → Executing → Speaking → Listening.
Gli stati sono espliciti così TTS intermedio (Phase 4) e logging possono
ancorarsi a transizioni chiare senza indovinare dal call-stack.
"""

from __future__ import annotations

from enum import Enum


class LoopState(str, Enum):
    """Fasi del turno utente → risposta stub (Phase 2) / orchestratore (Phase 3+)."""

    # Attesa utterance (Mock: readline; HTTP: POST /listen sul host).
    LISTENING = "listening"
    # Classificazione intent via Ollama (o heuristic fallback se LLM giù).
    THINKING = "thinking"
    # Esecuzione azione: in Phase 2 solo stub testuale; poi tools/framework.
    EXECUTING = "executing"
    # Output verso l'utente (Mock print / HTTP POST /speak).
    SPEAKING = "speaking"
