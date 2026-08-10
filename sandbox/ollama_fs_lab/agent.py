"""Loop singolo agente: MockSTT → LocalOllama (JSON tool) → FsTools → MockTTS.

Step 2–6: il modello può chiamare create/append/read testo e `read_pdf` sul
workspace Desktop. Schema JSON stretto (format=json + extract) perché i 3B
sbagliano facilmente con tool nativi Ollama troppo aperti.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

from lavora_e_guida.audio.interface import BaseSTT, BaseTTS
from lavora_e_guida.llm.local_ollama import LocalOllama, OllamaError

from sandbox.ollama_fs_lab.config import OLLAMA_MODEL, OLLAMA_URL
from sandbox.ollama_fs_lab.tools_fs import (
    FsToolError,
    append_note,
    create_text_file,
    read_text_file,
)
from sandbox.ollama_fs_lab.tools_pdf import read_pdf

# Schema unico name+content per create/append: meno campi = meno errori sui 3B.
# Path resolve resta solo in Python; il modello vede solo name/content/reply.
_SYSTEM_PROMPT = (
    "Sei l'assistente vocale del laboratorio. Rispondi ESCLUSIVAMENTE con un "
    "oggetto JSON valido. "
    "Non usare mai blocchi markdown ```json. Nessun testo prima o dopo il JSON.\n\n"
    "TOOL DISPONIBILI E SCHEMI JSON:\n"
    '- Crea file: {"tool": "create_text_file", "args": {"name": "string", '
    '"content": "string"}}\n'
    '- Aggiorna file: {"tool": "append_note", "args": {"name": "string", '
    '"content": "string"}}\n'
    '- Leggi file: {"tool": "read_text_file", "args": {"name": "string"}}\n'
    '- Leggi PDF: {"tool": "read_pdf", "args": {"name": "string"}}\n'
    '- Risposta parlata: {"tool": "none", "reply": "string"}\n\n'
    "REGOLE TASSATIVE:\n"
    "1. Emetti UN SOLO oggetto JSON con UN SOLO tool per risposta.\n"
    "2. Usa la chiave 'content' sia per create_text_file che per append_note "
    "per specificare il testo da scrivere.\n"
    "3. Estrai SEMPRE dall'input dell'utente sia il nome del file che "
    "l'elemento da aggiungere/scrivere.\n"
    "4. Se l'utente dice 'Aggiungi cetrioli alla spesa', il JSON deve "
    "contenere 'name': 'spesa' e 'content': 'cetrioli'.\n"
    "5. Dopo un Esito OK o ERRORE di un tool, rispondi SEMPRE con tool='none' "
    "e la conferma in 'reply'.\n\n"
    "ESEMPIO CORRETTO:\n"
    "Utente: Aggiungi latte alla lista spesa\n"
    'JSON: {"tool": "append_note", "args": {"name": "spesa", "content": "latte"}}'
)

# Comandi di uscita case-insensitive: allineati al main Phase 2 del progetto.
_EXIT_WORDS = frozenset({"esci", "exit", "quit"})

# Whitelist tool Step 2–6: qualunque altro nome → errore parlante (anti-invenzione).
_TOOL_CREATE = "create_text_file"
_TOOL_APPEND = "append_note"
_TOOL_READ = "read_text_file"
_TOOL_PDF = "read_pdf"
_ALLOWED_TOOLS = frozenset({_TOOL_CREATE, _TOOL_APPEND, _TOOL_READ, _TOOL_PDF})

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

    Side-effect: I/O FS solo via tools_fs / tools_pdf (confinato a WORKSPACE_ROOT).
    """
    # Whitelist stretta: i 3B inventano nomi tool; rifiutiamo subito.
    if tool not in _ALLOWED_TOOLS:
        allowed = ", ".join(sorted(_ALLOWED_TOOLS))
        return (
            f"ERRORE: tool sconosciuto {tool!r}. "
            f"Consentiti: {allowed} oppure tool=none."
        )

    # Il caller garantisce già un dict (args validi oppure {}).
    name = args.get("name")
    if not isinstance(name, str) or not name.strip():
        return "ERRORE: args.name deve essere una stringa non vuota (es. spesa.txt)."

    try:
        if tool == _TOOL_READ:
            # Solo name: la lettura non ha body; path risolto in tools_fs.
            return read_text_file(name)

        if tool == _TOOL_PDF:
            # Solo name: estrazione pypdf; path risolto in tools_pdf.
            return read_pdf(name)

        # create + append: stesso campo `content` (schema unificato nel prompt).
        # Fallback `text`: alcuni 3B riusano ancora la chiave legacy.
        raw_content = args.get("content", args.get("text", ""))
        content = "" if raw_content is None else str(raw_content)

        if tool == _TOOL_CREATE:
            return create_text_file(name, content)

        # Whitelist già filtrata: qui resta solo append_note.
        return append_note(name, content)
    except FsToolError as exc:
        # Path traversal / assoluti / file assente: messaggio già in italiano.
        return f"ERRORE: {exc}"
    except OSError as exc:
        # Disco pieno / permessi WSL→Windows: non propaghiamo stacktrace.
        return f"ERRORE I/O durante operazione file: {exc}"


def _followup_after_tool(tool: str, result: str) -> str:
    """Messaggio user dopo l'esecuzione di un tool.

    Fornisce solo i dati dell'esito senza template o suggerimenti di formattazione,
    evitando che l'LLM ricopi i placeholder nel campo reply.
    """
    # Stesso formato per create/append/read/pdf: l'LLM vede solo l'esito grezzo.
    return f"Esito tool {tool}: {result}"


def _parse_agent_json(raw: str) -> dict[str, Any]:
    """Estrae oggetto JSON da risposta modello (anche con rumore intorno)."""
    return LocalOllama.extract_json_object(raw)


def _json_schema_hint() -> str:
    """Messaggio di recovery quando il JSON del modello è rotto o incompleto.

    Tipi `"string"` (non `...` / `<...>`): evita pattern echoing sui 3B.
    """
    return (
        "JSON non valido. Emetti UN SOLO oggetto JSON. Schema ammesso: "
        '{"tool":"none","reply":"string"} oppure '
        '{"tool":"create_text_file","args":{"name":"string","content":"string"}} '
        'oppure {"tool":"append_note","args":{"name":"string","content":"string"}} '
        'oppure {"tool":"read_text_file","args":{"name":"string"}} '
        'oppure {"tool":"read_pdf","args":{"name":"string"}}.'
    )


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

    Side-effect: storia conversazione append-only; FS/PDF se il modello chiama
    i tool; TTS stampa ogni reply finale (contenuto, Q&A o riassunto).
    """
    # Messaggi Ollama: il system resta fisso in testa per tutto il loop.
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
    ]

    # Introduzione parlata: conferma Step 6 (anche PDF da inbox/).
    tts.speak(
        "Lab Ollama FS, step sei: posso creare, aggiornare, leggere testo e "
        "PDF da inbox sul Desktop. Di' esci per terminare."
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
                        "content": _json_schema_hint(),
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
                # Fallback: modello mette name/content a top-level (senza args).
                # Accettiamo anche `text` legacy e lo normalizziamo a `content`.
                top_content = parsed.get("content", parsed.get("text", ""))
                args = {
                    "name": parsed.get("name"),
                    "content": top_content,
                }
            result = _dispatch_tool(tool, args if isinstance(args, dict) else {})

            # Ruolo user con esito: format collaudato senza dipendere da role=tool.
            messages.append(
                {
                    "role": "user",
                    "content": _followup_after_tool(tool, result),
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
    # Timeout alto: cold start qwen2.5:3b su Ryzen 3 può superare i 3 minuti.
    return LocalOllama(base_url=OLLAMA_URL, model=OLLAMA_MODEL, timeout=300.0)
