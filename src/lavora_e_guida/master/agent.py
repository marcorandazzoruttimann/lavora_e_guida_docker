"""Spec vocale del router master: tre tool di delega, niente catalogo di dominio.

Questo modulo è l'agente che parla con l'utente. Importa da `lavora_e_guida.agent`
il contratto del loop (`LoopSpec` / `AgentSpec`) e `run_specialist_task` per il
sotto-giro Gemini dello specialista. Non importa i tool FS/Gmail/web nel
catalogo: Gemini del master vede solo `ask_fs`, `ask_gmail`, `ask_web`.

Dispatch: appende `query` alla storia isolata di quello specialista, riusa il
client Gemini del loop (`get_active_llm`), ritorna l'esito parlante. Lo
specialista nested non chiama TTS. HITL Gmail resta sul loop esterno: dopo
`ask_gmail` con bozza, `hitl_after_tool` del master parla la conferma;
`hitl_on_utterance` inoltra a `gmail_hitl_on_utterance`.

Gate chiavi lazy (non all'avvio): Tavily assente → `ERRORE:` da `ask_web`;
token Gmail assente → stesso da `ask_gmail`. Il master parte comunque: serve
il Desktop/RAG per `ask_fs`.
"""

from __future__ import annotations

from typing import Any

from lavora_e_guida.agent import (
    AgentSpec,
    LoopSpec,
    SupportsChat,
    get_active_llm,
    run_specialist_task,
)
from lavora_e_guida.config import get_settings
from lavora_e_guida.fs.agent import FS_LOOP_SPEC
from lavora_e_guida.gmail.agent import GMAIL_LOOP_SPEC, gmail_hitl_on_utterance
from lavora_e_guida.gmail.oauth import MSG_GMAIL_NOT_LINKED, resolve_token_path
from lavora_e_guida.gmail.send import get_draft_session, spoken_draft_confirm
from lavora_e_guida.llm.spoken import SPOKEN_REPLY_RULE
from lavora_e_guida.llm.usage import TokenUsage
from lavora_e_guida.tools.catalog import (
    ToolDeclaration,
    object_schema,
    string_param,
    to_gemini_tools,
)
from lavora_e_guida.web.agent import WEB_LOOP_SPEC
from lavora_e_guida.web.search import MSG_MISSING_KEY

# Nomi tool in un'unica costante: declaration, mappa e dispatch non divergono.
_TOOL_ASK_FS = "ask_fs"
_TOOL_ASK_GMAIL = "ask_gmail"
_TOOL_ASK_WEB = "ask_web"

# Catalogo del router: solo delega. Chiave omogenea `query` come find_file / list_emails.
MASTER_TOOL_DECLARATIONS: tuple[ToolDeclaration, ...] = (
    ToolDeclaration(
        name=_TOOL_ASK_FS,
        description=(
            "Chiede allo specialista file sul Desktop: creare, aggiornare, "
            "leggere e cercare note o PDF. Usalo per spesa, appunti, find file. "
            "Non usarlo per salvare un esito web o email se l'utente non ha "
            "chiesto un file."
        ),
        parameters=object_schema(
            {
                "query": string_param(
                    "Richiesta in italiano, come l'ha detta l'utente: "
                    "aggiungi latte alla spesa, leggi la nota spesa, "
                    "dove ho scritto cetrioli."
                ),
            },
            required=("query",),
        ),
    ),
    ToolDeclaration(
        name=_TOOL_ASK_GMAIL,
        description=(
            "Chiede allo specialista mailbox: elencare, leggere, allegati, "
            "bozza e invio. Per mandare un esito (meteo, ricerca) passa "
            "destinatario e testo nell'argomento query. Non inventare un file."
        ),
        parameters=object_schema(
            {
                "query": string_param(
                    "Richiesta in italiano: ultime email, leggi la seconda, "
                    "scrivi a Rossi oggetto Meteo testo Domani sereno."
                ),
            },
            required=("query",),
        ),
    ),
    ToolDeclaration(
        name=_TOOL_ASK_WEB,
        description=(
            "Chiede allo specialista di ricerca web (Tavily): fatti recenti "
            "o pagine online. Poi, se l'utente ha chiesto di mandare l'esito "
            "per email, chiama ask_gmail con destinatario e testo trovato."
        ),
        parameters=object_schema(
            {
                "query": string_param(
                    "Cosa cercare, in lingua naturale: meteo Roma domani, "
                    "risultati campionato, prezzo del gas oggi."
                ),
            },
            required=("query",),
        ),
    ),
)

# Mappa nome → presenza: alimenta whitelist e AgentSpec.tools (niente callable di dominio).
MASTER_TOOL_MAP: dict[str, Any] = {
    _TOOL_ASK_FS: _TOOL_ASK_FS,
    _TOOL_ASK_GMAIL: _TOOL_ASK_GMAIL,
    _TOOL_ASK_WEB: _TOOL_ASK_WEB,
}

_ALLOWED_TOOLS = frozenset(MASTER_TOOL_MAP)
MASTER_GEMINI_TOOLS = to_gemini_tools(MASTER_TOOL_DECLARATIONS)

# Intro parlata: identità del router, via d'uscita, niente markdown.
_MASTER_INTRO_TEXT = (
    "Assistente vocale: posso cercare file sul Desktop, leggere la posta "
    "e cercare sul web. Di' esci per terminare."
)

# Prompt: identità e regole di smistamento. Gli schemi stanno nelle declaration.
# Un tool per round; più round nello stesso enunciato se l'utente ha chiesto due azioni.
_SYSTEM_PROMPT = (
    "Sei l'assistente vocale che smista le richieste. Non esegui tu i tool "
    "di file, posta o ricerca: hai solo ask_fs, ask_gmail e ask_web. "
    "Un solo tool per round. Se l'utente ha chiesto due azioni nello stesso "
    "enunciato, fai un tool per round, in sequenza.\n\n"
    "ask_fs: file e ricerca sul Desktop. ask_gmail: mailbox, bozza, invio. "
    "ask_web: ricerca online. In query passa la richiesta in italiano, con "
    "tutti i dettagli utili (destinatario, testo da mandare, nome del file).\n"
    "Ricerca più email: prima ask_web, poi ask_gmail con destinatario e il "
    "testo dell'esito. Non salvare su file se l'utente non ha chiesto un file. "
    "Non chiamare ask_fs per parcheggiare un meteo o una ricerca.\n"
    "Dopo un tool, la reply all'utente usa i fatti dell'esito, non dire che "
    "hai smistato o che hai chiesto a un altro agente. Se l'esito è ERRORE, "
    "di' cosa è andato storto e fermati.\n"
    "Se la richiesta è chiacchiere o un saluto, rispondi a voce senza tool.\n\n"
    "Esempi: «Aggiungi latte alla spesa» → ask_fs query aggiungi latte alla "
    "spesa. «Ultime email» → ask_gmail query ultime email. «Che tempo fa a "
    "Roma» → ask_web query meteo Roma. «Cerca il meteo di Roma e mandalo a "
    "Mario» → prima ask_web query meteo Roma, poi ask_gmail con destinatario "
    "Mario e il testo trovato, senza creare un file.\n\n"
    f"{SPOKEN_REPLY_RULE}"
)

# Storie isolate per specialista: chiave = nome agente (fs / gmail / web).
# Persistono per la sessione di processo: «leggi la seconda» dopo «ultime email»
# deve vedere la lista. I test le svuotano con `reset_specialist_histories`.
_SPECIALIST_HISTORIES: dict[str, list[dict[str, Any]]] = {}

# Nome specialista → LoopSpec: un solo punto per dispatch e storia.
_SPECIALIST_SPECS: dict[str, LoopSpec] = {
    "fs": FS_LOOP_SPEC,
    "gmail": GMAIL_LOOP_SPEC,
    "web": WEB_LOOP_SPEC,
}

# Tool master → nome specialista: il dispatch non fa if a cascata sul dominio.
_TOOL_TO_SPECIALIST: dict[str, str] = {
    _TOOL_ASK_FS: "fs",
    _TOOL_ASK_GMAIL: "gmail",
    _TOOL_ASK_WEB: "web",
}


def reset_specialist_histories() -> None:
    """Svuota le storie isolate: isolamento tra test e nuova sessione vocale.

    Side-effect: cancella `_SPECIALIST_HISTORIES`. Non tocca la bozza Gmail
    (quella ha `reset_draft_session` nello specialista send).
    """
    _SPECIALIST_HISTORIES.clear()


def get_specialist_history(name: str) -> list[dict[str, Any]]:
    """Copia superficiale della storia di uno specialista, o lista vuota.

    Serve ai test (storia isolata: il system prompt FS non entra in Gmail).
    Side-effect: nessuno; i dict dei messaggi restano quelli vivi, la lista no.
    """
    history = _SPECIALIST_HISTORIES.get(name)
    if history is None:
        return []
    return list(history)


def tavily_key_is_present() -> bool:
    """True se `TAVILY_API_KEY` è valorizzata. Niente rete, niente client Tavily.

    Gate lazy di `ask_web`: il master parte senza la chiave (serve il Desktop
    per `ask_fs`). I test monkeypatchano questa funzione o `get_settings`.
    """
    # `or ""` + strip: nel `.env` la riga può esserci ma vuota (`TAVILY_API_KEY=`).
    return bool((get_settings().tavily_api_key or "").strip())


def gmail_token_is_present() -> bool:
    """True se esiste il JSON refresh su disco. Niente browser, niente refresh.

    Gate lazy di `ask_gmail`: assenza file → `MSG_GMAIL_NOT_LINKED`. Un token
    presente ma scaduto lo gestisce lo specialista (refresh in OAuth).
    """
    return resolve_token_path(get_settings()).is_file()


def _as_query(raw: object) -> str:
    """Normalizza args.query: assente, None o non-stringa → stringa (anche vuota)."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return str(raw)


def _history_for(name: str, system_prompt: str) -> list[dict[str, Any]]:
    """Storia isolata di uno specialista: system + turni precedenti di *quel* agente.

    Prima chiamata: lista nuova con solo il system prompt dello spec. Le
    chiamate dopo riusano la stessa lista (append-only, mutata da
    `run_specialist_task`). Side-effect: inserisce la lista nel dict di modulo.
    """
    history = _SPECIALIST_HISTORIES.get(name)
    if history is None:
        # Testa fissa: lo specialista non deve vedere il prompt del master.
        history = [{"role": "system", "content": system_prompt}]
        _SPECIALIST_HISTORIES[name] = history
    return history


def _delegate_to_specialist(
    name: str,
    query: str,
    llm: SupportsChat,
) -> str:
    """Appende `query` alla storia isolata e gira `run_specialist_task` senza TTS.

    Ritorna l'esito parlante (`text` o `hitl_spoken`) per la `functionResponse`
    del master. Stdout dello specialista (`[FS]` / `[RAG]` / `[GMAIL]` / `[WEB]`)
    resta: lo spec nested ha ancora `print_tool_result`. Nested `report_latency`
    è False: la latenza del round master è già sul loop esterno.

    Side-effect: muta la storia dello specialista; I/O di dominio via dispatch nested.
    """
    loop_spec = _SPECIALIST_SPECS[name]
    history = _history_for(name, loop_spec.system_prompt)
    # L'enunciato verso lo specialista è la query del master, non l'STT grezzo.
    history.append({"role": "user", "content": query})
    result = run_specialist_task(
        loop_spec,
        llm,
        history,
        TokenUsage(),
        report_latency=False,
    )
    # HITL: la conferma Python (bozza) ha priorità sul testo; il master la parla.
    if result.kind == "hitl" and result.hitl_spoken:
        return result.hitl_spoken
    return result.text


def dispatch_master_tool(
    tool: str,
    args: dict[str, Any],
    llm: SupportsChat | None = None,
) -> str:
    """Esegue i tre `ask_*` in whitelist; ritorna l'esito parlante `OK:` / `ERRORE:`.

    `llm` esplicito (test) vince su `get_active_llm` (giro tool del loop).
    Gate lazy prima del nested Gemini: niente Tavily / niente token Gmail →
    frase `ERRORE:` senza chiamare lo specialista. Contratto: mai un'eccezione
    verso il loop, solo testo già parlabile.

    Side-effect: print `[MASTER] ask_web` (o fs/gmail) su stdout; nested I/O.
    """
    if tool not in _ALLOWED_TOOLS:
        # Nome inventato (create_text_file, web_search, …): elenchiamo solo i tre ask.
        allowed = ", ".join(sorted(_ALLOWED_TOOLS))
        return f"ERRORE: tool sconosciuto {tool!r}. Consentiti: {allowed}."

    # Log prima del nested: a schermo si vede quale specialista parte, poi [WEB]/[GMAIL].
    print(f"[MASTER] {tool}")

    args_dict = args if isinstance(args, dict) else {}
    query = _as_query(args_dict.get("query", "")).strip()
    if not query:
        return "ERRORE: manca la richiesta da inoltrare allo specialista."

    # Gate Tavily: il master è già avviato; l'errore è parlante, non SystemExit.
    if tool == _TOOL_ASK_WEB and not tavily_key_is_present():
        return MSG_MISSING_KEY

    # Gate token: solo assenza file, niente refresh di rete in questo dispatch.
    if tool == _TOOL_ASK_GMAIL and not gmail_token_is_present():
        return MSG_GMAIL_NOT_LINKED

    client = llm if llm is not None else get_active_llm()
    if client is None:
        # Dispatch fuori dal giro (test senza mock) o bind perso: non inventiamo l'esito.
        return "ERRORE: client LLM assente, riprova."

    specialist = _TOOL_TO_SPECIALIST[tool]
    return _delegate_to_specialist(specialist, query, client)


def master_hitl_after_tool(tool: str, result: str) -> str | None:
    """Dopo `ask_gmail`, se lo specialista ha aperto una bozza: parla e salta Gemini.

    Lo specialista nested ha già fermato il *suo* Gemini (`kind=hitl`). Qui
    fermiamo quello del master, altrimenti riformulerebbe «ho smistato».
    None = nessuna bozza, il loop lascia parlare Gemini sui fatti dell'esito.
    """
    if tool != _TOOL_ASK_GMAIL:
        return None
    session = get_draft_session()
    if not session.awaiting_confirm:
        return None
    spoken = (result or "").strip()
    if spoken:
        return spoken
    # Nested senza frase (non dovrebbe accadere): riusiamo la conferma Python.
    draft = session.draft
    if draft is None:
        return "Di' sì per inviare o no per annullare."
    return spoken_draft_confirm(
        to=draft.to,
        subject=draft.subject,
        body=draft.body,
        cc=draft.cc,
    )


MASTER_AGENT_SPEC = AgentSpec(
    name="master",
    role="Router vocale",
    goal="Smistare a FS, Gmail o web e parlare l'esito con i fatti, non lo smistamento.",
    tools=tuple(MASTER_TOOL_MAP),
    dispatch=dispatch_master_tool,
    intro_text=_MASTER_INTRO_TEXT,
    backstory=(
        "Agente master (`--agent master`): unico possessore di STT/TTS, tre "
        "tool di delega. Non tocca i file, la mailbox né Tavily in proprio."
    ),
)

MASTER_LOOP_SPEC = LoopSpec(
    system_prompt=_SYSTEM_PROMPT,
    dispatch=dispatch_master_tool,
    intro_text=_MASTER_INTRO_TEXT,
    gemini_tools=MASTER_GEMINI_TOOLS,
    # Il log `[MASTER] ask_*` sta nel dispatch (prima del nested); niente doppio print.
    print_tool_result=None,
    hitl_after_tool=master_hitl_after_tool,
    # Sì/no sulla bozza: stesso interceptor Gmail, già None se non c'è bozza.
    hitl_on_utterance=gmail_hitl_on_utterance,
)
