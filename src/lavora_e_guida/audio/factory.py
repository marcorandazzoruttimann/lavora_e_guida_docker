"""Factory audio: sceglie Mock o HTTP in base a ``AUDIO_DRIVER``.

Centralizza il wiring così `main` e i test non importano driver concreti a mano.
Import lazy non serve qui: Mock e httpx sono dipendenze leggere sempre presenti.
"""

from __future__ import annotations

from lavora_e_guida.audio.http_bridge import (
    HttpBridgeSTT,
    HttpBridgeTTS,
    audio_http_timeout,
)
from lavora_e_guida.audio.interface import BaseSTT, BaseTTS
from lavora_e_guida.audio.mock import MockSTT, MockTTS
from lavora_e_guida.config import Settings, get_settings


def create_stt(settings: Settings | None = None) -> BaseSTT:
    """Restituisce l'implementazione STT configurata."""
    cfg = settings if settings is not None else get_settings()
    # Branch esplicito su Literal: fail-fast se qualcuno estende l'enum senza factory.
    if cfg.audio_driver == "mock":
        return MockSTT()
    if cfg.audio_driver == "http":
        # URL già risolto (nameserver WSL o WINDOWS_HOST).
        # Timeout da Settings: AUDIO_LISTEN_TIMEOUT_SEC, connect fisso a 5s.
        return HttpBridgeSTT(
            cfg.audio_bridge_url,
            timeout=audio_http_timeout(cfg.audio_listen_timeout_sec),
        )
    raise ValueError(f"AUDIO_DRIVER non supportato: {cfg.audio_driver!r}")


def create_tts(settings: Settings | None = None) -> BaseTTS:
    """Restituisce l'implementazione TTS configurata."""
    cfg = settings if settings is not None else get_settings()
    if cfg.audio_driver == "mock":
        return MockTTS()
    if cfg.audio_driver == "http":
        # Speak più corto del listen: sintesi + playback, non attesa wake.
        return HttpBridgeTTS(
            cfg.audio_bridge_url,
            timeout=audio_http_timeout(cfg.audio_speak_timeout_sec),
        )
    raise ValueError(f"AUDIO_DRIVER non supportato: {cfg.audio_driver!r}")


def create_audio_pair(
    settings: Settings | None = None,
) -> tuple[BaseSTT, BaseTTS]:
    """Comodo per lo script echo / entrypoint: STT e TTS dello stesso driver."""
    cfg = settings if settings is not None else get_settings()
    return create_stt(cfg), create_tts(cfg)
