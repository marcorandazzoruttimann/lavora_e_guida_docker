"""Spec vocale Gmail: lettura, allegati, bozza, reply al thread e invio HITL.

Questo modulo è lo specialista email. Importa da `lavora_e_guida.agent` solo
`LoopSpec` / `AgentSpec` (contratto del loop), mai i tool FS/RAG del master.
Il master non importa questo file: niente list/read/save/draft/reply/send sul FS.

I tool sono `functionDeclarations` Gemini. L'invio passa da `draft_email` o
`reply_email` / `reply_all_email` e da un sì vocale in Python: Gemini non può saltare la conferma.
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
    reply_all_email,
    reply_email,
    send_email,
    spoken_draft_confirm,
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
_TOOL_REPLY = "reply_email"
_TOOL_REPLY_ALL = "reply_all_email"
_TOOL_SEND = "send_email"

# draft e reply condividono lo stesso interceptor sì/no del loop.
_HITL_DRAFT_TOOLS = frozenset({_TOOL_DRAFT, _TOOL_REPLY, _TOOL_REPLY_ALL})

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
            "Prepara una bozza nuova, non una risposta al thread. "
            "In to passa cognome, nome e cognome, o indice della mail già "
            "elencata. Non inventare @ o domini: l'indirizzo lo riempie Python "
            "da From o Reply-To. Non invia. Dopo la bozza l'utente dice sì o no. "
            "Non spellingare l'indirizzo nella reply parlata."
        ),
        parameters=object_schema(
            {
                # Esempi concreti (Rossi), niente placeholder <nome>: Python
                # risolve l'@ dalla riga in sessione, Gemini non lo completa.
                "to": string_param(
                    "Cognome come Rossi, nome e cognome come Mario Rossi, "
                    "o indice della mail già elencata. Non inventare un @."
                ),
                "subject": string_param("Oggetto del messaggio."),
                "body": string_param("Testo del messaggio in italiano."),
            },
            required=("to", "subject", "body"),
        ),
    ),
    ToolDeclaration(
        name=_TOOL_REPLY,
        description=(
            "Risponde al mittente di un'email già elencata. "
            "In name passa cognome, nome e cognome, o indice, come read_email. "
            "Non inventare @, oggetto o threadId: li riempie Python da "
            "From o Reply-To. Non invia. Dopo la bozza l'utente dice sì o no. "
            "Non spellingare l'indirizzo nella reply parlata."
        ),
        parameters=object_schema(
            {
                # Stessa chiave name di read_email: Rossi / Mario Rossi / 1.
                "name": string_param(
                    "Indice, cognome come Rossi, o nome e cognome come "
                    "Mario Rossi, dell'email già in lista."
                ),
                "body": string_param("Testo della risposta in italiano."),
            },
            required=("name", "body"),
        ),
    ),
    ToolDeclaration(
        name=_TOOL_REPLY_ALL,
        description=(
            "Risponde a tutti (To e Cc) di un'email già elencata. "
            "In name passa cognome, nome e cognome, o indice, come reply_email. "
            "Non inventare @, oggetto o threadId: li riempie Python da "
            "From, Reply-To, To e Cc. Non invia. Dopo la bozza l'utente dice sì o no. "
            "Non spellingare l'indirizzo nella reply parlata."
        ),
        parameters=object_schema(
            {
                # Stesso schema di reply_email: Rossi / Mario Rossi / 1, niente flag all.
                "name": string_param(
                    "Indice, cognome come Rossi, o nome e cognome come "
                    "Mario Rossi, dell'email già in lista."
                ),
                "body": string_param("Testo della risposta in italiano."),
            },
            required=("name", "body"),
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
    _TOOL_REPLY: reply_email,
    _TOOL_REPLY_ALL: reply_all_email,
    _TOOL_SEND: send_email,
}

_ALLOWED_TOOLS = frozenset(GMAIL_TOOL_MAP)
GMAIL_GEMINI_TOOLS = to_gemini_tools(GMAIL_TOOL_DECLARATIONS)

_GMAIL_INTRO_TEXT = (
    "Agente Gmail: posso elencare, leggere le email, salvare gli allegati, "
    "rispondere e inviare dopo una conferma. Di' esci per terminare."
)

# Prompt: identità e regole. Gli schemi stanno nelle declaration.
# Stesso vincolo TTS del master FS: elenchi email in frasi, non markdown.
_SYSTEM_PROMPT = (
    "Sei l'assistente vocale Gmail. Usa i tool per elencare, leggere, "
    "salvare allegati, preparare email e rispondere ai thread. "
    "Un solo tool per enunciato. "
    "Dopo un tool, riassumi in italiano. Non inventare id Gmail.\n\n"
    "Per elencare o cercare usa list_emails con query libera "
    "(inbox, is:unread, from:mario, fattura). Per contare usa count true. "
    "Giorni: newer_than:3d. Ore: lascia l'italiano in query.\n"
    "Per leggere o salvare allegati usa name: indice parlato, mittente o oggetto. "
    "save_attachments solo se l'utente lo chiede, mai in automatico.\n"
    "L'indirizzo completo non lo inventi: lo riempie Python dalla riga in "
    "sessione, From o Reply-To. In to di draft_email e in name di "
    "reply_email o read_email passa cognome, nome e cognome, o indice "
    "della mail già elencata. Vietato inventare @ o domini.\n"
    "Per scrivere una mail nuova: draft_email con to, subject e body. "
    "Per rispondere al solo mittente: reply_email con name e body, niente to "
    "né subject. Per rispondere a tutti sul thread: reply_all_email con "
    "name e body, niente to né subject. Dopo il tool non spellingare "
    "l'indirizzo: lo dice già la conferma Python. Non chiamare send_email "
    "finché l'utente non ha confermato a voce (sì/no lo gestisce Python).\n\n"
    "Esempi: «ultime email» → list_emails query inbox. "
    "«ultime di Rossi» → list_emails query from:rossi. "
    "«leggi la seconda» → read_email name la seconda. "
    "«scarica gli allegati della prima» → save_attachments name 1. "
    "«scrivi a Rossi oggetto Preventivo testo Arrivo mercoledì» → "
    "draft_email to Rossi. "
    "«rispondi a Rossi che ok» → reply_email name Rossi body ok. "
    "«rispondi alla prima» → reply_email name 1. "
    "«rispondi a tutti» → reply_all_email name 1. "
    "«rispondi a tutti a Rossi che ok» → reply_all_email name Rossi body ok.\n\n"
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

    if tool == _TOOL_REPLY:
        # name può arrivare integer da Gemini (1); _as_name lo rende cifra.
        return reply_email(
            name=_as_name(args_dict.get("name")),
            body=args_dict.get("body", ""),
        )

    if tool == _TOOL_REPLY_ALL:
        # Stesso name integer di reply_email; un tool eseguito per enunciato.
        return reply_all_email(
            name=_as_name(args_dict.get("name")),
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
    """Dopo draft_email / reply_email / reply_all_email OK: parla e salta Gemini."""
    if tool not in _HITL_DRAFT_TOOLS or not result.startswith("OK:"):
        return None
    # Togliamo il prefisso OK: dal TTS: l'utente sente solo la frase.
    return result[3:].strip()


def gmail_hitl_on_utterance(text: str) -> str | None:
    """Se c'è una bozza in attesa: sì → send, no → annulla, altro → ripeti.

    Il retry riusa `spoken_draft_confirm` sulla bozza in sessione (to, oggetto,
    corpo), non solo «sì o no». None = nessuna HITL, il loop passa a Gemini.
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
    # Né sì né no: stessa frase della bozza, col corpo, non solo il sì/no.
    draft = session.draft
    if draft is None:
        # awaiting_confirm senza bozza non dovrebbe accadere; fallback parlante.
        return "Di' sì per inviare o no per annullare."
    return spoken_draft_confirm(to=draft.to, subject=draft.subject, body=draft.body, cc=draft.cc)


GMAIL_AGENT_SPEC = AgentSpec(
    name="gmail",
    role="Assistente Gmail vocale",
    goal="Elencare, leggere, salvare allegati, rispondere e inviare dopo HITL.",
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
