"""Loop singolo agente: MockSTT → LocalOllama chat → MockTTS.

Step 1 del lab: solo conversazione, nessun tool FS.
La storia messaggi resta in memoria di processo (niente persistenza).
"""

from __future__ import annotations

import time
from typing import Protocol

from lavora_e_guida.audio.interface import BaseSTT, BaseTTS
from lavora_e_guida.llm.local_ollama import LocalOllama, OllamaError

from sandbox.ollama_fs_lab.config import OLLAMA_MODEL, OLLAMA_URL

# System prompt corto: i 3B seguono meglio istruzioni brevi in italiano.
# Nessun tool in Step 1: vietiamo inventare azioni FS che non esistono ancora.
_SYSTEM_PROMPT = (
    "Sei un assistente vocale in italiano per un laboratorio di prova. "
    "Rispondi in italiano, in modo chiaro e conciso (poche frasi). "
    "Non inventare tool, file o azioni sul filesystem: in questa fase "
    "puoi solo chiacchierare. Se l'utente chiede di creare o leggere file, "
    "spiega che i tool arriveranno negli step successivi del lab."
)

# Comandi di uscita case-insensitive: allineati al main Phase 2 del progetto.
_EXIT_WORDS = frozenset({"esci", "exit", "quit"})


class SupportsChat(Protocol):
    """Contratto minimo del client LLM usato dal loop (testabile con mock)."""

    def chat(self, messages: list[dict[str, str]]) -> str: ...

    def close(self) -> None: ...


def run_chat_loop(
    stt: BaseSTT,
    tts: BaseTTS,
    llm: SupportsChat,
    *,
    # Limite turni: None = finché 'esci' / EOF (uso interattivo).
    max_turns: int | None = None,
    # Se True stampa su stderr la latenza warm di ogni chat (misura a occhio).
    report_latency: bool = True,
) -> int:
    """Un turno = listen → chat Ollama → speak; ritorna 0 in uscita normale.

    Side-effect: storia conversazione append-only; TTS stampa ogni risposta.
    Nessun tool: il modello riceve solo system + user/assistant precedenti.
    """
    # Messaggi Ollama: il system resta fisso in testa per tutto il loop.
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
    ]

    # Introduzione parlata: conferma che siamo nello Step 1 (solo chat).
    tts.speak(
        "Lab Ollama FS, step uno: solo chat, senza tool. "
        "Di' esci per terminare."
    )

    turns = 0
    while max_turns is None or turns < max_turns:
        # --- LISTENING: una riga da MockSTT = una “frase vocale” -----------
        user_text = stt.listen()

        # EOF / riga vuota → uscita soft (Ctrl+D o Enter a vuoto).
        if not user_text.strip():
            tts.speak("Nessun input. Uscita.")
            return 0

        # Stop esplicito: evita Ctrl+C durante le prove hands-free a terminale.
        if user_text.strip().casefold() in _EXIT_WORDS:
            tts.speak("Arrivederci.")
            return 0

        # Aggiungiamo l'utente alla storia prima della chiamata HTTP.
        messages.append({"role": "user", "content": user_text.strip()})

        # --- THINKING: chat non-stream verso qwen locale -------------------
        # time.perf_counter: latenza wall-clock warm (modello già in RAM).
        t0 = time.perf_counter()
        try:
            reply = llm.chat(messages)
        except OllamaError as exc:
            # Errore parlante: l'utente sente il problema senza stacktrace.
            # Rimuoviamo l'ultimo user così un retry non duplica il turno.
            messages.pop()
            tts.speak(f"Errore Ollama: {exc}")
            turns += 1
            continue
        elapsed = time.perf_counter() - t0

        # Risposta vuota dal modello: feedback chiaro invece di TTS muto.
        reply = (reply or "").strip()
        if not reply:
            messages.pop()
            tts.speak("Il modello non ha risposto. Riprova.")
            turns += 1
            continue

        # Append assistant: serve multi-turno (es. “ripeti in due parole”).
        messages.append({"role": "assistant", "content": reply})

        # Latenza su stderr: non sporca il canale [TTS] usato per assert/occhi.
        if report_latency:
            # Import lazy di sys: evita dipendenza a livello modulo per i test.
            import sys

            print(f"[lab] latenza chat: {elapsed:.2f}s", file=sys.stderr)

        # --- SPEAKING: MockTTS stampa [TTS] … ------------------------------
        tts.speak(reply)
        turns += 1

    return 0


def build_default_llm() -> LocalOllama:
    """Client LocalOllama puntato a config del lab (URL + modello Step 0)."""
    # Timeout alto: cold start qwen2.5:3b su Ryzen 3 può superare i 30s.
    return LocalOllama(base_url=OLLAMA_URL, model=OLLAMA_MODEL, timeout=180.0)
