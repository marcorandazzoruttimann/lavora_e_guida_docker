"""Entrypoint Phase 1: loop echo Mock/HTTP senza agenti né LLM.

Criterio di uscita Phase 1: testo end-to-end (listen → echo → speak)
senza dipendenze audio reali. Phase 2 sostituirà l'echo con la state machine.
"""

from __future__ import annotations

from lavora_e_guida.audio import BaseSTT, BaseTTS, create_audio_pair
from lavora_e_guida.config import get_settings


def run_echo_loop(
    stt: BaseSTT,
    tts: BaseTTS,
    *,
    # Limite utile ai test: None = finché l'utente non digita 'esci' / EOF.
    max_turns: int | None = None,
) -> int:
    """Ascolta, ripete il testo via TTS, gestisce uscita soft.

    Ritorna 0 in uscita normale; usato anche dallo script manuale e dai test.
    """
    # Messaggio iniziale in italiano: conferma che il driver è attivo.
    tts.speak("Pronto. Dimmi qualcosa: ripeto quello che capisco. Di' esci per terminare.")
    turns = 0
    while max_turns is None or turns < max_turns:
        # listen() può bloccare a lungo (HTTP host) o leggere una riga (Mock).
        user_text = stt.listen()
        # EOF / transcript vuoto: chiudiamo senza errore (pipe chiusa, Enter a vuoto).
        if not user_text.strip():
            tts.speak("Nessun input. Uscita.")
            return 0
        # Comando vocale/testuale di stop: evita Ctrl+C in contesti hands-free.
        if user_text.strip().casefold() in {"esci", "exit", "quit"}:
            tts.speak("Arrivederci.")
            return 0
        # Echo puro: nessuna classificazione intent (quella arriva in Phase 2).
        tts.speak(f"Hai detto: {user_text}")
        turns += 1
    return 0


def main() -> None:
    # Settings da .env / ambiente: sceglie mock vs http e l'URL del bridge.
    settings = get_settings()
    stt, tts = create_audio_pair(settings)
    try:
        code = run_echo_loop(stt, tts)
    finally:
        # Chiudiamo eventuali client HTTP di proprietà (Mock è no-op se assente).
        for endpoint in (stt, tts):
            close = getattr(endpoint, "close", None)
            if callable(close):
                close()
    raise SystemExit(code)


if __name__ == "__main__":
    # Permette anche `python -m lavora_e_guida.main` oltre all'entry point console.
    main()
