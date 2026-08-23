"""Spec vocale Gmail: lettura, allegati, bozza e invio con HITL.

Questo modulo è lo specialista email. Importa da `lavora_e_guida.agent` solo
`LoopSpec` / `AgentSpec` (contratto del loop), mai i tool FS/RAG del master.
Il master non importa questo file: niente list/read/save/draft/send sul FS.

I tool sono `functionDeclarations` Gemini. L'invio passa da `draft_email` e
da un sì vocale in Python: Gemini non può saltare la conferma.
"""

from __future__ import annotations

from typing import Any

from lavora_e_guida.agent import AgentSpec, LoopSpec
from lavora_e_guida.gmail.read import (
    MSG_EMPTY_NAME,
    list_emails,
    read_email,
    save_attachments,
)
from lavora_e_guida.gmail.send import (
    draft_email,
    get_draft_session,
    send_email,
)
from lavora_e_guida.llm.spoken import SPOKEN_REPLY_RULE
from lavora_e_guida.tools.catalog import (
    ToolDeclaration,
    boolean_param,
    integer_param,
    object_schema,
    string_param,
    to_gemini_tools,
)

_TOOL_LIST = "list_emails"
_TOOL_READ = "read_email"
_TOOL_SAVE = "save_attachments"
_TOOL_DRAFT = "draft_email"
_TOOL_SEND = "send_email"

# Catalogo Gmail isolato: il master FS non importa questo modulo.
GMAIL_TOOL_DECLARATIONS: tuple[ToolDeclaration, ...] = (
    ToolDeclaration(
        name=_TOOL_LIST,
        description=(
            "Elenca o conta email. Query libera: inbox, operatori Gmail, "
            "oppure le parole dell'utente. count true = solo il totale, "
            "senza limit. Giorni: newer_than:3d. Ore: lascia l'italiano in "
            "query, non inventare timestamp Unix."
        ),
        parameters=object_schema(
            {
                "query": string_param(
                    "Filtro: inbox, is:unread, from:mario, fattura, …"
                ),
                "limit": integer_param("Quante email elencare (default 5, massimo 20)."),
                "count": boolean_param("true per il totale senza elencare."),
            },
            required=("query",),
        ),
    ),
    ToolDeclaration(
        name=_TOOL_READ,
        description=(
            "Legge un'email già elencata. name è indice parlato (1, la seconda) "
            "oppure mittente o oggetto. Non inventare id Gmail."
        ),
        parameters=object_schema(
            {
                "name": string_param("Indice, mittente o oggetto dell'email già in lista."),
            },
            required=("name",),
        ),
    ),
    ToolDeclaration(
        name=_TOOL_SAVE,
        description=(
            "Scarica gli allegati di un'email già elencata. Stessa chiave name "
            "di read_email. Solo se l'utente lo chiede, mai in automatico dopo "
            "la lettura."
        ),
        parameters=object_schema(
            {
                "name": string_param("Indice, mittente o oggetto dell'email già in lista."),
            },
            required=("name",),
        ),
    ),
    ToolDeclaration(
        name=_TOOL_DRAFT,
        description=(
            "Prepara una bozza di email (destinatario, oggetto, corpo). "
            "Non invia. Dopo la bozza l'utente deve dire sì o no a voce."
        ),
        parameters=object_schema(
            {
                "to": string_param("Indirizzo email del destinatario."),
                "subject": string_param("Oggetto del messaggio."),
                "body": string_param("Testo del messaggio in italiano."),
            },
            required=("to", "subject", "body"),
        ),
    ),
    ToolDeclaration(
        name=_TOOL_SEND,
        description=(
            "Invia la bozza già confermata. Non chiamare se l'utente non ha "
            "ancora detto sì. Senza bozza o senza conferma Python rifiuta."
        ),
        parameters=object_schema({}),
    ),
)

GMAIL_TOOL_MAP: dict[str, Any] = {
    _TOOL_LIST: list_emails,
    _TOOL_READ: read_email,
    _TOOL_SAVE: save_attachments,
    _TOOL_DRAFT: draft_email,
    _TOOL_SEND: send_email,
}

_ALLOWED_TOOLS = frozenset(GMAIL_TOOL_MAP)
GMAIL_GEMINI_TOOLS = to_gemini_tools(GMAIL_TOOL_DECLARATIONS)

_GMAIL_INTRO_TEXT = (
    "Agente Gmail: posso elencare, leggere le email, salvare gli allegati "
    "e inviare dopo una conferma. Di' esci per terminare."
)

# Prompt: identità e regole. Gli schemi stanno nelle declaration.
# Stesso vincolo TTS del master FS: elenchi email in frasi, non markdown.
_SYSTEM_PROMPT = (
    "Sei l'assistente vocale Gmail. Usa i tool per elencare, leggere, "
    "salvare allegati e preparare email. Un solo tool per enunciato. "
    "Dopo un tool, riassumi in italiano. Non inventare id Gmail.\n\n"
    "Per elencare o cercare usa list_emails con query libera "
    "(inbox, is:unread, from:mario, fattura). Per contare usa count true. "
    "Giorni: newer_than:3d. Ore: lascia l'italiano in query.\n"
    "Per leggere o salvare allegati usa name: indice parlato, mittente o oggetto. "
    "save_attachments solo se l'utente lo chiede, mai in automatico.\n"
    "Per scrivere: draft_email con to, subject e body. Non chiamare send_email "
    "finché l'utente non ha confermato a voce (sì/no lo gestisce Python).\n\n"
    "Esempi: «ultime email» → list_emails query inbox. "
    "«leggi la seconda» → read_email name la seconda. "
    "«scarica gli allegati della prima» → save_attachments name 1. "
    "«invia a mario@x.test oggetto Fattura testo Pagare venerdì» → draft_email.\n\n"
    f"{SPOKEN_REPLY_RULE}"
)

# HITL: match sull'enunciato intero (niente «invia a Mario» come sì).
_HITL_YES = frozenset(
    {"sì", "si", "ok", "okay", "confermo", "conferma", "invia", "manda", "yes"}
)
_HITL_NO = frozenset({"no", "annulla", "cancella"})


def _as_query(raw: object) -> str:
    """Normalizza args.query: assente o None = inbox; il resto diventa stringa."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return str(raw)


def _as_name(raw: object) -> str:
    """Normalizza args.name: indice integer → cifra; bool non è un nome."""
    if raw is None or isinstance(raw, bool):
        return ""
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, str):
        return raw
    return str(raw)


def dispatch_gmail_tool(tool: str, args: dict[str, Any]) -> str:
    """Esegue i tool Gmail whitelist; ritorna OK:/ERRORE:.

    Side-effect: REST solo via gmail.read / gmail.send. Gli id restano in Python.
    """
    if tool not in _ALLOWED_TOOLS:
        allowed = ", ".join(sorted(_ALLOWED_TOOLS))
        return f"ERRORE: tool sconosciuto {tool!r}. Consentiti: {allowed}."

    args_dict = args if isinstance(args, dict) else {}

    if tool == _TOOL_LIST:
        query = _as_query(args_dict.get("query", ""))
        return list_emails(
            query,
            limit=args_dict.get("limit"),
            count=args_dict.get("count"),
        )

    if tool == _TOOL_DRAFT:
        return draft_email(
            to=args_dict.get("to", ""),
            subject=args_dict.get("subject", ""),
            body=args_dict.get("body", ""),
        )

    if tool == _TOOL_SEND:
        # Nessun argomento: la bozza e il flag HITL stanno nella sessione.
        return send_email()

    name = _as_name(args_dict.get("name"))
    if not name.strip():
        return MSG_EMPTY_NAME
    if tool == _TOOL_SAVE:
        return save_attachments(name)
    return read_email(name)


def _print_gmail_tool_result(_tool: str, result: str) -> None:
    """Stdout analogo a [FS]/[RAG]: prefisso [GMAIL] sugli esiti OK."""
    if not result.startswith("OK:"):
        return
    if "\n" in result:
        header, body = result.split("\n", 1)
        print(f"[GMAIL] {header}")
        print(body, end="" if body.endswith("\n") else "\n")
    else:
        print(f"[GMAIL] {result}")


def _normalize_hitl_utterance(text: str) -> str:
    """Casefold e punteggiatura finale: «Sì.» conta come sì."""
    return text.strip().casefold().strip(" .,!?;:")


def gmail_hitl_after_tool(tool: str, result: str) -> str | None:
    """Dopo draft_email OK: parla la richiesta di conferma e salta Gemini."""
    if tool != _TOOL_DRAFT or not result.startswith("OK:"):
        return None
    # Togliamo il prefisso OK: dal TTS: l'utente sente solo la frase.
    return result[3:].strip()


def gmail_hitl_on_utterance(text: str) -> str | None:
    """Se c'è una bozza in attesa: sì → send, no → annulla, altro → ripeti.

    None = nessuna HITL in corso, il loop passa l'enunciato a Gemini.
    """
    session = get_draft_session()
    if not session.awaiting_confirm:
        return None
    key = _normalize_hitl_utterance(text)
    if key in _HITL_YES:
        session.mark_confirmed()
        spoken = send_email()
        # send_email ritorna OK:/ERRORE:; per il TTS togliamo OK: se c'è.
        if spoken.startswith("OK:"):
            return spoken[3:].strip()
        return spoken
    if key in _HITL_NO:
        session.clear()
        return "Invio annullato."
    return "Di' sì per inviare o no per annullare."


GMAIL_AGENT_SPEC = AgentSpec(
    name="gmail",
    role="Assistente Gmail vocale",
    goal="Elencare, leggere, salvare allegati e inviare email dopo conferma HITL.",
    tools=tuple(GMAIL_TOOL_MAP),
    dispatch=dispatch_gmail_tool,
    intro_text=_GMAIL_INTRO_TEXT,
    backstory=(
        "Specialista mailbox. Non tocca i file del Desktop: quelli sono dello "
        "specialista FS (`--agent master`)."
    ),
)

GMAIL_LOOP_SPEC = LoopSpec(
    system_prompt=_SYSTEM_PROMPT,
    dispatch=dispatch_gmail_tool,
    intro_text=_GMAIL_INTRO_TEXT,
    gemini_tools=GMAIL_GEMINI_TOOLS,
    print_tool_result=_print_gmail_tool_result,
    hitl_after_tool=gmail_hitl_after_tool,
    hitl_on_utterance=gmail_hitl_on_utterance,
)
