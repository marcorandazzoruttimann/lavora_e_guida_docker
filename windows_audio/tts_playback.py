"""Sintesi edge-tts + playback pygame, solo sul processo host Windows.

Contratto verso ``server.py``: ``synthesize_and_play(text)`` blocca finché
l'audio non è finito; ``warmup_synthesis()`` fa il primo HTTPS a freddo senza
parlare, così WSL non timeout-a l'intro.

Side-effect: file MP3 temporaneo, device di default di Windows, chiamata HTTPS
verso i server Microsoft (edge-tts).

Non importa ``lavora_e_guida``: questo venv è separato e non deve dipendere
dal package WSL. Il testo arriva già pulito da ``prepare_spoken_text``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from pathlib import Path

# Voce del piano: Elsa neural italiano. Override con TTS_VOICE o --voice.
DEFAULT_VOICE = "it-IT-ElsaNeural"

_log = logging.getLogger("audio_host.tts")

# Mixer inizializzato una volta sola: pygame non ama init/quit a ogni frase.
_mixer_ready = False
# music.get_busy() su Windows+MP3 resta True a parlato finito (~130s, WSL 120s).
# Sound() decode in RAM può non tornare (nessun audio, hang sulla sintesi apparente).
# Play via music + sleep(durata). Tetto sotto AUDIO_SPEAK_TIMEOUT_SEC.
_MAX_PLAYBACK_SEC = 90.0
_PLAYBACK_TAIL_SEC = 0.25
# Stima se mutagen manca: edge-tts neural è tipicamente ~48 kbit/s.
_TTS_BITRATE_BPS = 48_000


def _ensure_mixer() -> None:
    """Apre SDL_mixer al primo speak. Senza device Windows fallisce in modo parlante.

    Non chiamare a import-time: i test WSL importano ``server.py`` senza pygame.
    """
    global _mixer_ready
    if _mixer_ready:
        return
    try:
        import pygame
    except ImportError as exc:
        # Messaggio per chi ha avviato il server senza ``pip install -r``.
        raise RuntimeError(
            "pygame non è installato. Sul venv Windows: pip install -r requirements.txt"
        ) from exc

    # 24 kHz: edge-tts neural emette tipicamente 24 kHz; pygame ricampiona se serve.
    # channels=2: qualche driver Windows mono-only si rifiuta, lo stereo è più tollerante.
    pygame.mixer.init(frequency=24000, size=-16, channels=2)
    _mixer_ready = True
    _log.info("pygame mixer pronto.")


def _release_mixer() -> None:
    """Chiude SDL_mixer dopo ogni speak. Senza, il device resta preso (ronzio / mic)."""
    global _mixer_ready
    if not _mixer_ready:
        return
    import pygame

    try:
        pygame.mixer.music.stop()
        unload = getattr(pygame.mixer.music, "unload", None)
        if unload is not None:
            unload()
        pygame.mixer.quit()
    except pygame.error:
        pass
    _mixer_ready = False


async def _save_mp3(text: str, dest: Path, voice: str) -> None:
    """Scrive l'MP3 su ``dest``. Richiede rete: edge-tts non è offline."""
    try:
        import edge_tts
    except ImportError as exc:
        raise RuntimeError(
            "edge-tts non è installato. Sul venv Windows: pip install -r requirements.txt"
        ) from exc

    # Communicate è one-shot: un testo, una voce, un file. Niente streaming al mixer
    # perché pygame.music.load vuole un file completo e il mutex copre già l'attesa.
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(dest))


def _mp3_duration_sec(path: Path) -> float:
    """Durata in secondi. mutagen se c'è; altrimenti size/bitrate. Mai 0, mai > tetto."""
    try:
        from mutagen.mp3 import MP3

        parsed = MP3(str(path))
        length = float(getattr(getattr(parsed, "info", None), "length", 0.0) or 0.0)
        if length > 0:
            return min(length, _MAX_PLAYBACK_SEC)
    except Exception:  # noqa: BLE001 — ImportError o MP3 rotto: stima dal peso
        # mutagen assente o header illeggibile: non è un fallimento dello speak.
        pass
    size = path.stat().st_size
    estimated = (size * 8) / _TTS_BITRATE_BPS
    return min(max(estimated, 0.5), _MAX_PLAYBACK_SEC)


def _play_mp3_blocking(path: Path) -> None:
    """Riproduce con ``mixer.music`` (streaming, come il giro che si sentiva).

    Attende ``durata + coda``, poi stop. Non ``get_busy`` (resta True) e non
    ``Sound()`` (su questa macchina hang prima di play, intro muta, WSL timeout).
    """
    import pygame

    _ensure_mixer()
    seconds = _mp3_duration_sec(path)
    wait = min(seconds + _PLAYBACK_TAIL_SEC, _MAX_PLAYBACK_SEC)
    _log.info("Playback %.1fs (stima durata %.1fs, %d byte).", wait, seconds, path.stat().st_size)
    pygame.mixer.music.load(str(path))
    pygame.mixer.music.play()
    try:
        time.sleep(wait)
    finally:
        pygame.mixer.music.stop()
        unload = getattr(pygame.mixer.music, "unload", None)
        if unload is not None:
            unload()


def synthesize_and_play(text: str, *, voice: str = DEFAULT_VOICE) -> None:
    """Sintetizza ``text`` e lo ascolta per intero sul device di default.

    Blocca il thread chiamante (il worker di ``/speak``). Side-effect: HTTPS
    Microsoft + file in %TEMP% cancellato in ``finally``. Non accetta stringa
    vuota: il server HTTP deve rifiutare prima, così non teniamo il mutex a vuoto.
    """
    stripped = text.strip()
    if not stripped:
        # Difesa in profondità: il handler ha già fatto 400. Qui è un bug di wiring.
        raise ValueError("synthesize_and_play: testo vuoto, niente da dire.")

    _ensure_mixer()
    # delete=False: su Windows il file aperto da pygame non si può cancellare
    # dal context manager NamedTemporaryFile mentre il mixer lo tiene.
    fd, raw_path = tempfile.mkstemp(prefix="lavora_e_guida_tts_", suffix=".mp3")
    os.close(fd)
    path = Path(raw_path)
    try:
        _log.info("Sintesi edge-tts voce=%s caratteri=%d", voice, len(stripped))
        asyncio.run(_save_mp3(stripped, path, voice))
        nbytes = path.stat().st_size
        if nbytes <= 0:
            raise RuntimeError("edge-tts ha scritto un MP3 vuoto.")
        _log.info("MP3 pronto, %d byte.", nbytes)
        _play_mp3_blocking(path)
    finally:
        # Rilascia WASAPI: cuffie USB con mixer tenuto aperto = ronzio e mic muto in dettatura.
        _release_mixer()
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            _log.warning("Non cancello %s: %s", path, exc)


def warmup_synthesis(*, voice: str = DEFAULT_VOICE) -> None:
    """Prima sintesi a freddo, senza parlato. Side-effect: HTTPS Microsoft + mixer.

    edge-tts al primo giro può impiegarci 2-3 minuti (TLS + modello vocale). Se WSL
    lancia ``/speak`` in parallelo con timeout 120s, l'utente rilancia l'agente e
    sente due intro: il mutex half-duplex tiene ancora la prima sintesi. Questo
    riscaldamento sta nel ``main`` del host, prima di ``serve_forever``.
    Fallire qui non è fatale: logghiamo e il primo ``/speak`` resterà lento.
    """
    _log.info("Riscaldo edge-tts (sintesi silenziosa, aspetta prima di lanciare WSL).")
    fd, raw_path = tempfile.mkstemp(prefix="lavora_e_guida_tts_warmup_", suffix=".mp3")
    os.close(fd)
    path = Path(raw_path)
    try:
        # Una parola basta ad aprire la sessione HTTPS. Non la riproduciamo.
        asyncio.run(_save_mp3("Pronto.", path, voice))
        nbytes = path.stat().st_size
        if nbytes <= 0:
            _log.warning("Warmup TTS: MP3 vuoto, il primo /speak potrà essere lento.")
            return
        _log.info("Warmup TTS ok, %d byte.", nbytes)
        # Init+quit mixer: il primo speak non paga anche l'apertura WASAPI.
        _ensure_mixer()
        _release_mixer()
    except Exception as exc:  # noqa: BLE001 — il host deve comunque ascoltare :8765
        _log.warning("Warmup TTS fallito: %s. Il primo /speak potrà essere lento.", exc)
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
