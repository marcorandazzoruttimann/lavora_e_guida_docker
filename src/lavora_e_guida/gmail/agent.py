"""Spec vocale Gmail (sola lettura): prompt Gemini, dispatch, LoopSpec.

Questo modulo è lo specialista email. Importa da `lavora_e_guida.agent` solo
`LoopSpec` (contratto del loop riusabile), mai i tool FS/RAG del master.
Il master non importa questo file: niente `list_emails` / `read_email` /
`save_attachments` sul FS.
"""

from __future__ import annotations

from typing import Any

from lavora_e_guida.agent import LoopSpec
from lavora_e_guida.gmail.read import (
    MSG_EMPTY_NAME,
    list_emails,
    read_email,
    save_attachments,
)

# Nomi tool allineati al JSON del loop: query per elencare, name per leggere/salvare.
_TOOL_LIST = "list_emails"
_TOOL_READ = "read_email"
_TOOL_SAVE = "save_attachments"
_ALLOWED_TOOLS = frozenset({_TOOL_LIST, _TOOL_READ, _TOOL_SAVE})

# Intro TTS: identità dello specialista, non una reply LLM (niente telemetria).
_GMAIL_INTRO_TEXT = (
    "Agente Gmail in sola lettura: posso elencare, leggere le email "
    "e salvare gli allegati. Di' esci per terminare."
)

# Prompt Gemini-first: JSON a un oggetto (contratto del loop), esempi ricchi.
# Non è il prompt anorettico del master 3B: query libera, niente whitelist keyword.
# count/tempo: tipi nativi e esempi concreti; niente after:epoch inventato (echo).
# Chiave name unica per read_email e save_attachments (omogeneità JSON).
_SYSTEM_PROMPT = (
    "Sei l'assistente vocale Gmail in sola lettura. Rispondi ESCLUSIVAMENTE "
    "con un oggetto JSON valido. "
    "Non usare mai blocchi markdown ```json. Nessun testo prima o dopo il JSON.\n\n"
    "TOOL DISPONIBILI E SCHEMI JSON:\n"
    '- Elenca o conta email: {"tool": "list_emails", "args": {"query": "string"}}\n'
    '  Opzionale in args: "limit" integer (default 5, massimo 20).\n'
    '  Opzionale in args: "count" true per il totale, senza limit.\n'
    '- Leggi un\'email già elencata: {"tool": "read_email", "args": {"name": "string"}}\n'
    "- Scarica gli allegati di un'email già elencata: "
    '{"tool": "save_attachments", "args": {"name": "string"}}\n'
    '- Risposta parlata: {"tool": "none", "reply": "string"}\n\n'
    "REGOLE TASSATIVE:\n"
    "1. Emetti UN SOLO oggetto JSON con UN SOLO tool per risposta.\n"
    "2. Per elencare o cercare usa list_emails e la chiave query. "
    "Query libera: inbox, operatori Gmail, oppure le parole dell'utente.\n"
    "3. Per contare (quante email) usa list_emails con count true. "
    "Giorni: newer_than:3d. Ore: italiano in query, mai newer_than:5h. "
    "Non inventare timestamp Unix.\n"
    "4. Per leggere usa read_email e la chiave name: indice parlato "
    "(1, la seconda) oppure mittente o oggetto. Non inventare id Gmail.\n"
    "5. Per scaricare gli allegati usa save_attachments con la stessa chiave name. "
    "Solo se l'utente lo chiede, mai in automatico dopo la lettura.\n"
    "6. Non puoi inviare, cancellare o modificare email. Solo elenco, "
    "conteggio, lettura e salvataggio allegati.\n"
    "7. Dopo un Esito OK o ERRORE di un tool, rispondi SEMPRE con tool none "
    "e la sintesi in reply. Non richiamare lo stesso tool.\n\n"
    "ESEMPI:\n"
    "Utente: ultime email\n"
    'JSON: {"tool": "list_emails", "args": {"query": "inbox"}}\n'
    "Utente: non lette\n"
    'JSON: {"tool": "list_emails", "args": {"query": "is:unread"}}\n'
    "Utente: non lette da Mario\n"
    'JSON: {"tool": "list_emails", "args": {"query": "from:mario is:unread"}}\n'
    "Utente: cerca fattura\n"
    'JSON: {"tool": "list_emails", "args": {"query": "fattura"}}\n'
    "Utente: mostrami le ultime tre\n"
    'JSON: {"tool": "list_emails", "args": {"query": "inbox", "limit": 3}}\n'
    "Utente: quante email da Mario\n"
    'JSON: {"tool": "list_emails", "args": {"query": "from:mario", "count": true}}\n'
    "Utente: quante da Mario negli ultimi 3 giorni\n"
    'JSON: {"tool": "list_emails", "args": '
    '{"query": "from:mario newer_than:3d", "count": true}}\n'
    "Utente: quante da Mario nelle ultime 5 ore\n"
    'JSON: {"tool": "list_emails", "args": '
    '{"query": "from:mario ultime 5 ore", "count": true}}\n'
    "Utente: leggi la seconda\n"
    'JSON: {"tool": "read_email", "args": {"name": "la seconda"}}\n'
    "Utente: leggi quella di Mario\n"
    'JSON: {"tool": "read_email", "args": {"name": "Mario"}}\n'
    "Utente: scarica gli allegati della prima\n"
    'JSON: {"tool": "save_attachments", "args": {"name": "1"}}\n'
    "Utente: scarica gli allegati di quella di Mario\n"
    'JSON: {"tool": "save_attachments", "args": {"name": "Mario"}}\n'
    "Esito tool save_attachments: OK: 1 allegato salvato in "
    "email_attachments/2026-08-19: fattura.pdf.\n"
    'JSON: {"tool": "none", "reply": '
    '"Ho salvato fattura.pdf nella cartella degli allegati di oggi."}\n'
    "Esito tool list_emails: OK: 2 email in inbox. "
    "1. Da Mario, oggetto Fattura. 2. Da Anna, oggetto Riunione.\n"
    'JSON: {"tool": "none", "reply": '
    '"Hai due email: da Mario, fattura, e da Anna, riunione."}\n'
    "Esito tool list_emails: OK: 42 email da mario.\n"
    'JSON: {"tool": "none", "reply": "Hai 42 email da Mario."}'
)


def _gmail_schema_hint() -> str:
    """Recovery JSON: stessi tool Gmail, tipi nativi string/integer/true, niente placeholder."""
    return (
        "JSON non valido. Emetti UN SOLO oggetto JSON. Schema ammesso: "
        '{"tool":"none","reply":"string"} oppure '
        '{"tool":"list_emails","args":{"query":"string"}} '
        "(count true solo se l'utente chiede quante) oppure "
        '{"tool":"read_email","args":{"name":"string"}} oppure '
        '{"tool":"save_attachments","args":{"name":"string"}}.'
    )


def _as_query(raw: object) -> str:
    """Normalizza args.query: assente o None = inbox; il resto diventa stringa."""
    # Query vuota è valida: list_emails la mappa a in:inbox.
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    # Gemini a volte emette un numero; Python inoltra il testo, non lo scarta.
    return str(raw)


def _as_name(raw: object) -> str:
    """Normalizza args.name: indice JSON integer → cifra parlata; bool non è un nome."""
    # bool è sottoclasse di int: True non deve diventare "1" come email da leggere.
    if raw is None or isinstance(raw, bool):
        return ""
    if isinstance(raw, int):
        return str(raw)
    if isinstance(raw, str):
        return raw
    return str(raw)


def dispatch_gmail_tool(tool: str, args: dict[str, Any]) -> str:
    """Esegue list_emails, read_email o save_attachments; ritorna OK:/ERRORE:.

    Side-effect: REST Gmail e aggiornamento MailboxSession solo via gmail.read.
    Gli id messaggio restano in Python: questo dispatch non li mette nella stringa.
    """
    # Whitelist stretta: send/modify e i tool FS del master non esistono qui.
    if tool not in _ALLOWED_TOOLS:
        allowed = ", ".join(sorted(_ALLOWED_TOOLS))
        return (
            f"ERRORE: tool sconosciuto {tool!r}. "
            f"Consentiti: {allowed} oppure tool=none."
        )

    # Il caller (run_chat_loop) garantisce un dict; difensivo se arriva altro.
    args_dict = args if isinstance(args, dict) else {}

    if tool == _TOOL_LIST:
        # query libera (operatori o italiano); limit e count li interpreta list_emails.
        # count assente = elenco vocale (default 5); count true = totale, sessione intatta.
        query = _as_query(args_dict.get("query", ""))
        return list_emails(
            query,
            limit=args_dict.get("limit"),
            count=args_dict.get("count"),
        )

    # read_email e save_attachments condividono la chiave name (indice o mittente).
    name = _as_name(args_dict.get("name"))
    if not name.strip():
        return MSG_EMPTY_NAME
    if tool == _TOOL_SAVE:
        return save_attachments(name)
    return read_email(name)


def _print_gmail_tool_result(_tool: str, result: str) -> None:
    """Stdout analogo a [FS]/[RAG]: prefisso [GMAIL] sugli esiti OK.

    list_emails è di solito una riga numerata; read_email ha header + corpo;
    save_attachments è una riga con i nomi file scritti sul Desktop.
    Gli ERRORE restano solo nel follow-up verso l'LLM, come nel master.
    Il nome tool non cambia il prefisso: i tool Gmail usano tutti [GMAIL].
    """
    if not result.startswith("OK:"):
        return
    # Prima riga = header parlante; resto = corpo dell'email se c'è un newline.
    if "\n" in result:
        header, body = result.split("\n", 1)
        print(f"[GMAIL] {header}")
        print(body, end="" if body.endswith("\n") else "\n")
    else:
        print(f"[GMAIL] {result}")


# Spec pubblico: main (todo CLI) e i test passano questo a run_chat_loop.
GMAIL_LOOP_SPEC = LoopSpec(
    system_prompt=_SYSTEM_PROMPT,
    dispatch=dispatch_gmail_tool,
    intro_text=_GMAIL_INTRO_TEXT,
    schema_hint=_gmail_schema_hint(),
    print_tool_result=_print_gmail_tool_result,
)
