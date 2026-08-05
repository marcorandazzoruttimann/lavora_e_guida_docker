"""Contratti astratti STT/TTS: gli agenti lavorano solo su `str`.

In Phase 1 l'audio reale (microfono/altoparlanti) resta fuori dal processo WSL:
qui definiamo solo il contratto che Mock e bridge HTTP devono rispettare,
così orchestratore e test non dipendono da hardware o WinRT.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class BaseSTT(ABC):
    """Speech-to-text: produce testo utente senza esporre dettagli audio."""

    @abstractmethod
    def listen(self) -> str:
        """Blocca fino a ottenere una frase trascritta (o input equivalente).

        Side-effect tipici: attesa microfono lato host, oppure `input()` in Mock.
        Il ritorno è sempre testo già normalizzato a stringa; stringa vuota
        significa “nessun input utile” (es. Enter a vuoto / timeout soft).
        """
        ...


class BaseTTS(ABC):
    """Text-to-speech: riproduce (o simula) una frase verso l'utente."""

    @abstractmethod
    def speak(self, text: str) -> None:
        """Emette `text` all'utente; non restituisce nulla.

        Side-effect tipici: HTTP POST verso host Windows, oppure print in Mock.
        Implementazioni HTTP devono propagare errori di rete al chiamante
        (il loop orchestratore deciderà come gestirli in Phase 4).
        """
        ...
