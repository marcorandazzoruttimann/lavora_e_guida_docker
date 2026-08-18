"""Spec vocale Gmail (sola lettura): prompt Gemini, dispatch, LoopSpec.

Questo modulo è lo specialista email. Importa da `lavora_e_guida.agent` solo
`LoopSpec` (contratto del loop riusabile), mai i tool FS/RAG del master.
Il master non importa questo file: niente `list_emails` / `read_email` sul FS.
"""

from __future__ import annotations

from typing import Any

from lavora_e_guida.agent import LoopSpec
from lavora_e_guida.gmail.read import MSG_EMPTY_NAME, list_emails, read_email

# Nomi tool allineati al JSON del loop: query per elencare, name per leggere.
_TOOL_LIST = "list_emails"
_TOOL_READ = "read_email"
_ALLOWED_TOOLS = frozenset({_TOOL_LIST, _TOOL_READ})

# Intro TTS: identità dello specialista, non una reply LLM (niente telemetria).
_GMAIL_INTRO_TEXT = (
    "Agente Gmail in sola lettura: posso elencare e leggere le email. "
    "Di' esci per terminare."
)

# Prompt Gemini-first: JSON a un oggetto (contratto del loop), esempi ricchi.
# Non è il prompt anorettico del master 3B: query libera, niente whitelist keyword.
_SYSTEM_PROMPT = (
    "Sei l'assistente vocale Gmail in sola lettura. Rispondi ESCLUSIVAMENTE "
    "con un oggetto JSON valido. "
    "Non usare mai blocchi markdown ```json. Nessun testo prima o dopo il JSON.\n\n"
    "TOOL DISPONIBILI E SCHEMI JSON:\n"
    '- Elenca email: {"tool": "list_emails", "args": {"query": "string"}}\n'
    '  Opzionale in args: "limit" integer (default 5, massimo 20).\n'
    '- Leggi un\'email già elencata: {"tool": "read_email", "args": {"name": "string"}}\n'
    '- Risposta parlata: {"tool": "none", "reply": "string"}\n\n'
    "REGOLE TASSATIVE:\n"
    "1. Emetti UN SOLO oggetto JSON con UN SOLO tool per risposta.\n"
    "2. Per elencare o cercare usa list_emails e la chiave query. "
    "Query libera: inbox, operatori Gmail, oppure le parole dell'utente.\n"
    "3. Per leggere usa read_email e la chiave name: indice parlato "
    "(1, la seconda) oppure mittente o oggetto. Non inventare id Gmail.\n"
    "4. Non puoi inviare, cancellare o modificare email. Solo elenco e lettura.\n"
    "5. Dopo un Esito OK o ERRORE di un tool, rispondi SEMPRE con tool none "
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
    "Utente: leggi la seconda\n"
    'JSON: {"tool": "read_email", "args": {"name": "la seconda"}}\n'
    "Utente: leggi quella di Mario\n"
    'JSON: {"tool": "read_email", "args": {"name": "Mario"}}\n'
    "Esito tool list_emails: OK: 2 email in inbox. "
    "1. Da Mario, oggetto Fattura. 2. Da Anna, oggetto Riunione.\n"
    'JSON: {"tool": "none", "reply": '
    '"Hai due email: da Mario, fattura, e da Anna, riunione."}'
)


def _gmail_schema_hint() -> str:
    """Recovery JSON: stessi tool Gmail, tipi nativi string/integer, niente placeholder."""
    return (
        "JSON non valido. Emetti UN SOLO oggetto JSON. Schema ammesso: "
        '{"tool":"none","reply":"string"} oppure '
        '{"tool":"list_emails","args":{"query":"string"}} oppure '
        '{"tool":"read_email","args":{"name":"string"}}.'
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
    """Esegue list_emails o read_email; ritorna OK:/ERRORE: parlante per il loop.

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
        # query libera (operatori o italiano); limit opzionale lo clampano i tool.
        query = _as_query(args_dict.get("query", ""))
        return list_emails(query, limit=args_dict.get("limit"))

    # Whitelist già filtrata: resta solo read_email. name vuoto → errore parlante.
    name = _as_name(args_dict.get("name"))
    if not name.strip():
        return MSG_EMPTY_NAME
    return read_email(name)


def _print_gmail_tool_result(_tool: str, result: str) -> None:
    """Stdout analogo a [FS]/[RAG]: prefisso [GMAIL] sugli esiti OK.

    list_emails è di solito una riga numerata; read_email ha header + corpo.
    Gli ERRORE restano solo nel follow-up verso l'LLM, come nel master.
    Il nome tool non cambia il prefisso: entrambi i tool Gmail usano [GMAIL].
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
