"""Turno LLM strutturato: testo parlato e/o functionCall nativi Gemini.

Sostituisce il JSON-in-testo `{"tool","args"}` / `tool=none`. Il loop vocale
parla `text` oppure esegue la prima `FunctionCall` e rimanda `functionResponse`.
Side-effect: nessuno; è solo il valore di ritorno di `GeminiChat.chat`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FunctionCall:
    """Una chiamata tool proposta dal modello (name + args già parsati).

    `call_id` è l'`id` Gemini da riecheggiare in `functionResponse` se presente;
    None sui mock e quando l'API omette il campo.

    `thought_signature` è il blob opaco (`thoughtSignature` sulla part REST)
    che Gemini 3 valida sul primo `functionCall` del turno successivo. Va
    copiato tale e quale in history; None sui mock e se l'API omette il campo.
    """

    name: str
    args: dict[str, Any]
    call_id: str | None = None
    thought_signature: str | None = None


@dataclass(frozen=True)
class LlmTurn:
    """Esito di un `generateContent`: reply parlata e/o tool da eseguire.

    `text` vuoto + zero call = turno vuoto (il loop parla un fallback).
    Più call: il loop ne esegue una sola (la prima); le altre si ignorano.
    """

    text: str = ""
    function_calls: tuple[FunctionCall, ...] = ()

    def is_empty(self) -> bool:
        """True se non c'è nulla da dire né da eseguire."""
        return (not self.text.strip()) and (not self.function_calls)
