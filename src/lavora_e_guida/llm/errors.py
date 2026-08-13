"""Errori LLM condivisi (Ollama locale e Gemini cloud).

Una sola gerarchia permette al loop del lab (e al classifier) di catturare
`LLMError` senza dipendere dal provider concreto.
"""

from __future__ import annotations


class LLMError(RuntimeError):
    """Errore di trasporto, auth o payload non valido da un client LLM."""
