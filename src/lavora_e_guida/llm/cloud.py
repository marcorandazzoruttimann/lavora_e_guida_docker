"""Stub client LLM cloud (Phase 4).

Presente come placeholder così gli import `lavora_e_guida.llm` restano
stabili; la policy ibrida Ollama/cloud arriva in Phase 4.1.
"""

from __future__ import annotations


class CloudLLM:
    """Non implementato in Phase 2: richiede chiavi in `.env` (Phase 4)."""

    def complete(self, prompt: str) -> str:
        # Fail esplicito: evita che un import accidentale finga di funzionare.
        raise NotImplementedError(
            "CloudLLM è previsto in Phase 4 (ANTHROPIC_API_KEY / OPENAI_API_KEY)."
        )
