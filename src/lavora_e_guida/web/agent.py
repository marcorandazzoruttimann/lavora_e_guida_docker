"""Spec vocale dell'agente di ricerca web: un solo tool `web_search` su Tavily.

Copia strutturale dello specialista Gmail: importa da `lavora_e_guida.agent`
solo `LoopSpec` / `AgentSpec` (il contratto del loop), mai i tool FS/RAG del
master, e il master non importa questo file.

Il pattern è quello del function calling, non del grounding nativo: Gemini
emette una `functionCall web_search`, Python esegue la REST Tavily e rimanda
l'esito come `functionResponse`, la sintesi parlata la scrive Gemini al turno
successivo. Niente HITL: una ricerca è read-only, non c'è nulla da confermare.

Vincolo vocale: la stringa che torna al modello cita le fonti come dominio
(`corriere.it`); gli URL completi finiscono solo sullo stdout `[WEB]`, dove
leggerli ha senso.
"""

from __future__ import annotations

from typing import Any

from lavora_e_guida.agent import AgentSpec, LoopSpec
from lavora_e_guida.llm.spoken import SPOKEN_REPLY_RULE
from lavora_e_guida.tools.catalog import (
    ToolDeclaration,
    enum_param,
    integer_param,
    object_schema,
    string_param,
    to_gemini_tools,
)
from lavora_e_guida.web.search import (
    DEFAULT_MAX_RESULTS,
    MAX_MAX_RESULTS,
    get_last_web_results,
    web_search,
)

# Nome del tool in un'unica costante: declaration, mappa e dispatch non possono
# divergere per un refuso (stesso accorgimento dei `_TOOL_*` di Gmail).
_TOOL_SEARCH = "web_search"

# Valori degli enum, allineati alle whitelist di `web/search.py`. Dichiararli
# nello schema fa smettere Gemini di inventare `topic=finance` o `time_range=oggi`.
_TOPIC_VALUES = ("general", "news")
_TIME_RANGE_VALUES = ("day", "week", "month", "year")

# Catalogo web isolato: né il master FS né lo specialista Gmail importano di qui.
WEB_TOOL_DECLARATIONS: tuple[ToolDeclaration, ...] = (
    ToolDeclaration(
        name=_TOOL_SEARCH,
        description=(
            "Cerca sul web e restituisce titoli, fonte e riassunto breve. "
            "Usalo quando la risposta dipende da fatti recenti o da pagine "
            "online. Per le notizie di attualità metti topic news e "
            "time_range day o week. Un solo tool per enunciato."
        ),
        parameters=object_schema(
            {
                # Stessa chiave `query` di `find_file` e `list_emails`: dati
                # uguali, nomi uguali, come chiede il contratto dei tool.
                "query": string_param(
                    "Cosa cercare, in lingua naturale: meteo Roma domani, "
                    "risultati campionato, prezzo del gas oggi."
                ),
                # Enum invece di stringa libera: la validazione sta nello schema,
                # Python poi ignora comunque un valore fuori lista (non fallisce).
                "topic": enum_param(
                    "news per notizie e attualità, general per tutto il resto. "
                    "Se l'utente non parla di cronaca, omettilo.",
                    _TOPIC_VALUES,
                ),
                "time_range": enum_param(
                    "Finestra temporale dei risultati: day per oggi, week per "
                    "questa settimana. Ometti se la domanda non è legata al tempo.",
                    _TIME_RANGE_VALUES,
                ),
                "max_results": integer_param(
                    f"Quanti risultati cercare (default {DEFAULT_MAX_RESULTS}, "
                    f"massimo {MAX_MAX_RESULTS}). Di norma ometti."
                ),
            },
            required=("query",),
        ),
    ),
)

# Mappa nome → callable: qui c'è un tool solo, ma il dizionario tiene la stessa
# forma degli altri specialisti (e alimenta whitelist e `AgentSpec.tools`).
WEB_TOOL_MAP: dict[str, Any] = {
    _TOOL_SEARCH: web_search,
}

_ALLOWED_TOOLS = frozenset(WEB_TOOL_MAP)
WEB_GEMINI_TOOLS = to_gemini_tools(WEB_TOOL_DECLARATIONS)

# Intro parlata all'avvio: identità dell'agente e via d'uscita, come le altre.
_WEB_INTRO_TEXT = (
    "Agente di ricerca web: dimmi cosa vuoi sapere e cerco online, "
    "poi ti riassumo i risultati citando le fonti. Di' esci per terminare."
)

# Prompt: identità e regole di comportamento. Gli schemi dei parametri stanno
# nella declaration, non qui: duplicarli è solo contesto sprecato.
_SYSTEM_PROMPT = (
    "Sei l'assistente vocale di ricerca web. Quando la risposta dipende da "
    "fatti recenti o da pagine online usa il tool web_search, non la tua "
    "memoria. Un solo tool per enunciato.\n\n"
    "Per le notizie di attualità usa topic news, e time_range day per oggi o "
    "week per questa settimana. Per le domande generiche ometti topic e "
    "time_range. Ometti anche max_results, il valore di default va bene quasi "
    "sempre.\n"
    "Dopo il tool riassumi in italiano in poche frasi: prima la risposta, poi "
    "le fonti. Cita la fonte col nome del sito, per esempio secondo ansa punto "
    "it, e non leggere mai l'indirizzo completo di una pagina.\n"
    "Se il tool risponde ERRORE, di' all'utente cosa è andato storto e fermati: "
    "non inventare risultati né contenuti di pagine che non hai letto. Se il "
    "tool non trova nulla, dillo e proponi di riformulare la ricerca.\n"
    "Se la richiesta è vaga, prima chiedi cosa cercare esattamente, poi cerca.\n\n"
    "Esempi: «cerca chi ha vinto il campionato» → web_search query chi ha vinto "
    "il campionato. «che notizie ci sono oggi» → web_search query notizie di "
    "oggi, topic news, time_range day. «novità sull'intelligenza artificiale "
    "questa settimana» → web_search query novità intelligenza artificiale, "
    "topic news, time_range week.\n\n"
    f"{SPOKEN_REPLY_RULE}"
)


def dispatch_web_tool(tool: str, args: dict[str, Any]) -> str:
    """Esegue i tool web in whitelist; ritorna la stringa `OK:` / `ERRORE:`.

    Contratto identico agli altri dispatch: mai un'eccezione verso il loop, solo
    testo già parlabile che rientra come `functionResponse`. La validazione fine
    (query vuota, enum fuori lista, clamp di `max_results`) sta in `web_search`:
    qui si controlla soltanto che il nome del tool sia ammesso.

    Side-effect: la POST HTTPS verso Tavily fatta da `web_search` (un credito).
    """
    if tool not in _ALLOWED_TOOLS:
        # Nome inventato dal modello: elenchiamo il consentito, senza eseguire nulla.
        allowed = ", ".join(sorted(_ALLOWED_TOOLS))
        return f"ERRORE: tool sconosciuto {tool!r}. Consentiti: {allowed}."

    # Gemini può mandare `args` assente o non-dict: normalizziamo a dizionario
    # vuoto, così i `.get` sotto non esplodono e la query risulta mancante.
    args_dict = args if isinstance(args, dict) else {}

    # Unico tool ammesso: chiamata diretta con le chiavi della declaration.
    # `query` non-stringa (numero, None) viene gestita da `web_search` stesso.
    query = args_dict.get("query", "")
    return web_search(
        query if isinstance(query, str) else str(query or ""),
        topic=args_dict.get("topic"),
        time_range=args_dict.get("time_range"),
        max_results=args_dict.get("max_results"),
    )


def _print_web_tool_result(tool: str, result: str) -> None:
    """Stdout analogo a [FS]/[RAG]/[GMAIL]: prefisso [WEB] e URL per esteso.

    A voce le fonti sono solo domini; a schermo servono invece gli indirizzi
    completi, per poter aprire la pagina. Li prendiamo da
    `get_last_web_results`, cioè dallo stato dell'ultima ricerca riuscita, e
    non dalla stringa mandata a Gemini, che gli URL non li contiene.

    Side-effect: print su stdout. Gli errori non si stampano: li dice già il TTS.
    """
    if tool != _TOOL_SEARCH or not result.startswith("OK:"):
        return
    # Intestazione: la stessa riga di esito che ha visto il modello.
    print(f"[WEB] {result}")
    # Elenco numerato degli indirizzi, nello stesso ordine della frase parlata.
    for index, item in enumerate(get_last_web_results(), start=1):
        # Risultato senza URL (campo mancante nel payload): niente riga vuota.
        if not item.url:
            continue
        print(f"  {index}. {item.url}")


WEB_AGENT_SPEC = AgentSpec(
    name="web",
    role="Assistente di ricerca web vocale",
    goal="Cercare sul web con Tavily e riassumere a voce citando le fonti.",
    tools=tuple(WEB_TOOL_MAP),
    dispatch=dispatch_web_tool,
    intro_text=_WEB_INTRO_TEXT,
    backstory=(
        "Specialista di ricerca online. Non tocca i file del Desktop né la "
        "mailbox: quelli sono degli specialisti FS (`--agent master`) e Gmail."
    ),
)

WEB_LOOP_SPEC = LoopSpec(
    system_prompt=_SYSTEM_PROMPT,
    dispatch=dispatch_web_tool,
    intro_text=_WEB_INTRO_TEXT,
    gemini_tools=WEB_GEMINI_TOOLS,
    print_tool_result=_print_web_tool_result,
    # Nessun HITL: la ricerca è read-only, non c'è un invio da confermare.
)
