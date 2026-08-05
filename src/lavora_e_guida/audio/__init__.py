"""STT/TTS abstractions and drivers (Phase 1+).

Esporta i contratti e la factory; i driver concreti restano importabili
dai sotto-moduli per test mirati e wiring avanzato.
"""

from lavora_e_guida.audio.factory import create_audio_pair, create_stt, create_tts
from lavora_e_guida.audio.interface import BaseSTT, BaseTTS

__all__ = [
    "BaseSTT",
    "BaseTTS",
    "create_audio_pair",
    "create_stt",
    "create_tts",
]
