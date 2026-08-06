"""Entrypoint Phase 2: state machine Listening→Thinking→Executing→Speaking.

Criterio di uscita Phase 2: classifica intent (Ollama o heuristic) e
risponde con stub via TTS Mock/HTTP. Phase 3 sostituirà lo stub con
BaseOrchestrator + tools.
"""

from __future__ import annotations

from lavora_e_guida.audio import BaseSTT, BaseTTS, create_audio_pair
from lavora_e_guida.config import Settings, get_settings
from lavora_e_guida.llm.local_ollama import LocalOllama
from lavora_e_guida.orchestrator.states import LoopState
from lavora_e_guida.routing.intent import IntentClassifier, stub_response_for


def _build_classifier(settings: Settings) -> tuple[IntentClassifier, LocalOllama | None]:
    """Crea classifier + client Ollama (se raggiungibile).

    Ritorna anche il client così `main` può chiuderlo in finally.
    Se il daemon è giù: classifier solo heuristic (loop comunque verde).
    """
    # Client puntato a settings: permette OLLAMA_BASE_URL su host Windows.
    ollama = LocalOllama(
        base_url=settings.ollama_base_url,
        model=settings.ollama_model,
    )
    # ping soft: non blocchiamo l'avvio se Ollama non c'è.
    if ollama.ping():
        return IntentClassifier(ollama), ollama
    # Daemon assente: chiudiamo subito il client e usiamo solo keyword.
    ollama.close()
    return IntentClassifier(llm=None), None


def run_agent_loop(
    stt: BaseSTT,
    tts: BaseTTS,
    classifier: IntentClassifier,
    *,
    # Limite utile ai test: None = finché 'esci' / EOF.
    max_turns: int | None = None,
) -> int:
    """Un turno = listen → classify → stub → speak; gestisce uscita soft.

    Ritorna 0 in uscita normale. Gli stati sono locali al turno (nessun
    side-effect globale): utili a logging futuro / TTS di status.
    """
    # Introduzione: chiarisce che non è più l'echo Phase 1.
    tts.speak(
        "Pronto. Classifico l'intent e rispondo con uno stub. Di' esci per terminare."
    )
    turns = 0
    while max_turns is None or turns < max_turns:
        # --- LISTENING -------------------------------------------------
        state = LoopState.LISTENING
        # listen() può bloccare a lungo (HTTP host) o leggere una riga (Mock).
        user_text = stt.listen()
        # EOF / transcript vuoto: uscita soft (pipe chiusa, Enter a vuoto).
        if not user_text.strip():
            state = LoopState.SPEAKING
            tts.speak("Nessun input. Uscita.")
            return 0
        # Stop vocale/testuale: evita Ctrl+C in contesti hands-free.
        if user_text.strip().casefold() in {"esci", "exit", "quit"}:
            state = LoopState.SPEAKING
            tts.speak("Arrivederci.")
            return 0

        # --- THINKING (classificazione) --------------------------------
        state = LoopState.THINKING
        result = classifier.classify(user_text)

        # --- EXECUTING (stub Phase 2) ----------------------------------
        state = LoopState.EXECUTING
        # Nessun tool reale: solo frase di conferma intent per validare il loop.
        reply = stub_response_for(result.label, user_text.strip())
        # Prefisso diagnostico leggero: aiuta i test e il debug Mock.
        status = (
            f"Intent {result.label.value} "
            f"(confidenza {result.confidence:.2f}, via {result.source}). "
        )

        # --- SPEAKING --------------------------------------------------
        state = LoopState.SPEAKING
        # Concateniamo status + stub in un'unica utterance TTS.
        tts.speak(status + reply)
        # state usato: evita warning unused e documenta l'ultima fase.
        _ = state
        turns += 1
    return 0


# Alias retrocompatibile per i test Phase 1 che importavano run_echo_loop.
# Comportamento diverso (intent stub vs echo): i test echo vanno aggiornati.
def run_echo_loop(
    stt: BaseSTT,
    tts: BaseTTS,
    *,
    max_turns: int | None = None,
) -> int:
    """Deprecated alias: usa heuristic-only (nessun Ollama) per smoke test."""
    return run_agent_loop(
        stt,
        tts,
        IntentClassifier(llm=None),
        max_turns=max_turns,
    )


def main() -> None:
    # Settings da .env / ambiente: audio driver, modello Ollama, URL.
    settings = get_settings()
    stt, tts = create_audio_pair(settings)
    classifier, ollama = _build_classifier(settings)
    try:
        code = run_agent_loop(stt, tts, classifier)
    finally:
        # Chiudiamo client HTTP audio e Ollama di proprietà.
        if ollama is not None:
            ollama.close()
        for endpoint in (stt, tts):
            close = getattr(endpoint, "close", None)
            if callable(close):
                close()
    raise SystemExit(code)


if __name__ == "__main__":
    # Permette `python -m lavora_e_guida.main` oltre all'entry point console.
    main()
