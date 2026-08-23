"""Loop vocale riusabile: STT → Gemini (functionCall / testo) → dispatch → TTS.

Il default è lo specialista FS (`master` in CLI: create/append/read/find sul
Desktop). Gli specialisti passano un `LoopSpec` diverso; questo modulo non
importa i tool Gmail. Function calling nativo: niente JSON `{"tool","args"}`.
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
from lavora_e_guida.llm.spoken import SPOKEN_REPLY_RULE, prepare_spoken_text
from lavora_e_guida.llm.turn import FunctionCall, LlmTurn
from lavora_e_guida.llm.usage import TokenUsage
from lavora_e_guida.telemetry import TelemetryDB, utc_now_iso
from lavora_e_guida.tools.catalog import (
    ToolDeclaration,
    object_schema,
    string_param,
    to_gemini_tools,
)
from lavora_e_guida.tools.find import FindToolError, find_file
from lavora_e_guida.tools.fs import (
    FsToolError,
    append_note,
    create_text_file,
    read_file,
)

# Prompt: identità e regole. Gli schemi stanno nelle functionDeclarations.
# SPOKEN_REPLY_RULE è condiviso con Gmail: stesso vincolo TTS su ogni specialista.
_SYSTEM_PROMPT = (
    "Sei l'assistente vocale del laboratorio sul Desktop. "
    "Usa i tool per creare, aggiornare, leggere e cercare file. "
    "Dopo un tool, conferma in italiano all'utente con una frase breve. "
    "Un solo tool per enunciato. Non inventare path. "
    "Il path lo risolve Python: tu passi solo name o query.\n\n"
    "Esempio: l'utente dice «Aggiungi latte alla spesa» → "
    "chiama append_note con name spesa e content latte. "
    "Poi conferma a voce che hai aggiunto latte.\n\n"
    f"{SPOKEN_REPLY_RULE}"
)

# Intro TTS dello specialista FS (CLI --agent master): non è una reply LLM.
_MASTER_INTRO_TEXT = (
    "Assistente file sul Desktop: posso creare, aggiornare e leggere file, "
    "cercare per contenuto con find file. Di' esci per terminare."
)

# Comandi di uscita case-insensitive: allineati all'entrypoint vocale.
_EXIT_WORDS = frozenset({"esci", "exit", "quit"})

# Nomi tool FS: whitelist anti-invenzione (Gemini a volte inventa funzioni).
_TOOL_CREATE = "create_text_file"
_TOOL_APPEND = "append_note"
_TOOL_READ = "read_file"
_TOOL_FIND = "find_file"

# Catalogo FS: stesso gesto Impesud, involucro Gemini (vedi to_gemini_tools).
FS_TOOL_DECLARATIONS: tuple[ToolDeclaration, ...] = (
    ToolDeclaration(
        name=_TOOL_CREATE,
        description=(
            "Crea un file di testo sul Desktop (notes/ o inbox/). "
            "Usa name per il file (es. spesa) e content per il testo iniziale."
        ),
        parameters=object_schema(
            {
                "name": string_param("Nome del file, senza path (es. spesa)."),
                "content": string_param("Testo da scrivere nel file nuovo."),
            },
            required=("name", "content"),
        ),
    ),
    ToolDeclaration(
        name=_TOOL_APPEND,
        description=(
            "Aggiunge una riga a una nota esistente sul Desktop. "
            "Se il file non c'è, Python lo crea sotto notes/. "
            "Esempio: name spesa, content latte."
        ),
        parameters=object_schema(
            {
                "name": string_param("Nome della nota (es. spesa)."),
                "content": string_param("Testo da aggiungere (es. latte)."),
            },
            required=("name", "content"),
        ),
    ),
    ToolDeclaration(
        name=_TOOL_READ,
        description=(
            "Legge un file di testo o PDF sul Desktop. "
            "Passa name (es. spesa o il titolo del PDF). Non inventare path."
        ),
        parameters=object_schema(
            {
                "name": string_param("Nome del file da leggere."),
            },
            required=("name",),
        ),
    ),
    ToolDeclaration(
        name=_TOOL_FIND,
        description=(
            "Cerca nei file del Desktop per contenuto (RAG). "
            "Usa query con le parole dell'utente (es. dove ho scritto cetrioli)."
        ),
        parameters=object_schema(
            {
                "query": string_param("Frase di ricerca in italiano."),
            },
            required=("query",),
        ),
    ),
)

# Mappa nome → presenza: il dispatch usa questa whitelist, non importa Gmail.
FS_TOOL_MAP: dict[str, Any] = {
    _TOOL_CREATE: create_text_file,
    _TOOL_APPEND: append_note,
    _TOOL_READ: read_file,
    _TOOL_FIND: find_file,
}

_ALLOWED_TOOLS = frozenset(FS_TOOL_MAP)

# Limite round tool per turno utente: evita loop se il modello ripete la call.
_MAX_TOOL_ROUNDS = 4

# Body Gemini: functionDeclarations dello specialista FS.
FS_GEMINI_TOOLS = to_gemini_tools(FS_TOOL_DECLARATIONS)


# Dispatch: Python esegue il tool; ritorna stringa di esito per functionResponse.
ToolDispatch = Callable[[str, dict[str, Any]], str]
# Stampa esito su stdout ([FS], [RAG], [GMAIL]); lo spec decide il prefisso.
ToolResultPrinter = Callable[[str, str], None]
# Dopo un tool: se ritorna testo, il loop lo parla e attende HITL (niente LLM).
HitlAfterTool = Callable[[str, str], str | None]
# All'ascolto: se ritorna testo, è gestito (sì/no) e non passa da Gemini.
HitlOnUtterance = Callable[[str], str | None]


@dataclass(frozen=True)
class AgentSpec:
    """Dato statico di uno specialista (nome, ruolo, tool ammessi, dispatch).

    Non è il motore del loop: `LoopSpec` avvolge prompt + tools Gemini + HITL.
    `master` in CLI è lo specialista FS (`name='fs'`), non un router.
    """

    name: str
    role: str
    goal: str
    tools: tuple[str, ...]
    dispatch: ToolDispatch
    intro_text: str
    backstory: str = ""


@dataclass(frozen=True)
class LoopSpec:
    """Contratto del loop: prompt, dispatch, intro TTS, catalogo Gemini, HITL.

    Default = specialista FS (`MASTER_LOOP_SPEC`). Gmail passa un'istanza
    propria; il master non importa i tool email.
    """

    # System prompt fisso in testa alla storia per tutta la sessione.
    system_prompt: str
    # Esegue un tool e ritorna l'esito parlante (OK: / ERRORE:).
    dispatch: ToolDispatch
    # Prima frase TTS all'avvio: identità dell'agente, non reply LLM.
    intro_text: str
    # Catalogo già avvolto: lista `tools` per generateContent (None = nessuno).
    gemini_tools: list[dict[str, Any]] | None = None
    # None = nessun dump a terminale (solo functionResponse verso il modello).
    print_tool_result: ToolResultPrinter | None = None
    # Gmail: dopo draft_email parla la richiesta di conferma e salta Gemini.
    hitl_after_tool: HitlAfterTool | None = None
    # Gmail: sì/no sull'utterance successiva, senza LLM.
    hitl_on_utterance: HitlOnUtterance | None = None


class SupportsChat(Protocol):
    """Contratto del client LLM del loop: Gemini nativo (testabile con mock)."""

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> LlmTurn: ...

    def close(self) -> None: ...


def _dispatch_tool(tool: str, args: dict[str, Any]) -> str:
    """Esegue un tool FS whitelist; ritorna stringa di esito per Gemini.

    Side-effect: I/O FS solo via tools_fs (confinato a WORKSPACE_ROOT).
    """
    # Whitelist stretta: Gemini a volte inventa nomi; rifiutiamo subito.
    if tool not in _ALLOWED_TOOLS:
        allowed = ", ".join(sorted(_ALLOWED_TOOLS))
        return f"ERRORE: tool sconosciuto {tool!r}. Consentiti: {allowed}."

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

        # create + append: stesso campo `content` (chiavi omogenee nel catalogo).
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
    Il TTS resta sul testo del modello; qui mostriamo il testo al terminale.
    """
    if not result.startswith("OK:"):
        return False
    if "\n" in result:
        header, body = result.split("\n", 1)
        print(f"[FS] {header}")
        print(body, end="" if body.endswith("\n") else "\n")
    else:
        print(f"[FS] {result}")
    return True


def _print_master_tool_result(tool: str, result: str) -> None:
    """Stdout dello specialista FS: [FS] per read_file, [RAG] per find_file."""
    if tool == _TOOL_READ:
        _print_read_file_to_terminal(result)
    elif tool == _TOOL_FIND:
        _print_find_file_to_terminal(result)


def _tool_loop_key(tool: str, args: dict[str, Any]) -> str:
    """Chiave anti-ripetizione nel turno: query se presente, altrimenti name.

    find_file e list_emails identificano la ricerca con `query`; read_file e
    read_email usano `name`. Preferire query evita collisioni se entrambi
    i campi arrivano nello stesso args.
    """
    query = args.get("query")
    if isinstance(query, str) and query.strip():
        identity = query.strip().casefold()
    else:
        identity = str(args.get("name") or "").strip().casefold()
    return f"{tool}|{identity}"


# Spec di default: specialista FS (CLI master). Test e avvio senza --agent.
MASTER_AGENT_SPEC = AgentSpec(
    name="fs",
    role="Assistente file sul Desktop",
    goal="Creare, aggiornare, leggere e cercare file sotto WORKSPACE_ROOT.",
    tools=tuple(FS_TOOL_MAP),
    dispatch=_dispatch_tool,
    intro_text=_MASTER_INTRO_TEXT,
    backstory=(
        "Specialista filesystem/RAG. Non legge Gmail: lo specialista email "
        "è `--agent gmail`."
    ),
)

MASTER_LOOP_SPEC = LoopSpec(
    system_prompt=_SYSTEM_PROMPT,
    dispatch=_dispatch_tool,
    intro_text=_MASTER_INTRO_TEXT,
    gemini_tools=FS_GEMINI_TOOLS,
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
    max_turns: int | None = None,
    report_latency: bool = True,
    telemetry_db: Path | None = None,
    spec: LoopSpec | None = None,
) -> int:
    """Un turno = listen → (functionCall)* → reply TTS; ritorna 0 in uscita.

    Side-effect: storia append-only (testo + functionCall/Response); tool dello
    spec; TTS su ogni reply finale; una riga telemetria per turno vocale valido
    (non intro / riga vuota / esci).
    """
    loop_spec = spec if spec is not None else MASTER_LOOP_SPEC

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": loop_spec.system_prompt},
    ]

    tts.speak(loop_spec.intro_text)

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
        store.close()


def _run_chat_loop_body(
    stt: BaseSTT,
    tts: BaseTTS,
    llm: SupportsChat,
    messages: list[dict[str, Any]],
    store: TelemetryDB,
    spec: LoopSpec,
    *,
    max_turns: int | None,
    report_latency: bool,
) -> int:
    """Corpo del loop: listen → (HITL | chat/tool) → speak + insert telemetria."""
    turns = 0
    while max_turns is None or turns < max_turns:
        user_text = stt.listen()

        if not user_text.strip():
            tts.speak("Nessun input. Uscita.")
            return 0

        if user_text.strip().casefold() in _EXIT_WORDS:
            tts.speak("Arrivederci.")
            return 0

        started_at = utc_now_iso()
        usage = TokenUsage()
        stripped = user_text.strip()

        # HITL Gmail: sì/no sulla bozza, senza Gemini (stesso posto di `esci`).
        if spec.hitl_on_utterance is not None:
            hitl_spoken = spec.hitl_on_utterance(stripped)
            if hitl_spoken is not None:
                tts.speak(hitl_spoken)
                _record_stt_turn(
                    store,
                    started_at=started_at,
                    usage=usage,
                    tts_response=hitl_spoken,
                )
                turns += 1
                continue

        messages.append({"role": "user", "content": stripped})

        spoken = False
        prev_tool_key: str | None = None
        for _round in range(_MAX_TOOL_ROUNDS):
            t0 = time.perf_counter()
            try:
                turn = llm.chat(
                    messages,
                    tools=spec.gemini_tools,
                    options={"temperature": 0.1},
                )
            except LLMError as exc:
                messages.pop()
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

            usage = usage + getattr(llm, "last_usage", TokenUsage())

            if report_latency:
                print(f"[lab] latenza chat: {elapsed:.2f}s", file=sys.stderr)

            if turn.is_empty():
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

            # Testo senza tool: reply parlata (ex tool=none).
            if not turn.function_calls:
                # Storia: testo crudo del modello. TTS: markup rimosso (edge-tts).
                reply_raw = turn.text.strip()
                reply_s = prepare_spoken_text(reply_raw)
                if not reply_s:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "Manca la frase da dire all'utente. "
                                "Rispondi in italiano, da leggere a voce, "
                                "senza markdown e senza tool."
                            ),
                        }
                    )
                    continue
                messages.append({"role": "assistant", "content": reply_raw})
                tts.speak(reply_s)
                _record_stt_turn(
                    store,
                    started_at=started_at,
                    usage=usage,
                    tts_response=reply_s,
                )
                spoken = True
                break

            # Un solo tool eseguito: la prima functionCall, le altre si ignorano.
            call = turn.function_calls[0]
            args_dict = call.args if isinstance(call.args, dict) else {}
            tool = call.name
            tool_key = _tool_loop_key(tool, args_dict)
            if prev_tool_key is not None and tool_key == prev_tool_key:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Hai già ricevuto l'esito di questo tool. "
                            "Rispondi ora in italiano all'utente, da leggere "
                            "a voce, senza markdown e senza altri tool."
                        ),
                    }
                )
                continue

            messages.append(_assistant_function_call_message(call))
            result = spec.dispatch(tool, args_dict)
            if spec.print_tool_result is not None:
                spec.print_tool_result(tool, result)
            messages.append(_user_function_response_message(call, result))
            prev_tool_key = tool_key

            # HITL: dopo la bozza parliamo noi e aspettiamo il prossimo ascolto.
            if spec.hitl_after_tool is not None:
                hitl_prompt = spec.hitl_after_tool(tool, result)
                if hitl_prompt:
                    tts.speak(hitl_prompt)
                    _record_stt_turn(
                        store,
                        started_at=started_at,
                        usage=usage,
                        tts_response=hitl_prompt,
                    )
                    spoken = True
                    break

        if not spoken:
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


def _assistant_function_call_message(call: FunctionCall) -> dict[str, Any]:
    """Turno model da rimandare a Gemini: una functionCall (id e firma se c'erano).

    `thought_signature` resta nel dict della history: `_message_to_gemini_parts`
    la mette sulla part (`thoughtSignature`), non dentro `functionCall.args`.
    Senza questo Gemini 3 rifiuta il generateContent dopo il tool (HTTP 400).
    """
    item: dict[str, Any] = {"name": call.name, "args": call.args}
    if call.call_id:
        item["id"] = call.call_id
    # Blob opaco: lo passiamo tale e quale; i mock hanno None e omettono la chiave.
    if call.thought_signature:
        item["thought_signature"] = call.thought_signature
    return {"role": "assistant", "function_calls": [item]}


def _user_function_response_message(call: FunctionCall, result: str) -> dict[str, Any]:
    """Turno user con functionResponse: il modello legge `result` (OK:/ERRORE:)."""
    payload: dict[str, Any] = {"name": call.name, "result": result}
    if call.call_id:
        payload["id"] = call.call_id
    return {"role": "user", "function_response": payload}


def build_llm(
    provider: Literal["ollama", "gemini"] = "gemini",
    model: str | None = None,
) -> LocalOllama | GeminiChat:
    """Factory: gemini (loop vocale) oppure ollama (prove isolate, non il loop).

    Il CLI `--llm ollama` non arriva qui: fail-fast in `main` prima del ping.
    """
    if provider == "gemini":
        return GeminiChat(model=model or GEMINI_MODEL, timeout=120.0)

    return LocalOllama(
        base_url=OLLAMA_URL,
        model=model or OLLAMA_MODEL,
        timeout=500.0,
    )


def build_default_llm() -> LocalOllama:
    """Prove isolate Ollama: non è il runtime del loop vocale."""
    llm = build_llm("ollama")
    assert isinstance(llm, LocalOllama)
    return llm
