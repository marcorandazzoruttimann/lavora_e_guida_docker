"""Client HTTP JSON verso il servizio audio sul Windows Host (Phase 5).

Contratto atteso sul server host (porta tipica ``WINDOWS_AUDIO_PORT``):

- ``POST /listen`` → body JSON ``{"transcript": "<testo>"}``
- ``POST /speak``  → body JSON ``{"text": "<testo>"}`` → 2xx senza payload obbligatorio

In Phase 1 il server host non esiste ancora: i test usano un transport mock httpx.
L'URL base arriva da ``Settings.audio_bridge_url`` (discovery WSL + override).
"""

from __future__ import annotations

from typing import Any, Self

import httpx

from lavora_e_guida.audio.interface import BaseSTT, BaseTTS

# Timeout generoso: STT lato host può attendere la frase completa dell'utente.
_DEFAULT_TIMEOUT = httpx.Timeout(60.0, connect=5.0)


class HttpBridgeSTT(BaseSTT):
    """STT remoto: chiede al host di ascoltare e restituisce il transcript."""

    def __init__(
        self,
        base_url: str,
        *,
        # Client iniettabile: riuso connessioni e MockTransport nei test.
        client: httpx.Client | None = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> None:
        # Slash finale rimosso: concateniamo path senza `//listen`.
        self._base_url = base_url.rstrip("/")
        # Tracciamo ownership: chiudiamo solo i client creati qui, non quelli iniettati.
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout if timeout is not None else _DEFAULT_TIMEOUT,
        )
        # URL assoluto: funziona sia con client “nudo” sia con base_url già impostato.
        self._listen_url = f"{self._base_url}/listen"

    def listen(self) -> str:
        # POST senza body: il server host gestisce wake/VAD; qui aspettiamo solo JSON.
        response = self._client.post(self._listen_url)
        # raise_for_status: un 4xx/5xx non deve sembrare “l'utente ha taciuto”.
        response.raise_for_status()
        payload: Any = response.json()
        # Chiave documentata obbligatoriamente: assenza → KeyError esplicito in debug.
        transcript = payload["transcript"]
        if not isinstance(transcript, str):
            raise TypeError(
                f"Campo 'transcript' deve essere str, ricevuto {type(transcript)!r}"
            )
        return transcript

    def close(self) -> None:
        """Chiude il client solo se creato da questa istanza."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class HttpBridgeTTS(BaseTTS):
    """TTS remoto: invia il testo al host per sintesi e playback."""

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.Client | None = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=timeout if timeout is not None else _DEFAULT_TIMEOUT,
        )
        self._speak_url = f"{self._base_url}/speak"

    def speak(self, text: str) -> None:
        # Body minimo `{text}`: il host sceglie voce edge-tts e il device di playback.
        response = self._client.post(self._speak_url, json={"text": text})
        response.raise_for_status()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
