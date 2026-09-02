"""Spec vocale FS/RAG: create/append/read/find sul Desktop.

Uno dei quattro agenti vocali (`master` router, `fs` qui, `gmail` mailbox,
`web` Tavily). Alla pari di `gmail.agent` e `web.agent`. Importa da
`lavora_e_guida.agent` solo `LoopSpec` / `AgentSpec` (contratto del loop),
mai il dispatcher di altri specialisti. Il motore in `agent.py` non importa
questo file a livello di modulo: il default del loop è lazy.

Due modi di girare: `--agent fs` (loop isolato sul Desktop) oppure nested dal
router (`ask_fs`). Flusso composto: ricerca + email è del master (`ask_web`
poi `ask_gmail`); questo specialista non salva un meteo su file se l'utente
non l'ha chiesto.

I tool sono `functionDeclarations` Gemini. Il path lo risolve Python: il
modello passa solo `name` o `query`.
"""

from __future__ import annotations

from typing import Any

from lavora_e_guida.agent import AgentSpec, LoopSpec
from lavora_e_guida.fs.files import (
    FsToolError,
    append_note,
    create_text_file,
    read_file,
)
from lavora_e_guida.fs.find import FindToolError, find_file
from lavora_e_guida.llm.spoken import SPOKEN_REPLY_RULE
from lavora_e_guida.tools.catalog import (
    ToolDeclaration,
    object_schema,
    string_param,
    to_gemini_tools,
)

# Prompt: identità e regole. Gli schemi stanno nelle functionDeclarations.
# SPOKEN_REPLY_RULE è condiviso: stesso vincolo TTS su ogni specialista.
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

# Intro TTS dello specialista FS: non è una reply LLM.
_FS_INTRO_TEXT = (
    "Assistente file sul Desktop: posso creare, aggiornare e leggere file, "
    "cercare per contenuto con find file. Di' esci per terminare."
)

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

# Mappa nome → presenza: il dispatch usa questa whitelist, non importa Gmail/web.
FS_TOOL_MAP: dict[str, Any] = {
    _TOOL_CREATE: create_text_file,
    _TOOL_APPEND: append_note,
    _TOOL_READ: read_file,
    _TOOL_FIND: find_file,
}

_ALLOWED_TOOLS = frozenset(FS_TOOL_MAP)

# Body Gemini: functionDeclarations dello specialista FS.
FS_GEMINI_TOOLS = to_gemini_tools(FS_TOOL_DECLARATIONS)


def dispatch_fs_tool(tool: str, args: dict[str, Any]) -> str:
    """Esegue un tool FS whitelist; ritorna stringa di esito per Gemini.

    Side-effect: I/O FS solo via fs.files (confinato a WORKSPACE_ROOT).
    Mai un'eccezione verso il loop: solo testo già parlabile `OK:` / `ERRORE:`.
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
            # Solo name: testo o PDF; resolve + dispatch pypdf in fs.files.
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


def _print_fs_tool_result(tool: str, result: str) -> None:
    """Stdout dello specialista FS: [FS] per read_file, [RAG] per find_file."""
    if tool == _TOOL_READ:
        _print_read_file_to_terminal(result)
    elif tool == _TOOL_FIND:
        _print_find_file_to_terminal(result)


FS_AGENT_SPEC = AgentSpec(
    name="fs",
    role="Assistente file sul Desktop",
    goal="Creare, aggiornare, leggere e cercare file sotto WORKSPACE_ROOT.",
    tools=tuple(FS_TOOL_MAP),
    dispatch=dispatch_fs_tool,
    intro_text=_FS_INTRO_TEXT,
    backstory=(
        "Specialista filesystem/RAG (`--agent fs`). Non legge Gmail: lo "
        "specialista email è `--agent gmail`."
    ),
)

FS_LOOP_SPEC = LoopSpec(
    system_prompt=_SYSTEM_PROMPT,
    dispatch=dispatch_fs_tool,
    intro_text=_FS_INTRO_TEXT,
    gemini_tools=FS_GEMINI_TOOLS,
    print_tool_result=_print_fs_tool_result,
)
