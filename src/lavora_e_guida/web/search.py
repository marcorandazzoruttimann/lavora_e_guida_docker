"""Tool `web_search`: ricerca su Tavily, esito parlante per il TTS.

Gemini chiama `web_search` via function calling; qui si esegue la REST Tavily
con l'SDK ufficiale e si restituisce una stringa `OK:` / `ERRORE:` che rientra
nel loop come `functionResponse`. La sintesi la fa Gemini: chiediamo a Tavily
snippet grezzi (`include_answer=False`), non la sua risposta già confezionata.

Vincolo vocale: l'elenco parlato cita le fonti come **dominio**
(`corriere.it`), mai l'URL completo, perché `SPOKEN_REPLY_RULE` vieta di
spellare indirizzi a voce. Gli indirizzi interi arrivano comunque al modello,
ma in coda e sotto un'etichetta che ne limita l'uso (`URL_SECTION_INTRO`): il
prompt gli permette di darli solo se l'utente li chiede espressamente. Senza
questa appendice Gemini non avrebbe alcun URL vero e, se glieli chiedessimo,
se li inventerebbe. Gli stessi indirizzi restano anche nella lista in-process
(`get_last_web_results`), che l'agente stampa a schermo col prefisso `[WEB]`.

Side-effect: una POST HTTPS verso api.tavily.com (costo in crediti) e la
riscrittura della lista dei risultati dell'ultima ricerca del processo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

# `requests` è la dipendenza HTTP di tavily-python: rete giù o 5xx risalgono da
# lì (l'SDK mappa in eccezioni proprie solo 400/401/403/429), quindi la catturiamo.
import requests
from tavily import TavilyClient
from tavily.errors import (
    BadRequestError,
    ForbiddenError,
    InvalidAPIKeyError,
    KeylessUnsupportedEndpointError,
    MissingAPIKeyError,
    TavilyKeylessLimitError,
    UsageLimitExceededError,
)

# L'SDK definisce un `TimeoutError` che ombreggia quello builtin e non ne è
# sottoclasse: alias esplicito, altrimenti l'except prenderebbe quello sbagliato.
from tavily.errors import TimeoutError as TavilyTimeoutError

from lavora_e_guida.config import Settings, get_settings

# Risposta parlata, non una SERP: cinque risultati bastano da ascoltare.
DEFAULT_MAX_RESULTS = 5
# Tetto duro: oltre otto snippet il turno vocale diventa illeggibile e il
# contesto mandato a Gemini si gonfia senza aggiungere informazione utile.
MAX_MAX_RESULTS = 8

# Profondità fissa: `basic` costa 1 credito, `advanced` ne costa 2. Non usiamo
# `auto_parameters` proprio perché può promuovere la ricerca ad advanced da sola.
SEARCH_DEPTH = "basic"

# Taglio per snippet: Tavily può restituire paragrafi lunghi, qui serve solo
# il materiale da far riassumere a Gemini in una frase.
MAX_SNIPPET_CHARS = 400

# Un minuto, come il default dell'SDK. Mezzo minuto sembrava più adatto a un
# loop vocale, ma è un timeout di orologio sulla socket: sotto debugger (o con
# rete lenta) scadeva su ricerche che Tavily aveva già servito, e la richiesta
# consumava comunque il credito. Meglio aspettare che pagare per niente.
SEARCH_TIMEOUT = 60.0

# Valori accettati da Tavily e replicati nella declaration dell'agente. Un
# valore fuori lista non è un errore parlante: si omette e vale il default API.
_ALLOWED_TOPICS: frozenset[str] = frozenset({"general", "news"})
_ALLOWED_TIME_RANGES: frozenset[str] = frozenset({"day", "week", "month", "year"})

# Messaggi TTS: prefisso ERRORE, niente stacktrace né testo inglese dell'SDK.
MSG_EMPTY_QUERY = "ERRORE: dimmi cosa devo cercare sul web"
MSG_MISSING_KEY = "ERRORE: chiave Tavily assente, imposta TAVILY_API_KEY"
MSG_INVALID_KEY = "ERRORE: chiave Tavily non valida, controlla TAVILY_API_KEY"
MSG_QUOTA = "ERRORE: crediti Tavily esauriti, riprova più tardi"
MSG_FORBIDDEN = "ERRORE: ricerca web non consentita dal piano Tavily"
MSG_BAD_REQUEST = "ERRORE: Tavily ha rifiutato la ricerca, prova a riformularla"
MSG_TIMEOUT = "ERRORE: la ricerca web ci ha messo troppo, riprova"
MSG_UNREACHABLE = "ERRORE: Tavily non raggiungibile, riprova più tardi"

# Qualsiasi URL dentro titolo o snippet: a voce diventerebbe uno spelling
# infinito, quindi lo sostituiamo col solo dominio (vedi `_strip_urls`).
_URL_RE = re.compile(r"https?://\S+")

# Residui markdown negli snippet Tavily (grassetti, elenchi): il testo passa da
# Gemini prima del TTS, ma meglio non insegnargli asterischi.
_MARKUP_CHARS = ("**", "__", "*", "#", "`")

# `www.` non si pronuncia: la fonte parlata è `corriere.it`, non `www.corriere.it`.
_WWW_PREFIX = "www."

# Etichetta dell'appendice con gli indirizzi interi. Non è testo da leggere: è
# un'istruzione per Gemini, che senza questa riga tratterebbe gli URL come
# materiale da riassumere a voce. Sta su una riga a parte (vedi lo `\n` in
# `format_search_result`) così chi stampa a schermo può separarla in un colpo.
URL_SECTION_INTRO = (
    "Indirizzi completi delle fonti, nello stesso ordine: dalli all'utente solo "
    "se li chiede espressamente, altrimenti cita soltanto il nome del sito."
)


class WebSearchError(ValueError):
    """Contratto del tool (query vuota, chiave assente): messaggio già per il TTS."""


@dataclass(frozen=True)
class WebResult:
    """Un risultato Tavily normalizzato: parlato (dominio) e schermo (url) insieme.

    `snippet` è già ripulito da URL e markdown; `url` resta intero perché serve
    allo stdout dell'agente, dove leggere l'indirizzo ha senso.
    """

    title: str
    url: str
    domain: str
    snippet: str


# Ultima ricerca del processo: l'agente stampa gli URL completi da qui, senza
# doverli infilare nella stringa che va a Gemini e poi al TTS.
_LAST_RESULTS: list[WebResult] = []


def get_last_web_results() -> tuple[WebResult, ...]:
    """Risultati dell'ultima `web_search` andata a buon fine (tuple immutabile)."""
    # Copia: chi stampa non deve poter svuotare la lista del modulo.
    return tuple(_LAST_RESULTS)


def reset_last_web_results() -> None:
    """Svuota la lista dell'ultima ricerca: isolamento tra i casi di test."""
    _LAST_RESULTS.clear()


@lru_cache
def get_tavily_client() -> TavilyClient:
    """Client Tavily del processo, costruito una volta sola (sessione riusata).

    Chiave assente o vuota → `WebSearchError` parlante: senza `api_key` l'SDK
    entrerebbe in modalità keyless e fallirebbe più avanti con un messaggio
    inglese. Il fail-fast all'avvio resta comunque nella CLI.
    I test monkeypatchano questa funzione (o passano `client=` a `web_search`),
    così nei test non parte mai una richiesta di rete.
    """
    settings: Settings = get_settings()
    # `or ""` perché il campo è opzionale e nel `.env` può essere `TAVILY_API_KEY=`.
    api_key = (settings.tavily_api_key or "").strip()
    if not api_key:
        raise WebSearchError(MSG_MISSING_KEY)
    return TavilyClient(api_key=api_key)


def reset_tavily_client() -> None:
    """Butta il client memoizzato: serve ai test e dopo un cambio di chiave."""
    get_tavily_client.cache_clear()


def clamp_max_results(value: object) -> int:
    """Default 5, minimo 1, tetto 8. Accetta l'int JSON o la stringa del modello."""
    # Argomento omesso dalla functionCall: si va col default vocale.
    if value is None:
        return DEFAULT_MAX_RESULTS
    # `bool` è sottoclasse di `int`: True diventerebbe un pericoloso «1 risultato».
    if isinstance(value, bool):
        return DEFAULT_MAX_RESULTS
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        # Gemini ha mandato «cinque» o spazzatura: meglio il default che un errore.
        return DEFAULT_MAX_RESULTS
    if number < 1:
        return 1
    return min(number, MAX_MAX_RESULTS)


def _clean_enum(value: object, allowed: frozenset[str]) -> str | None:
    """Normalizza un parametro enum; fuori whitelist → None (default lato API).

    Un `topic` inventato non merita un `ERRORE:` in faccia all'utente: la
    ricerca generica è comunque una risposta sensata alla sua domanda.
    """
    if not isinstance(value, str):
        return None
    token = value.strip().casefold()
    return token if token in allowed else None


def _domain_from_url(url: str) -> str:
    """Host parlabile da un URL: niente schema, niente `www.`, niente porta.

    Vuoto se l'URL manca o non è parsabile: il formatter allora omette del tutto
    il pezzo «fonte», invece di dire «fonte» e poi niente.
    """
    text = (url or "").strip()
    if not text:
        return ""
    try:
        netloc = urlsplit(text).netloc
    except ValueError:
        # URL malformato (IPv6 rotto, caratteri illegali): nessuna fonte parlata.
        return ""
    # `user:pass@host` esiste ancora in giro: dell'autenticazione non ci importa.
    host = netloc.rsplit("@", 1)[-1].strip().casefold()
    # Porta esplicita (`:8080`): non si pronuncia. Occhio a non tagliare IPv6.
    if ":" in host and not host.startswith("["):
        host = host.split(":", 1)[0]
    return host.removeprefix(_WWW_PREFIX)


def _strip_urls(text: str) -> str:
    """Sostituisce gli URL nel testo col loro dominio: la frase resta sensata.

    Gli snippet Tavily citano spesso link interni. Cancellarli spezzerebbe la
    frase, leggerli ad alta voce sarebbe peggio: il dominio è il compromesso.
    """

    def _repl(match: re.Match[str]) -> str:
        # Se l'URL non è parsabile lo togliamo e basta: mai spelling a voce.
        return _domain_from_url(match.group(0))

    return _URL_RE.sub(_repl, text or "")


def _clean_text(raw: object) -> str:
    """Campo Tavily (titolo o contenuto) → testo parlabile su una riga sola.

    Ordine dei passi: prima gli URL (il markdown non deve spezzarli), poi i
    marcatori, infine il collasso degli spazi che uccide gli a capo.
    """
    if not isinstance(raw, str):
        return ""
    out = _strip_urls(raw)
    for token in _MARKUP_CHARS:
        out = out.replace(token, " ")
    return " ".join(out.split())


def _truncate_snippet(text: str, max_chars: int = MAX_SNIPPET_CHARS) -> str:
    """Taglia lo snippet; se possibile all'ultimo spazio, per non mozzare parole."""
    body = (text or "").strip()
    if max_chars <= 0 or len(body) <= max_chars:
        return body
    cut = body[:max_chars]
    # Spazio abbastanza avanti: tagliamo lì. Altrimenti è una parola lunghissima
    # (URL residuo, token tecnico) e il taglio netto è il male minore.
    space = cut.rfind(" ")
    if space > max_chars // 2:
        cut = cut[:space]
    return cut.rstrip()


def _results_from_payload(payload: Any) -> list[WebResult]:
    """Normalizza `payload["results"]` in WebResult, scartando le righe inutili.

    L'SDK garantisce la chiave `results` (fa `setdefault`), non il suo contenuto:
    ogni voce si controlla a mano. Senza titolo né snippet la riga non è
    pronunciabile e viene saltata invece di produrre un «1. , fonte .».
    """
    found: list[WebResult] = []
    if not isinstance(payload, dict):
        return found
    rows = payload.get("results")
    if not isinstance(rows, list):
        return found
    for row in rows:
        # Difensivo: una stringa in mezzo alla lista non deve far esplodere il tool.
        if not isinstance(row, dict):
            continue
        url = row.get("url")
        url_text = url.strip() if isinstance(url, str) else ""
        title = _clean_text(row.get("title"))
        # `content` è lo snippet estratto da Tavily, non la pagina intera.
        snippet = _truncate_snippet(_clean_text(row.get("content")))
        if not title and not snippet:
            continue
        found.append(
            WebResult(
                title=title,
                url=url_text,
                domain=_domain_from_url(url_text),
                snippet=snippet,
            )
        )
    return found


def format_search_result(query: str, results: list[WebResult]) -> str:
    """Esito in due parti: elenco parlato (domini) più appendice con gli URL.

    Prima riga, quella che Gemini riassume a voce: `OK: 3 risultati per meteo
    Roma. 1. Titolo, fonte ansa.it. Snippet.` — solo domini, nessun indirizzo
    da spellare. Seconda riga, presente solo se almeno un risultato ha un URL:
    `URL_SECTION_INTRO` più gli indirizzi numerati come sopra, così il modello
    può darli quando l'utente li chiede senza doverseli inventare.

    Lista vuota: formula dedicata, che non è un errore ma un esito legittimo.
    """
    if not results:
        return f"OK: nessun risultato trovato per {query}."
    pieces: list[str] = []
    # Appendice: stessa numerazione dell'elenco parlato, così «il secondo link»
    # dell'utente e «la seconda fonte» del riassunto indicano la stessa pagina.
    links: list[str] = []
    for index, item in enumerate(results, start=1):
        # Titolo assente ma snippet sì: la riga vale ancora, senza frase vuota.
        head = item.title or "risultato senza titolo"
        # Dominio assente (url rotto): niente «fonte» monca nel parlato.
        source = f", fonte {item.domain}" if item.domain else ""
        # Punteggiatura già presente in coda: evitiamo il doppio punto.
        body = f" {item.snippet.rstrip(' .;,')}." if item.snippet else ""
        pieces.append(f"{index}. {head}{source}.{body}")
        # Risultato senza indirizzo (campo mancante nel payload): si salta il
        # numero nell'appendice invece di scrivere un «2. » senza link dietro.
        if item.url:
            links.append(f"{index}. {item.url}")
    count = len(results)
    # Singolare/plurale: «1 risultati» è la classica stonatura da TTS.
    noun = "risultato" if count == 1 else "risultati"
    spoken = f"OK: {count} {noun} per {query}. {' '.join(pieces)}"
    # Nessun URL in tutta la risposta: niente appendice, l'esito resta una riga.
    if not links:
        return spoken
    # `\n` come unico separatore: `split("\n", 1)` basta a chi vuole solo il parlato.
    return f"{spoken}\n{URL_SECTION_INTRO} {' '.join(links)}"


def _spoken_error(exc: BaseException) -> str:
    """Normalizza a stringa `ERRORE:` (WebSearchError è già prefissata)."""
    text = str(exc).strip()
    if text.startswith("ERRORE:"):
        return text
    return f"ERRORE: {text}"


def web_search(
    query: str = "",
    *,
    topic: object = None,
    time_range: object = None,
    max_results: object = None,
    client: Any | None = None,
) -> str:
    """Cerca sul web con Tavily e ritorna una stringa `OK:` / `ERRORE:` parlante.

    `query` è la domanda in lingua naturale. `topic` (general/news) e
    `time_range` (day/week/month/year) sono gli enum della declaration: un
    valore fuori whitelist viene ignorato, non fatto fallire. `max_results` è
    clampato tra 1 e 8. `client` esiste per i test (client finto), a runtime si
    usa quello memoizzato del processo.

    Side-effect: una POST verso api.tavily.com che consuma un credito e la
    riscrittura di `get_last_web_results` (solo in caso di successo).
    """
    # Collapse degli spazi STT: «  meteo   Roma » è la stessa query di «meteo Roma».
    cleaned_query = " ".join((query or "").split())
    if not cleaned_query:
        # Niente rete, niente crediti: il modello deve richiedere cosa cercare.
        return MSG_EMPTY_QUERY

    # Enum e limite si normalizzano prima della chiamata: a Tavily arriva o un
    # valore valido o niente (i campi None l'SDK li toglie dal payload).
    topic_value = _clean_enum(topic, _ALLOWED_TOPICS)
    time_range_value = _clean_enum(time_range, _ALLOWED_TIME_RANGES)
    limit = clamp_max_results(max_results)

    try:
        # Client iniettato dai test, altrimenti quello con la chiave del `.env`.
        tavily = client if client is not None else get_tavily_client()
        payload = tavily.search(
            query=cleaned_query,
            topic=topic_value,
            time_range=time_range_value,
            max_results=limit,
            # Esplicito, non `auto_parameters`: un credito a ricerca, prevedibile.
            search_depth=SEARCH_DEPTH,
            # La sintesi la fa Gemini nel loop: la risposta di Tavily non serve.
            include_answer=False,
            # Pagine intere: contesto enorme per una frase parlata, no grazie.
            include_raw_content=False,
            timeout=SEARCH_TIMEOUT,
        )
    except WebSearchError as exc:
        # Chiave assente: `get_tavily_client` parla già italiano.
        return _spoken_error(exc)
    except InvalidAPIKeyError:
        # HTTP 401: la chiave c'è ma Tavily la rifiuta (revocata, typo nel `.env`).
        return MSG_INVALID_KEY
    except MissingAPIKeyError:
        # L'SDK non ha trovato né `api_key` né la variabile d'ambiente.
        return MSG_MISSING_KEY
    except (TavilyKeylessLimitError, KeylessUnsupportedEndpointError):
        # Client senza chiave (modalità keyless): stesso rimedio, imposta la chiave.
        # Va prima di UsageLimitExceededError, di cui il keyless è sottoclasse.
        return MSG_MISSING_KEY
    except UsageLimitExceededError:
        # HTTP 429: crediti del piano finiti o rate limit.
        return MSG_QUOTA
    except ForbiddenError:
        # HTTP 403/432/433: endpoint non incluso nel piano dell'account.
        return MSG_FORBIDDEN
    except BadRequestError:
        # HTTP 400: parametri rifiutati; riformulare è l'unica mossa dell'utente.
        return MSG_BAD_REQUEST
    except TavilyTimeoutError:
        # Timeout dell'SDK (non quello builtin): rete lenta o Tavily sotto carico.
        return MSG_TIMEOUT
    except requests.exceptions.RequestException:
        # DNS, TLS, connessione giù, 5xx via `raise_for_status`: un solo parlato.
        return MSG_UNREACHABLE

    results = _results_from_payload(payload)
    # Stato per lo stdout dell'agente: si aggiorna solo dopo una ricerca riuscita,
    # anche se a zero risultati (l'elenco a schermo deve seguire l'ultimo turno).
    _LAST_RESULTS.clear()
    _LAST_RESULTS.extend(results)
    return format_search_result(cleaned_query, results)
