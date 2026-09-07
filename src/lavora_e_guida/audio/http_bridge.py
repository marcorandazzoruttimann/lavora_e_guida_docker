"""Client HTTP JSON verso il servizio audio sul Windows Host (Phase 5).

Contratto atteso sul server host (porta tipica ``WINDOWS_AUDIO_PORT``):

- ``POST /listen`` → body JSON ``{"transcript": "<testo>"}``
- ``POST /speak``  → body JSON ``{"text": "<testo>"}`` → 2xx senza payload obbligatorio

In Phase 1 il server host non esiste ancora: i test usano un transport mock httpx.
L'URL base arriva da ``Settings.audio_bridge_url`` (discovery WSL + override).

I timeout di read sono volutamente diversi da un tetto unico: ``/listen`` deve
coprire wake + dettatura (minuti), ``/speak`` solo sintesi e playback. Il
connect resta corto così un host spento fallisce in pochi secondi. I default
qui coincidono con ``Settings``; la factory passa i valori da env.
"""

from __future__ import annotations

from typing import Any, Self

import httpx

from lavora_e_guida.audio.interface import BaseSTT, BaseTTS

# Handshake TCP verso il host: se 8765 è chiusa o il NAT WSL non risponde,
# non ha senso aspettare i 300s del read di /listen.
CONNECT_TIMEOUT_SEC = 5.0
# Fallback se si costruisce HttpBridgeSTT senza Settings (test diretti).
# Allineati a Settings.audio_listen_timeout_sec / audio_speak_timeout_sec.
DEFAULT_LISTEN_TIMEOUT_SEC = 300.0
DEFAULT_SPEAK_TIMEOUT_SEC = 120.0


def audio_http_timeout(read_sec: float) -> httpx.Timeout:
    """Timeout httpx a due tempi: connect corto, read/write lunghi come ``read_sec``.

    ``POST /listen`` resta in volo mentre l'helper C# aspetta la wake e accumula
    hypotesis; ``POST /speak`` resta in volo fino a fine playback. In entrambi
    i casi il tempo utile è il *read* della risposta. Il connect resta
    ``CONNECT_TIMEOUT_SEC``: porta chiusa o firewall non devono ereditare i minuti
    del listen. ``write`` usa lo stesso tetto del read (body piccoli, ma httpx
    richiede un valore); ``pool`` idem, irrilevante con un client per istanza.
    """
    return httpx.Timeout(read_sec, connect=CONNECT_TIMEOUT_SEC)


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
        # Senza timeout esplicito: 300s di read (wake + parlato), connect 5s.
        # La factory passa Settings.audio_listen_timeout_sec così l'env vince.
        resolved = (
            timeout
            if timeout is not None
            else audio_http_timeout(DEFAULT_LISTEN_TIMEOUT_SEC)
        )
        self._client = client or httpx.Client(timeout=resolved)
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
        # Stesso rstrip dello STT: POST su `/speak` senza slash doppio.
        self._base_url = base_url.rstrip("/")
        # Chiudiamo solo i client creati qui; MockTransport nei test resta al chiamante.
        self._owns_client = client is None
        # Speak: tetto da reply lunga (default 120s), non i 300s del listen.
        # Connect resta 5s come STT: stesso host, stesso fallimento rapido.
        resolved = (
            timeout
            if timeout is not None
            else audio_http_timeout(DEFAULT_SPEAK_TIMEOUT_SEC)
        )
        self._client = client or httpx.Client(timeout=resolved)
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
