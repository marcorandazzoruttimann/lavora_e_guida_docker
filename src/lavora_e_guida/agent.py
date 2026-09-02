"""Motore del loop vocale: STT → Gemini (functionCall / testo) → dispatch → TTS.

Questo modulo non è uno specialista: niente catalogo FS/Gmail/web. Gli
specialisti passano un `LoopSpec`; il default (`spec=None`) è il router
master, importato lazy per non ciclare con `master.agent`.

Function calling nativo: niente JSON `{"tool","args"}`. Il giro tool è
`run_specialist_task` (senza TTS): il loop esterno parla solo l'esito.
"""

from __future__ import annotations

import importlib
import sys
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
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
from lavora_e_guida.llm.spoken import prepare_spoken_text
from lavora_e_guida.llm.turn import FunctionCall, LlmTurn
from lavora_e_guida.llm.usage import TokenUsage
from lavora_e_guida.telemetry import TelemetryDB, utc_now_iso

# Dispatch: Python esegue il tool; ritorna stringa di esito per functionResponse.
ToolDispatch = Callable[[str, dict[str, Any]], str]
# Stampa esito su stdout ([FS], [RAG], [GMAIL], [WEB]); lo spec decide il prefisso.
ToolResultPrinter = Callable[[str, str], None]
# Dopo un tool: se ritorna testo, il loop lo parla e attende HITL (niente LLM).
HitlAfterTool = Callable[[str, str], str | None]
# All'ascolto: se ritorna testo, è gestito (sì/no) e non passa da Gemini.
HitlOnUtterance = Callable[[str], str | None]

# Comandi di uscita case-insensitive: allineati all'entrypoint vocale.
_EXIT_WORDS = frozenset({"esci", "exit", "quit"})

# Limite round tool per turno utente: evita loop se il modello ripete la call.
_MAX_TOOL_ROUNDS = 4

# Esito di un giro specialista: il loop (o il master) parla `text`.
SpecialistKind = Literal["text", "hitl", "error", "exhausted"]


@dataclass(frozen=True)
class AgentSpec:
    """Dato statico di uno specialista (nome, ruolo, tool ammessi, dispatch).

    Non è il motore del loop: `LoopSpec` avvolge prompt + tools Gemini + HITL.
    Ogni pacchetto specialista costruisce la propria istanza; questo modulo
    non ne possiede nessuna.
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

    Il motore non ha uno spec di default in questo file: `run_chat_loop`
    risolve `spec=None` con un import lazy del router master. Gmail/web/fs
    passano un'istanza propria; il motore non importa i loro tool.
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
    # Gmail: dopo draft_email / reply_email / reply_all_email parla la conferma e salta Gemini.
    hitl_after_tool: HitlAfterTool | None = None
    # Gmail: sì/no sull'utterance successiva, senza LLM.
    hitl_on_utterance: HitlOnUtterance | None = None


@dataclass(frozen=True)
class SpecialistTurnResult:
    """Esito di un giro tool **senza TTS**: il caller parla `text`.

    `kind`:
      text — reply parlata del modello (`parts[].text` già pulita per edge-tts)
      hitl — lo spec ha aperto una conferma (bozza Gmail): parlare `hitl_spoken`
      error — LLMError o modello vuoto
      exhausted — 4 round senza reply finale

    `text` va nella `functionResponse` del master oppure al TTS del loop.
    `usage` è la somma dei round (TokenUsage è frozen: non si muta in place).
    """

    kind: SpecialistKind
    text: str
    hitl_spoken: str | None = None
    usage: TokenUsage = field(default_factory=TokenUsage)


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


# Client del giro tool in corso: il dispatch `ask_*` del master lo legge senza
# cambiare la firma `ToolDispatch` (tool, args) → str. Nested `run_specialist_task`
# riusa lo stesso oggetto (stesso Gemini del loop vocale). Default None: un
# dispatch chiamato fuori dal giro (test unitario senza bind) vede l'assenza.
_active_llm: ContextVar[SupportsChat | None] = ContextVar(
    "lavora_e_guida_active_llm",
    default=None,
)


def get_active_llm() -> SupportsChat | None:
    """Client LLM del `run_specialist_task` in corso, o None se siamo fuori dal giro.

    Il master lo usa per delegare allo specialista con lo stesso Gemini del
    loop. Non è un singleton di processo: è uno stack di contextvar, così un
    nested `run_specialist_task` (ask_web dentro ask del master) ripristina
    il client esterno all'uscita. Side-effect: nessuno.
    """
    return _active_llm.get()


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


def _default_loop_spec() -> LoopSpec:
    """Default vocale: router master, import lazy per non ciclare.

    Il pacchetto `master` importa `run_specialist_task` da questo modulo:
    un import eager di `MASTER_LOOP_SPEC` in testa chiuderebbe il ciclo
    agent ↔ master. Caduta su FS solo se il pacchetto master manca.
    """
    try:
        # importlib: `master.agent` importa `run_specialist_task` da qui. Un
        # `from` statico in testa a questo modulo chiuderebbe il ciclo
        # agent ↔ master già all'import (e l'avvio del loop).
        master_agent = importlib.import_module("lavora_e_guida.master.agent")
    except ImportError:
        # Difesa: senza pacchetto master lo specialista file resta avviabile.
        from lavora_e_guida.fs.agent import FS_LOOP_SPEC

        return FS_LOOP_SPEC
    return master_agent.MASTER_LOOP_SPEC


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


def run_specialist_task(
    spec: LoopSpec,
    llm: SupportsChat,
    messages: list[dict[str, Any]],
    usage: TokenUsage,
    *,
    report_latency: bool = True,
) -> SpecialistTurnResult:
    """Giro tool di uno specialista: fino a 4 round, **senza TTS**.

    Stesso contratto del vecchio for-round in `run_chat_loop`: una sola
    `functionCall` eseguita per round, anti-ripetizione, `print_tool_result`,
    `hitl_after_tool`. La storia `messages` è mutata in place (append-only
    salvo pop su errore/vuoto). Il caller (loop vocale o dispatch `ask_*`
    del master) parla `result.text` / `result.hitl_spoken`.

    Side-effect: dispatch dello spec (I/O FS, REST, …); print su stdout se
    lo spec ha `print_tool_result`. Nessun `tts.speak`. Bind di `get_active_llm`
    per la durata del giro (il master nested riusa questo stesso `llm`).
    """
    # Bind visibile al dispatch `ask_*`: stesso client, senza allargare ToolDispatch.
    llm_token = _active_llm.set(llm)
    try:
        return _run_specialist_task_body(
            spec,
            llm,
            messages,
            usage,
            report_latency=report_latency,
        )
    finally:
        # Nested (master → ask_gmail → questo stesso giro) ripristina il bind esterno.
        _active_llm.reset(llm_token)


def _run_specialist_task_body(
    spec: LoopSpec,
    llm: SupportsChat,
    messages: list[dict[str, Any]],
    usage: TokenUsage,
    *,
    report_latency: bool,
) -> SpecialistTurnResult:
    """Corpo del giro tool: il bind del client LLM sta nel wrapper pubblico."""
    # Accumulator locale: TokenUsage è frozen, ogni round produce una somma nuova.
    accumulated = usage
    prev_tool_key: str | None = None
    for _round in range(_MAX_TOOL_ROUNDS):
        t0 = time.perf_counter()
        try:
            # Stesso client Gemini del loop: il master nested riusa la sessione.
            turn = llm.chat(
                messages,
                tools=spec.gemini_tools,
                options={"temperature": 0.1},
            )
        except LLMError as exc:
            # Chat fallita: togliamo l'ultimo messaggio (di solito l'utterance)
            # così un retry non riparte da una storia a metà. Token di questo
            # round non ci sono: non sommiamo `last_usage`.
            messages.pop()
            err_label = "Ollama" if isinstance(llm, LocalOllama) else "LLM"
            return SpecialistTurnResult(
                kind="error",
                text=f"Errore {err_label}: {exc}",
                usage=accumulated,
            )
        elapsed = time.perf_counter() - t0

        # Token del round: anche un turno vuoto può aver consumato prompt.
        accumulated = accumulated + getattr(llm, "last_usage", TokenUsage())

        if report_latency:
            print(f"[lab] latenza chat: {elapsed:.2f}s", file=sys.stderr)

        if turn.is_empty():
            # Niente testo né tool: fallback parlante, storia ripulita come prima.
            messages.pop()
            return SpecialistTurnResult(
                kind="error",
                text="Il modello non ha risposto. Riprova.",
                usage=accumulated,
            )

        # Testo senza tool: reply parlata (ex tool=none). Niente TTS qui.
        if not turn.function_calls:
            # Storia: testo crudo del modello. Esito: markup rimosso (edge-tts).
            reply_raw = turn.text.strip()
            reply_s = prepare_spoken_text(reply_raw)
            if not reply_s:
                # Nudge: il modello ha mandato markup-only o spazi. Riprova.
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
            return SpecialistTurnResult(
                kind="text",
                text=reply_s,
                usage=accumulated,
            )

        # Un solo tool eseguito: la prima functionCall, le altre si ignorano.
        call = turn.function_calls[0]
        args_dict = call.args if isinstance(call.args, dict) else {}
        tool = call.name
        tool_key = _tool_loop_key(tool, args_dict)
        if prev_tool_key is not None and tool_key == prev_tool_key:
            # Anti-ripetizione: stessa call di fila → chiediamo la frase, non un altro tool.
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

        # HITL: dopo la bozza il caller parla e aspetta il prossimo ascolto.
        # Lo specialista nested (master → ask_gmail) non deve chiamare TTS:
        # ritorniamo `kind=hitl` e il loop esterno (o lo spec master) parla.
        if spec.hitl_after_tool is not None:
            hitl_prompt = spec.hitl_after_tool(tool, result)
            if hitl_prompt:
                return SpecialistTurnResult(
                    kind="hitl",
                    text=hitl_prompt,
                    hitl_spoken=hitl_prompt,
                    usage=accumulated,
                )

    # Quattro round senza testo finale né HITL: fallback parlante, niente altro tool.
    return SpecialistTurnResult(
        kind="exhausted",
        text="Non sono riuscito a completare l'azione in questo turno. Riprova.",
        usage=accumulated,
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
    spec; TTS solo sull'esito di `run_specialist_task` (o HITL in ascolto);
    una riga telemetria per turno vocale valido (non intro / riga vuota / esci).
    """
    # spec=None → router master (lazy). I test FS passano `FS_LOOP_SPEC`.
    loop_spec = spec if spec is not None else _default_loop_spec()

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
    """Corpo del loop: listen → (HITL utterance | specialist task) → speak + insert."""
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
        # Resta sul loop esterno: lo specialista nested non ascolta.
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

        # Un solo giro specialista: TTS solo sull'esito (testo, HITL, errore, esausto).
        result = run_specialist_task(
            spec,
            llm,
            messages,
            usage,
            report_latency=report_latency,
        )
        # HITL: parla la conferma della bozza; gli altri kind parlano `text`.
        if result.kind == "hitl" and result.hitl_spoken:
            spoken_text = result.hitl_spoken
        else:
            spoken_text = result.text
        tts.speak(spoken_text)
        _record_stt_turn(
            store,
            started_at=started_at,
            usage=result.usage,
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
