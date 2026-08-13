"""Token usage estratto dai payload LLM (Ollama locale e Gemini cloud).

I client tengono `last_usage` come side-effect: `chat()` / `generate()`
restano `str` così `SupportsChat` e i test esistenti non cambiano.
Campi assenti o tipi inattesi → 0 (mai eccezione: la telemetria non
deve far fallire il turno vocale).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def _as_token_count(value: object) -> int:
    """Converte un campo JSON in token count non negativo; altrimenti 0.

    `bool` è sottoclasse di `int` in Python: lo scartiamo esplicitamente
    così `True` non diventa 1 token fittizio.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return max(0, value)


@dataclass(frozen=True)
class TokenUsage:
    """Conteggio token di una singola chiamata generate/chat.

    `prompt_tokens` = input (prompt + history); `completion_tokens` = output.
    Somma con `+` per aggregare più round tool nello stesso turno STT.
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0

    def __add__(self, other: object) -> TokenUsage:
        # NotImplemented: permette a Python di provare l'altro operando.
        if not isinstance(other, TokenUsage):
            return NotImplemented
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
        )

    def __radd__(self, other: object) -> TokenUsage:
        # sum() parte da 0: 0 + usage deve restituire usage, non TypeError.
        if other == 0:
            return self
        return self.__add__(other)


def parse_ollama_usage(payload: Any) -> TokenUsage:
    """Legge `prompt_eval_count` / `eval_count` dal JSON `/api/chat` o `/api/generate`.

    Payload non-dict o campi mancanti → TokenUsage(0, 0).
    """
    # Payload HTTP già deserializzato: se non è un oggetto, niente da leggere.
    if not isinstance(payload, dict):
        return TokenUsage()
    # prompt_eval_count = input; eval_count = token generati (docs Ollama).
    return TokenUsage(
        prompt_tokens=_as_token_count(payload.get("prompt_eval_count")),
        completion_tokens=_as_token_count(payload.get("eval_count")),
    )


def parse_gemini_usage(payload: Any) -> TokenUsage:
    """Legge `usageMetadata.promptTokenCount` / `candidatesTokenCount`.

    `usageMetadata` assente o non-oggetto → TokenUsage(0, 0).
    """
    # generateContent mette i conteggi in usageMetadata, non in cima al JSON.
    if not isinstance(payload, dict):
        return TokenUsage()
    meta = payload.get("usageMetadata")
    if not isinstance(meta, dict):
        return TokenUsage()
    # promptTokenCount = input; candidatesTokenCount = testo emesso.
    return TokenUsage(
        prompt_tokens=_as_token_count(meta.get("promptTokenCount")),
        completion_tokens=_as_token_count(meta.get("candidatesTokenCount")),
    )
