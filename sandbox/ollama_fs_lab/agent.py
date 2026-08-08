"""Loop singolo agente: MockSTT → LocalOllama (JSON tool) → FsTools → MockTTS.

Step 2: oltre alla chat, il modello può chiamare `create_text_file` sul
workspace Desktop. Schema JSON stretto (format=json + extract) perché i 3B
sbagliano facilmente con tool nativi Ollama troppo aperti.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

from lavora_e_guida.audio.interface import BaseSTT, BaseTTS
from lavora_e_guida.llm.local_ollama import LocalOllama, OllamaError

from sandbox.ollama_fs_lab.config import OLLAMA_MODEL, OLLAMA_URL
from sandbox.ollama_fs_lab.tools_fs import FsToolError, create_text_file

# Prompt corto + schema unico: i 3B seguono meglio due forme fisse che enum lunghi.
# Solo create_text_file in Step 2; altri tool arriveranno negli step successivi.
_SYSTEM_PROMPT = (
    "Sei un assistente vocale in italiano per un laboratorio FS. "
    "Rispondi SEMPRE e SOLO con un oggetto JSON in uno di questi due formati:\n"
    '1) Tool: {"tool":"create_text_file","args":{"name":"<relativo>",'
    '"content":"<testo>"}}\n'
    '2) Risposta parlata: {"tool":"none","reply":"<testo italiano breve>"}\n'
    "Regole: name è relativo al workspace (es. spesa.txt), mai path assoluti "
    "né '..'. Dopo un esito tool, conferma all'utente con tool=none. "
    "Niente markdown, niente testo fuori dal JSON."
)

# Comandi di uscita case-insensitive: allineati al main Phase 2 del progetto.
_EXIT_WORDS = frozenset({"esci", "exit", "quit"})

# Nome tool registrato nello Step 2 (whitelist stretta → niente inventati).
_TOOL_CREATE = "create_text_file"

# Limite round tool per turno utente: evita loop infinito se il modello ripete.
_MAX_TOOL_ROUNDS = 4


class SupportsChat(Protocol):
    """Contratto minimo del client LLM usato dal loop (testabile con mock)."""

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        format_json: bool = False,
        options: dict[str, Any] | None = None,
    ) -> str: ...

    def close(self) -> None: ...


def _dispatch_tool(tool: str, args: dict[str, Any]) -> str:
    """Esegue un tool whitelist; ritorna stringa di esito per il modello.

    Side-effect: I/O FS solo via tools_fs (già confinato a WORKSPACE_ROOT).
    """
    # Solo create_text_file in Step 2: qualsiasi altro nome → errore parlante.
    if tool != _TOOL_CREATE:
        return (
            f"ERRORE: tool sconosciuto {tool!r}. "
            f"Consentito solo {_TOOL_CREATE!r} oppure tool=none."
        )

    # Il caller garantisce già un dict (args validi oppure {}).
    name = args.get("name")
    content = args.get("content", "")
    if not isinstance(name, str) or not name.strip():
        return "ERRORE: args.name deve essere una stringa non vuota (es. spesa.txt)."

    # content può essere int/list se il modello sbaglia tipo → forziamo str.
    try:
        return create_text_file(name, "" if content is None else str(content))
    except FsToolError as exc:
        # Path traversal / assoluti: messaggio già in italiano, pronto per TTS.
        return f"ERRORE: {exc}"
    except OSError as exc:
        # Disco pieno / permessi WSL→Windows: non propaghiamo stacktrace.
        return f"ERRORE I/O durante creazione file: {exc}"


def _parse_agent_json(raw: str) -> dict[str, Any]:
    """Estrae oggetto JSON da risposta modello (anche con rumore intorno)."""
    return LocalOllama.extract_json_object(raw)


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
    """Un turno = listen → (tool JSON)* → reply TTS; ritorna 0 in uscita normale.

    Side-effect: storia conversazione append-only; FS solo se il modello
    chiama create_text_file; TTS stampa ogni reply finale.
    """
    # Messaggi Ollama: il system resta fisso in testa per tutto il loop.
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
    ]

    # Introduzione parlata: conferma Step 2 (create_text_file attivo).
    tts.speak(
        "Lab Ollama FS, step due: posso creare file di testo sul Desktop. "
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

        # --- THINKING + TOOLS: fino a reply o esaurimento round ------------
        spoken = False
        for _round in range(_MAX_TOOL_ROUNDS):
            t0 = time.perf_counter()
            try:
                # format_json: spinge qwen a emettere un oggetto; temperature
                # bassa riduce tool inventati / campi extra.
                raw = llm.chat(
                    messages,
                    format_json=True,
                    options={"temperature": 0.1},
                )
            except OllamaError as exc:
                # Errore parlante: l'utente sente il problema senza stacktrace.
                # Rimuoviamo l'ultimo user così un retry non duplica il turno.
                messages.pop()
                tts.speak(f"Errore Ollama: {exc}")
                spoken = True
                break
            elapsed = time.perf_counter() - t0

            if report_latency:
                import sys

                print(f"[lab] latenza chat: {elapsed:.2f}s", file=sys.stderr)

            raw = (raw or "").strip()
            if not raw:
                messages.pop()
                tts.speak("Il modello non ha risposto. Riprova.")
                spoken = True
                break

            # Append assistant grezzo: serve al modello vedere la propria call.
            messages.append({"role": "assistant", "content": raw})

            try:
                parsed = _parse_agent_json(raw)
            except OllamaError:
                # JSON rotto tipico dei 3B: chiediamo una sola retry strutturata.
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "JSON non valido. Rispondi SOLO con "
                            '{"tool":"none","reply":"..."} oppure '
                            '{"tool":"create_text_file","args":'
                            '{"name":"...","content":"..."}}.'
                        ),
                    }
                )
                continue

            tool = str(parsed.get("tool") or "").strip()
            # Alias comuni: alcuni 3B usano action/reply senza tool=none.
            if tool in ("", "none", "reply", "speak"):
                reply = parsed.get("reply") or parsed.get("text") or parsed.get("message")
                reply_s = (str(reply).strip() if reply is not None else "")
                if not reply_s:
                    # Tool=none senza testo: spingiamo una conferma breve.
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                'Manca reply. Rispondi con '
                                '{"tool":"none","reply":"frase breve in italiano"}.'
                            ),
                        }
                    )
                    continue
                tts.speak(reply_s)
                spoken = True
                break

            # --- TOOL EXEC: Python esegue, risultato torna al modello ------
            args = parsed.get("args")
            if args is None and "name" in parsed:
                # Fallback: modello mette name/content a top-level invece di args.
                args = {"name": parsed.get("name"), "content": parsed.get("content", "")}
            result = _dispatch_tool(tool, args if isinstance(args, dict) else {})

            # Ruolo user con esito: format collaudato senza dipendere da role=tool.
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Esito tool {tool}: {result}. "
                        'Ora rispondi con {"tool":"none","reply":"conferma breve"}.'
                    ),
                }
            )

        if not spoken:
            # Troppi round senza reply: feedback chiaro, non silenzio.
            tts.speak(
                "Non sono riuscito a completare l'azione in questo turno. Riprova."
            )

        turns += 1

    return 0


def build_default_llm() -> LocalOllama:
    """Client LocalOllama puntato a config del lab (URL + modello Step 0)."""
    # Timeout alto: cold start qwen2.5:3b su Ryzen 3 può superare i 30s.
    return LocalOllama(base_url=OLLAMA_URL, model=OLLAMA_MODEL, timeout=180.0)
