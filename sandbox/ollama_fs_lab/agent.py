"""Loop singolo agente: MockSTT → LocalOllama (JSON tool) → FsTools → MockTTS.

Step 2–6: il modello può chiamare create/append/`read_file` (testo + PDF) sul
workspace Desktop. Schema JSON stretto (format=json + extract) perché i 3B
sbagliano facilmente con tool nativi Ollama troppo aperti.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Protocol

from lavora_e_guida.audio.interface import BaseSTT, BaseTTS
from lavora_e_guida.llm.local_ollama import LocalOllama, OllamaError

from sandbox.ollama_fs_lab.config import OLLAMA_MODEL, OLLAMA_URL
from sandbox.ollama_fs_lab.tools_fs import (
    FsToolError,
    append_note,
    create_text_file,
    read_file,
)
from sandbox.ollama_fs_lab.tools_find import FindToolError, find_file
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
    '- Leggi file: {"tool": "read_file", "args": {"name": "string"}}\n'
    '- Cerca per contenuto: {"tool": "find_file", "args": {"query": "string"}}\n'
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
    "e la conferma in 'reply'. Non richiamare lo stesso tool.\n\n"
    "ESEMPIO CORRETTO:\n"
    "Utente: Aggiungi latte alla lista spesa\n"
    'JSON: {"tool": "append_note", "args": {"name": "spesa", "content": "latte"}}\n'
    "Esito tool append_note: OK: riga aggiunta a notes/spesa.txt\n"
    'JSON: {"tool": "none", "reply": "Ho aggiunto latte alla spesa"}'
)

# Comandi di uscita case-insensitive: allineati al main Phase 2 del progetto.
_EXIT_WORDS = frozenset({"esci", "exit", "quit"})

# Whitelist tool Step 2–6: qualunque altro nome → errore parlante (anti-invenzione).
_TOOL_CREATE = "create_text_file"
_TOOL_APPEND = "append_note"
_TOOL_READ = "read_file"
_TOOL_FIND = "find_file"
_ALLOWED_TOOLS = frozenset({_TOOL_CREATE, _TOOL_APPEND, _TOOL_READ, _TOOL_FIND})

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

    Side-effect: I/O FS solo via tools_fs (confinato a WORKSPACE_ROOT).
    """
    # Whitelist stretta: i 3B inventano nomi tool; rifiutiamo subito.
    if tool not in _ALLOWED_TOOLS:
        allowed = ", ".join(sorted(_ALLOWED_TOOLS))
        return (
            f"ERRORE: tool sconosciuto {tool!r}. "
            f"Consentiti: {allowed} oppure tool=none."
        )

    try:
        if tool == _TOOL_FIND:
            query = args.get("query")
            if not isinstance(query, str) or not query.strip():
                return (
                    "ERRORE: args.query deve essere una stringa non vuota "
                    "(es. dove ho scritto dei cetrioli)."
                )
            return find_file(query)

        # Il caller garantisce già un dict (args validi oppure {}).
        name = args.get("name")
        if not isinstance(name, str) or not name.strip():
            return "ERRORE: args.name deve essere una stringa non vuota (es. spesa.txt)."

        if tool == _TOOL_READ:
            # Solo name: testo o PDF; resolve + dispatch pypdf in tools_fs.
            return read_file(name)

        # create + append: stesso campo `content` (schema unificato nel prompt).
        # Fallback `text`: alcuni 3B riusano ancora la chiave legacy.
        raw_content = args.get("content", args.get("text", ""))
        content = "" if raw_content is None else str(raw_content)

        if tool == _TOOL_CREATE:
            return create_text_file(name, content)

        # Whitelist già filtrata: qui resta solo append_note.
        return append_note(name, content)
    except (FsToolError, FindToolError) as exc:
        # Path traversal / assenti / RAG: messaggio già in italiano.
        return f"ERRORE: {exc}"
    except OSError as exc:
        # Disco pieno / permessi WSL→Windows: non propaghiamo stacktrace.
        return f"ERRORE I/O durante operazione file: {exc}"


def _followup_after_tool(tool: str, result: str) -> str:
    """Messaggio user dopo l'esecuzione di un tool.

    Chiude con un vincolo imperativo concreto (niente placeholder): i 3B
    altrimenti ripetono lo stesso tool invece di passare a tool=none.
    """
    # Esito grezzo + regola post-tool in una sola chiusura (budget attenzione).
    return (
        f"Esito tool {tool}: {result}\n"
        "Ora rispondi SOLO con tool none e la conferma o la risposta in reply. "
        f"Non richiamare {tool}."
    )


def _print_find_file_to_terminal(result: str) -> bool:
    """Stampa chunk RAG su stdout (prefisso [RAG]) se find_file ha avuto successo."""
    if not result.startswith("OK:"):
        return False
    if "\n---\n" in result:
        header, body = result.split("\n---\n", 1)
        print(f"[RAG] {header}")
        print(body, end="" if body.endswith("\n") else "\n")
    else:
        print(f"[RAG] {result}")
    return True


def _print_read_file_to_terminal(result: str) -> bool:
    """Stampa a stdout il contenuto letto da read_file (prefisso [FS]).

    Side-effect: print su stdout. Ritorna True se ha stampato un OK.
    Il TTS resta sul reply del modello; qui mostriamo il testo al terminale.
    """
    # Solo esiti positivi: errori restano nel follow-up verso l'LLM.
    if not result.startswith("OK:"):
        return False
    # Prima riga = header (path + lunghezza); resto = corpo del file.
    if "\n" in result:
        header, body = result.split("\n", 1)
        print(f"[FS] {header}")
        # Evita doppia newline se il file termina già con \\n.
        print(body, end="" if body.endswith("\n") else "\n")
    else:
        print(f"[FS] {result}")
    return True


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
        'oppure {"tool":"read_file","args":{"name":"string"}} '
        'oppure {"tool":"find_file","args":{"query":"string"}}.'
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

    # Introduzione parlata: un solo tool di lettura (testo + PDF).
    tts.speak(
        "Lab Ollama FS: posso creare, aggiornare e leggere file, "
        "cercare per contenuto con find file, sul Desktop. Di' esci per terminare."
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
        # Chiave tool|name già eseguita nel turno: anti-loop sui 3B.
        prev_tool_key: str | None = None
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
            args_dict = args if isinstance(args, dict) else {}
            # Chiave anti-loop: find_file usa query, gli altri name.
            if tool == _TOOL_FIND:
                tool_key = f"{tool}|{str(args_dict.get('query') or '').strip().casefold()}"
            else:
                tool_name_arg = str(args_dict.get("name") or "")
                tool_key = f"{tool}|{tool_name_arg.strip().casefold()}"
            # Anti-loop: i 3B ripetono read_file dopo Esito OK; Python interrompe.
            if prev_tool_key is not None and tool_key == prev_tool_key:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Hai già ricevuto l'Esito di questo tool. "
                            "Rispondi ora SOLO con tool none e la conferma in reply. "
                            "Non richiamare lo stesso tool."
                        ),
                    }
                )
                continue

            result = _dispatch_tool(tool, args_dict)

            # read_file / find_file: stampa subito il corpo a terminale.
            if tool == _TOOL_READ:
                _print_read_file_to_terminal(result)
            elif tool == _TOOL_FIND:
                _print_find_file_to_terminal(result)

            # Ruolo user con esito + vincolo tool=none (anti-ripetizione 3B).
            prev_tool_key = tool_key
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
    # Timeout alto: cold start qwen2.5:3b su Ryzen 3 può superare i 5 minuti.
    return LocalOllama(base_url=OLLAMA_URL, model=OLLAMA_MODEL, timeout=500.0)
