"""Loop vocale riusabile: STT → LLM (JSON tool) → dispatch dello spec → TTS.

Il default è il master FS (create/append/read/find sul Desktop). Gli specialisti
passano un `LoopSpec` diverso; questo modulo non importa i tool Gmail.
Schema JSON stretto (format=json + extract): contratto del loop, non del solo 3B.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from lavora_e_guida.audio.interface import BaseSTT, BaseTTS
from lavora_e_guida.config import (
    GEMINI_MODEL,
    OLLAMA_MODEL,
    OLLAMA_URL,
    TELEMETRY_DB,
)
from lavora_e_guida.llm.cloud import GeminiChat
from lavora_e_guida.llm.errors import LLMError
from lavora_e_guida.llm.local_ollama import LocalOllama
from lavora_e_guida.llm.usage import TokenUsage
from lavora_e_guida.telemetry import TelemetryDB, utc_now_iso
from lavora_e_guida.tools.find import FindToolError, find_file
from lavora_e_guida.tools.fs import (
    FsToolError,
    append_note,
    create_text_file,
    read_file,
)

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

# Intro TTS del master: non è una reply LLM, quindi niente riga telemetria.
_MASTER_INTRO_TEXT = (
    "Lab Ollama FS: posso creare, aggiornare e leggere file, "
    "cercare per contenuto con find file, sul Desktop. Di' esci per terminare."
)

# Comandi di uscita case-insensitive: allineati all'entrypoint vocale.
_EXIT_WORDS = frozenset({"esci", "exit", "quit"})

# Whitelist tool Step 2–6: qualunque altro nome → errore parlante (anti-invenzione).
_TOOL_CREATE = "create_text_file"
_TOOL_APPEND = "append_note"
_TOOL_READ = "read_file"
_TOOL_FIND = "find_file"
_ALLOWED_TOOLS = frozenset({_TOOL_CREATE, _TOOL_APPEND, _TOOL_READ, _TOOL_FIND})

# Limite round tool per turno utente: evita loop infinito se il modello ripete.
_MAX_TOOL_ROUNDS = 4


# Dispatch: Python esegue il tool; ritorna stringa di esito per il modello.
ToolDispatch = Callable[[str, dict[str, Any]], str]
# Stampa esito su stdout ([FS], [RAG], [GMAIL]); lo spec decide il prefisso.
ToolResultPrinter = Callable[[str, str], None]


@dataclass(frozen=True)
class LoopSpec:
    """Contratto minimo del loop: prompt, tool, intro TTS, recovery JSON.

    Default = master FS (`MASTER_LOOP_SPEC`). Uno specialista (Gmail) passa
    un'istanza propria; il master non importa i tool email.
    """

    # System prompt fisso in testa alla storia per tutta la sessione.
    system_prompt: str
    # Esegue un tool e ritorna l'esito parlante (OK: / ERRORE:).
    dispatch: ToolDispatch
    # Prima frase TTS all'avvio: identità dell'agente, non reply LLM.
    intro_text: str
    # Messaggio user di recovery se il JSON del modello è rotto.
    schema_hint: str
    # None = nessun dump a terminale (solo follow-up verso l'LLM).
    print_tool_result: ToolResultPrinter | None = None


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


def _print_master_tool_result(tool: str, result: str) -> None:
    """Stdout del master: [FS] per read_file, [RAG] per find_file.

    Gli altri tool (create/append) non dumpano il corpo: basta il TTS.
    Uno spec Gmail userà analogamente un prefisso [GMAIL].
    """
    # Stesso filtro OK: gli ERRORE restano solo nel follow-up all'LLM.
    if tool == _TOOL_READ:
        _print_read_file_to_terminal(result)
    elif tool == _TOOL_FIND:
        _print_find_file_to_terminal(result)


def _tool_loop_key(tool: str, args: dict[str, Any]) -> str:
    """Chiave anti-ripetizione nel turno: query se presente, altrimenti name.

    find_file e list_emails identificano la ricerca con `query`; read_file e
    read_email usano `name`. Preferire query evita collisioni se entrambi
    i campi arrivano nello stesso args (i 3B a volte mischiano le chiavi).
    """
    query = args.get("query")
    # Stringa non vuota: è una ricerca, non un identificatore di file/mail.
    if isinstance(query, str) and query.strip():
        identity = query.strip().casefold()
    else:
        identity = str(args.get("name") or "").strip().casefold()
    return f"{tool}|{identity}"


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


# Spec di default: stesso prompt, dispatch, intro e stampa del master FS.
# I test e `lavora-e-guida` senza --agent restano invariati.
MASTER_LOOP_SPEC = LoopSpec(
    system_prompt=_SYSTEM_PROMPT,
    dispatch=_dispatch_tool,
    intro_text=_MASTER_INTRO_TEXT,
    schema_hint=_json_schema_hint(),
    print_tool_result=_print_master_tool_result,
)


def _record_stt_turn(
    store: TelemetryDB,
    *,
    started_at: str,
    usage: TokenUsage,
    tts_response: str,
) -> None:
    """Insert riga `stt_requests` dopo lo speak finale del turno.

    Contratto: non alza eccezioni (lo store logga e ritorna False).
    Un turno con N round tool → una riga, token già sommati in `usage`.
    """
    # ended_at dopo tts.speak: include la durata della reply parlata.
    store.insert_stt_request(
        started_at=started_at,
        ended_at=utc_now_iso(),
        token_input=usage.prompt_tokens,
        token_output=usage.completion_tokens,
        tts_response=tts_response,
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
    # Path file SQLite; None → TELEMETRY_DB (INDEX_ROOT / telemetry.db).
    # I test passano tmp_path / "telemetry.db" per non toccare il DB reale.
    telemetry_db: Path | None = None,
    # None → master FS: prompt, dispatch e intro attuali restano il default.
    spec: LoopSpec | None = None,
) -> int:
    """Un turno = listen → (tool JSON)* → reply TTS; ritorna 0 in uscita normale.

    Side-effect: storia conversazione append-only; tool dello spec (FS di
    default); TTS stampa ogni reply finale; una riga telemetria per turno
    vocale valido (non intro / riga vuota / esci).
    """
    # Default esplicito: chi non passa spec ottiene il master, non uno vuoto.
    loop_spec = spec if spec is not None else MASTER_LOOP_SPEC

    # Messaggi LLM: il system dello spec resta fisso in testa per tutto il loop.
    messages: list[dict[str, str]] = [
        {"role": "system", "content": loop_spec.system_prompt},
    ]

    # Introduzione parlata dallo spec: non è una reply LLM, niente telemetria.
    tts.speak(loop_spec.intro_text)

    # Lazy connect: il file SQLite nasce al primo insert, non all'avvio.
    store = TelemetryDB(telemetry_db if telemetry_db is not None else TELEMETRY_DB)
    try:
        return _run_chat_loop_body(
            stt,
            tts,
            llm,
            messages,
            store,
            loop_spec,
            max_turns=max_turns,
            report_latency=report_latency,
        )
    finally:
        # Chiude SQLite anche su return anticipato (esci / EOF).
        store.close()


def _run_chat_loop_body(
    stt: BaseSTT,
    tts: BaseTTS,
    llm: SupportsChat,
    messages: list[dict[str, str]],
    store: TelemetryDB,
    spec: LoopSpec,
    *,
    max_turns: int | None,
    report_latency: bool,
) -> int:
    """Corpo del loop: listen → chat/tool → speak + insert telemetria."""
    turns = 0
    while max_turns is None or turns < max_turns:
        # --- LISTENING: una riga da MockSTT = una “frase vocale” -----------
        user_text = stt.listen()

        # EOF / riga vuota → uscita soft (Ctrl+D o Enter a vuoto).
        # Nessuna riga telemetria: non c'è stata una richiesta LLM.
        if not user_text.strip():
            tts.speak("Nessun input. Uscita.")
            return 0

        # Stop esplicito: evita Ctrl+C durante le prove hands-free a terminale.
        if user_text.strip().casefold() in _EXIT_WORDS:
            tts.speak("Arrivederci.")
            return 0

        # Transcript valido: parte il turno telemetria (clock + token a zero).
        started_at = utc_now_iso()
        usage = TokenUsage()

        # Aggiungiamo l'utente alla storia prima della chiamata HTTP.
        messages.append({"role": "user", "content": user_text.strip()})

        # --- THINKING + TOOLS: fino a reply o esaurimento round ------------
        spoken = False
        # Chiave tool|query-o-name già eseguita nel turno: anti-loop sui 3B.
        prev_tool_key: str | None = None
        for _round in range(_MAX_TOOL_ROUNDS):
            t0 = time.perf_counter()
            try:
                # format_json: spinge qwen/GPT a emettere un oggetto; temperature
                # bassa riduce tool inventati / campi extra.
                raw = llm.chat(
                    messages,
                    format_json=True,
                    options={"temperature": 0.1},
                )
            except LLMError as exc:
                # Errore parlante: l'utente sente il problema senza stacktrace.
                # Rimuoviamo l'ultimo user così un retry non duplica il turno.
                messages.pop()
                # Ollama resta etichettato "Ollama"; Gemini (e altri) → "LLM".
                err_label = "Ollama" if isinstance(llm, LocalOllama) else "LLM"
                spoken_text = f"Errore {err_label}: {exc}"
                tts.speak(spoken_text)
                _record_stt_turn(
                    store,
                    started_at=started_at,
                    usage=usage,
                    tts_response=spoken_text,
                )
                spoken = True
                break
            elapsed = time.perf_counter() - t0

            # chat() ok: sommiamo last_usage (assente sul mock → 0+0).
            usage = usage + getattr(llm, "last_usage", TokenUsage())

            if report_latency:
                print(f"[lab] latenza chat: {elapsed:.2f}s", file=sys.stderr)

            raw = (raw or "").strip()
            if not raw:
                messages.pop()
                spoken_text = "Il modello non ha risposto. Riprova."
                tts.speak(spoken_text)
                _record_stt_turn(
                    store,
                    started_at=started_at,
                    usage=usage,
                    tts_response=spoken_text,
                )
                spoken = True
                break

            # Append assistant grezzo: serve al modello vedere la propria call.
            messages.append({"role": "assistant", "content": raw})

            try:
                parsed = _parse_agent_json(raw)
            except LLMError:
                # JSON rotto: retry con lo schema dello spec (master o specialista).
                messages.append(
                    {
                        "role": "user",
                        "content": spec.schema_hint,
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
                _record_stt_turn(
                    store,
                    started_at=started_at,
                    usage=usage,
                    tts_response=reply_s,
                )
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
            # Chiave anti-loop: query se presente (find/list), altrimenti name.
            tool_key = _tool_loop_key(tool, args_dict)
            # Anti-loop: i 3B ripetono lo stesso tool dopo Esito OK; Python interrompe.
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

            # Dispatch dello spec: master → FS/RAG; Gmail → list/read email.
            result = spec.dispatch(tool, args_dict)

            # Dump stdout analogo a [FS]/[RAG]; lo spec sceglie prefisso e filtro.
            if spec.print_tool_result is not None:
                spec.print_tool_result(tool, result)

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
            spoken_text = (
                "Non sono riuscito a completare l'azione in questo turno. Riprova."
            )
            tts.speak(spoken_text)
            _record_stt_turn(
                store,
                started_at=started_at,
                usage=usage,
                tts_response=spoken_text,
            )

        turns += 1

    return 0


def build_llm(
    provider: Literal["ollama", "gemini"] = "ollama",
    model: str | None = None,
) -> LocalOllama | GeminiChat:
    """Factory provider: ollama (default) oppure gemini (stesso contratto chat).

    `model` None → OLLAMA_MODEL oppure GEMINI_MODEL (env / default gemini-3.5-flash).
    """
    if provider == "gemini":
        # Timeout generoso: rete pubblica + eventuale cold start lato API.
        return GeminiChat(model=model or GEMINI_MODEL, timeout=120.0)

    # Timeout alto: cold start qwen2.5:3b su Ryzen 3 può superare i 5 minuti.
    return LocalOllama(
        base_url=OLLAMA_URL,
        model=model or OLLAMA_MODEL,
        timeout=500.0,
    )


def build_default_llm() -> LocalOllama:
    """Retrocompat: stesso di `build_llm(\"ollama\")` (URL + modello Step 0)."""
    # Cast implicito: con provider ollama la factory restituisce sempre LocalOllama.
    llm = build_llm("ollama")
    assert isinstance(llm, LocalOllama)
    return llm
