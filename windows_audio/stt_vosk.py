"""Dettatura italiana locale (Vosk) dopo la wake WinRT.

WinRT list-constraint riconosce «ehi assistente»; la dettatura topic cloud su
questo helper Win32 si chiude con UserCanceled senza hypotesis. Qui il mic è
già libero (l'helper ha fatto Dispose). Side-effect: download del modello
small-it al primo uso, apertura del device di default a 16 kHz.

Il loop WSL non vede questo modulo: parla solo con ``POST /listen`` su :8765.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlopen

_log = logging.getLogger("audio_host.stt")

# Stesso tetto dell'helper C# (timer 45s dopo la wake).
DICTATION_TIMEOUT_SEC = 45
# Vosk small italiano: abbastanza leggero da stare in %LOCALAPPDATA%.
_MODEL_NAME = "vosk-model-small-it-0.22"
_MODEL_ZIP_URL = (
    "https://alphacephei.com/vosk/models/vosk-model-small-it-0.22.zip"
)
_SAMPLE_HZ = 16_000
_BLOCK_FRAMES = 4000  # 0,25 s a 16 kHz: PartialResult abbastanza frequente.


@dataclass(frozen=True)
class DictationOutcome:
    """Risultato di un turno dopo il beep. text vuoto solo per cancel/empty."""

    kind: str  # close | cancel | timeout_text | timeout_empty
    text: str = ""


def _model_dir() -> Path:
    # LOCALAPPDATA su Windows; fallback accanto a questo file se manca.
    root = os.environ.get("LOCALAPPDATA") or str(Path(__file__).resolve().parent)
    return Path(root) / "lavora_e_guida" / _MODEL_NAME


def ensure_vosk_model() -> Path:
    """Scarica e scompatta il modello se manca. Rete HTTPS verso alphacephei."""
    dest = _model_dir()
    marker = dest / "am" / "final.mdl"
    if marker.is_file() or (dest / "conf" / "model.conf").is_file():
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    zip_path = dest.parent / f"{_MODEL_NAME}.zip"
    _log.info("Scarico modello Vosk italiano (%s)…", _MODEL_NAME)
    with urlopen(_MODEL_ZIP_URL, timeout=120) as resp, zip_path.open("wb") as out:
        while True:
            chunk = resp.read(1024 * 64)
            if not chunk:
                break
            out.write(chunk)

    _log.info("Scompatto %s", zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(dest.parent)
    try:
        zip_path.unlink()
    except OSError:
        pass
    if not dest.is_dir():
        raise RuntimeError(f"Modello Vosk non trovato dopo lo zip: {dest}")
    return dest


def _normalize(text: str) -> str:
    """Come PhraseMatcher C#: minuscole, punteggiatura → spazio, spazi singoli."""
    if not text.strip():
        return ""
    pieces: list[str] = []
    for ch in text.lower():
        pieces.append(ch if ch.isalnum() else " ")
    return " ".join("".join(pieces).split())


def contains_phrase(haystack: str, phrase: str) -> bool:
    h = _normalize(haystack)
    p = _normalize(phrase)
    if not h or not p:
        return False
    pattern = r"(^|\s)" + re.escape(p) + r"($|\s)"
    return re.search(pattern, h) is not None


def strip_phrase(raw: str, phrase: str) -> str:
    """Toglie l'ultima occorrenza della frase di chiusura, restano le parole utili."""
    words = _normalize(phrase).split()
    if not words:
        return raw.strip()
    pattern = r"[\s\W]+".join(re.escape(w) for w in words)
    matches = list(re.finditer(pattern, raw, flags=re.IGNORECASE))
    if not matches:
        return " ".join(raw.split())
    last = matches[-1]
    cut = raw[: last.start()] + " " + raw[last.end() :]
    return " ".join(cut.split())


def run_dictation(
    *,
    wake_close: str,
    wake_cancel: str,
    timeout_sec: float = DICTATION_TIMEOUT_SEC,
    should_stop: Callable[[], bool] | None = None,
) -> DictationOutcome:
    """Ascolta il mic di default fino a chiudi / annulla / timeout.

    Side-effect: device input WASAPI. Non chiamare mentre pygame tiene il mixer
    (il mutex half-duplex del host lo evita). should_stop: WSL ha staccato.
    """
    try:
        import sounddevice as sd
        from vosk import KaldiRecognizer, Model, SetLogLevel
    except ImportError as exc:
        raise RuntimeError(
            "Dettatura Vosk: nel venv Windows pip install -r requirements.txt "
            "(pacchetti vosk, sounddevice, numpy)."
        ) from exc

    SetLogLevel(-1)
    model_path = ensure_vosk_model()
    _log.info("Vosk modello %s, ascolto %ss.", model_path, timeout_sec)
    recognizer = KaldiRecognizer(Model(str(model_path)), _SAMPLE_HZ)
    recognizer.SetWords(False)

    committed: list[str] = []
    deadline = time.monotonic() + timeout_sec

    def combined(extra: str = "") -> str:
        parts = [p for p in committed if p.strip()]
        if extra.strip():
            parts.append(extra.strip())
        return " ".join(parts)

    def decide(snapshot: str) -> DictationOutcome | None:
        if contains_phrase(snapshot, wake_cancel):
            _log.info("Vosk: assistente annulla.")
            return DictationOutcome("cancel")
        if contains_phrase(snapshot, wake_close):
            transcript = strip_phrase(snapshot, wake_close)
            _log.info("Vosk: assistente chiudi, transcript='%s'.", transcript)
            if not transcript.strip():
                return DictationOutcome("timeout_empty")
            return DictationOutcome("close", transcript)
        return None

    with sd.RawInputStream(
        samplerate=_SAMPLE_HZ,
        blocksize=_BLOCK_FRAMES,
        dtype="int16",
        channels=1,
    ) as stream:
        while time.monotonic() < deadline:
            if should_stop is not None and should_stop():
                raise RuntimeError("client WSL staccato durante Vosk")
            data, overflowed = stream.read(_BLOCK_FRAMES)
            if overflowed:
                _log.info("Vosk: overflow mic (un blocco perso).")
            raw = bytes(data)
            if recognizer.AcceptWaveform(raw):
                payload = json.loads(recognizer.Result() or "{}")
                piece = str(payload.get("text") or "").strip()
                if piece:
                    _log.info("Vosk frase: '%s'", piece)
                    committed.append(piece)
                decided = decide(combined())
                if decided is not None:
                    return decided
            else:
                partial_json = json.loads(recognizer.PartialResult() or "{}")
                partial = str(partial_json.get("partial") or "").strip()
                if partial:
                    decided = decide(combined(partial))
                    if decided is not None:
                        return decided

    leftover = json.loads(recognizer.FinalResult() or "{}")
    last = str(leftover.get("text") or "").strip()
    text = combined(last).strip()
    if text:
        _log.info("Vosk timeout 45s con testo: '%s'", text)
        return DictationOutcome("timeout_text", text)
    _log.info("Vosk timeout 45s buffer vuoto.")
    return DictationOutcome("timeout_empty")
