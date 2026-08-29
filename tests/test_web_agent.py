"""Tool `web_search` e spec vocale web: client Tavily finto, zero rete.

Nessun test qui deve aprire una socket verso api.tavily.com: ogni caso passa un
client finto a `web_search(client=...)` oppure monkeypatcha
`lavora_e_guida.web.search.get_tavily_client`, che è il solo punto in cui il
modulo costruisce il `TavilyClient` vero. Stesso spirito del `client=`
iniettabile dei tool Gmail: se un giorno qualcuno tolesse l'iniezione, questi
test tenterebbero una richiesta reale e si vedrebbe subito.

Cosa si sorveglia: il contratto parlato (`OK:` / `ERRORE:` in italiano, elenco
con le fonti come dominio e mai un URL da spellare a voce), l'appendice con gli
indirizzi interi che il modello può dare solo su richiesta esplicita, la
traduzione delle eccezioni dell'SDK in frasi TTS e i parametri effettivamente
mandati a Tavily (profondità `basic` = un credito, `include_answer=False`
perché la sintesi la fa Gemini).

Nota sulle asserzioni «niente URL»: valgono sulla **parte parlata** dell'esito,
cioè la prima riga. Dalla seconda in poi c'è l'appendice `URL_SECTION_INTRO`,
che gli indirizzi deve contenerli: senza, Gemini non avrebbe link veri da dare
e li inventerebbe. `_spoken_part` isola la riga giusta in un punto solo.
"""

from __future__ import annotations

from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
import requests
from tavily.errors import (
    BadRequestError,
    ForbiddenError,
    InvalidAPIKeyError,
    KeylessUnsupportedEndpointError,
    MissingAPIKeyError,
    TavilyKeylessLimitError,
    UsageLimitExceededError,
)
from tavily.errors import TimeoutError as TavilyTimeoutError

from lavora_e_guida.agent import run_chat_loop
from lavora_e_guida.audio.mock import MockSTT, MockTTS
from lavora_e_guida.config import Settings
from lavora_e_guida.llm.turn import FunctionCall, LlmTurn
from lavora_e_guida.llm.usage import TokenUsage
from lavora_e_guida.web import search as search_mod
from lavora_e_guida.web.agent import (
    WEB_GEMINI_TOOLS,
    WEB_LOOP_SPEC,
    WEB_TOOL_DECLARATIONS,
    WEB_TOOL_MAP,
    _print_web_tool_result,
    dispatch_web_tool,
)
from lavora_e_guida.web.search import (
    DEFAULT_MAX_RESULTS,
    MAX_MAX_RESULTS,
    MAX_SNIPPET_CHARS,
    MSG_BAD_REQUEST,
    MSG_EMPTY_QUERY,
    MSG_FORBIDDEN,
    MSG_INVALID_KEY,
    MSG_MISSING_KEY,
    MSG_QUOTA,
    MSG_TIMEOUT,
    MSG_UNREACHABLE,
    SEARCH_DEPTH,
    URL_SECTION_INTRO,
    WebSearchError,
    clamp_max_results,
    get_last_web_results,
    reset_last_web_results,
    reset_tavily_client,
    web_search,
)


class _FakeTavily:
    """Sosia di `TavilyClient`: registra i kwargs e ritorna payload o eccezione.

    Basta il metodo `search`: `web_search` non usa nient'altro del client. I
    kwargs finiscono in `self.calls` perché parte del contratto (search_depth,
    include_answer, max_results) si verifica solo guardando cosa parte.
    """

    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> None:
        # Default: risposta ben formata ma vuota, il caso «nessun risultato».
        self.payload: dict[str, Any] = payload if payload is not None else {"results": []}
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def search(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        # Simula il fallimento dell'SDK: web_search deve tradurlo in italiano.
        if self.error is not None:
            raise self.error
        return self.payload


def _row(title: str, url: str, content: str) -> dict[str, Any]:
    """Una riga di `payload["results"]` come la manda Tavily (chiavi minime)."""
    return {"title": title, "url": url, "content": content}


def _boom_client() -> Any:
    """Client vietato: se qualcuno lo chiama, il test stava per andare in rete."""
    raise AssertionError("get_tavily_client non deve partire in questo caso")


def _spoken_part(result: str) -> str:
    """Prima riga dell'esito: quella che Gemini riassume a voce, senza indirizzi.

    L'appendice con gli URL vive dalla seconda riga in poi. Tenere lo split qui
    evita che ogni test si ricordi da sé dov'è il confine.
    """
    return result.split("\n", 1)[0]


# Riferimento alla fabbrica memoizzata vera, catturato all'import: la fixture
# sostituisce l'attributo di modulo, quindi in teardown `reset_tavily_client`
# non troverebbe più la `cache_clear` della lru_cache.
_REAL_GET_TAVILY_CLIENT = search_mod.get_tavily_client


@pytest.fixture(autouse=True)
def _isolated_web_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stato di modulo pulito: ultima ricerca svuotata e client memoizzato buttato.

    `_LAST_RESULTS` e la `lru_cache` di `get_tavily_client` sono globali di
    processo (il loop vocale è single-user): senza reset un test erediterebbe i
    risultati del precedente. Di default il client reale è sabotato: solo i test
    che lo monkeypatchano esplicitamente possono "chiamare" Tavily.
    """
    reset_last_web_results()
    _REAL_GET_TAVILY_CLIENT.cache_clear()
    monkeypatch.setattr(search_mod, "get_tavily_client", _boom_client)
    yield
    reset_last_web_results()
    _REAL_GET_TAVILY_CLIENT.cache_clear()


def test_dispatch_unknown_tool_is_spoken_error() -> None:
    """Whitelist web: i tool FS/Gmail non partono e non toccano la rete."""
    result = dispatch_web_tool("create_text_file", {"name": "spesa", "content": "latte"})
    assert result.startswith("ERRORE:")
    assert "tool sconosciuto" in result
    # L'esito elenca il consentito: Gemini al turno dopo sa cosa può chiedere.
    assert "web_search" in result
    # Nessun tool degli altri specialisti deve comparire come ammesso.
    assert "list_emails" not in result
    assert "append_note" not in result


def test_dispatch_missing_or_empty_query_never_calls_tavily() -> None:
    """`query` assente, vuota o solo spazi: errore parlante, zero crediti spesi."""
    # args senza la chiave query: il modello ha chiamato il tool a vuoto.
    assert dispatch_web_tool("web_search", {}) == MSG_EMPTY_QUERY
    assert dispatch_web_tool("web_search", {"query": ""}) == MSG_EMPTY_QUERY
    # Solo spazi dallo STT: il collapse in web_search la riduce a stringa vuota.
    assert dispatch_web_tool("web_search", {"query": "   "}) == MSG_EMPTY_QUERY
    # args non-dict (Gemini che manda una stringa): normalizzato, non esplode.
    assert dispatch_web_tool("web_search", None) == MSG_EMPTY_QUERY  # type: ignore[arg-type]
    # Il messaggio è una frase da leggere, non un errore tecnico inglese.
    assert MSG_EMPTY_QUERY.startswith("ERRORE:")
    assert "cosa devo cercare" in MSG_EMPTY_QUERY


def test_web_search_formats_numbered_results_with_domains_only() -> None:
    """Esito numerato: titolo, fonte come dominio, snippet; mai un URL a voce."""
    client = _FakeTavily(
        {
            "results": [
                _row(
                    "Meteo Roma domani",
                    "https://www.ansa.it/meteo/roma",
                    "Domani sereno con massime di 30 gradi.",
                ),
                _row(
                    "Previsioni Lazio",
                    "https://corriere.it/meteo/lazio?utm_source=x",
                    "Nel weekend arriva la pioggia.",
                ),
            ]
        }
    )
    result = web_search("meteo Roma", client=client)
    spoken = _spoken_part(result)

    assert spoken.startswith("OK: 2 risultati per meteo Roma.")
    assert "1. Meteo Roma domani, fonte ansa.it. Domani sereno con massime di 30 gradi." in spoken
    assert "2. Previsioni Lazio, fonte corriere.it. Nel weekend arriva la pioggia." in spoken
    # Vincolo TTS: niente indirizzi da spellare, niente `www.` da pronunciare.
    assert "https://" not in spoken
    assert "www." not in spoken
    assert "utm_source" not in spoken
    # Niente markdown nell'esito: `SPOKEN_REPLY_RULE` vale anche per i tool.
    assert "**" not in spoken
    assert "- " not in spoken


def test_web_search_appends_full_urls_for_the_model() -> None:
    """Appendice con gli indirizzi interi: il modello li ha davvero, non li inventa.

    È il pezzo che rende possibile «dammi i link»: senza, il prompt potrebbe
    anche autorizzare Gemini a darli, ma lui non ne avrebbe nessuno in mano.
    """
    client = _FakeTavily(
        {
            "results": [
                _row("Uno", "https://www.ansa.it/meteo/roma", "Sereno."),
                _row("Due", "https://corriere.it/meteo/lazio", "Pioggia."),
            ]
        }
    )
    result = web_search("meteo Roma", client=client)

    # Due righe: prima il parlato, poi l'appendice. Mai in ordine inverso.
    spoken, appendix = result.split("\n", 1)
    assert spoken.startswith("OK: 2 risultati per meteo Roma.")
    # L'etichetta dice a Gemini quando può usarli: è un vincolo, non decorazione.
    assert appendix.startswith(URL_SECTION_INTRO)
    assert "se li chiede espressamente" in URL_SECTION_INTRO
    # Indirizzi interi, con lo schema e il `www.` che a voce si tolgono.
    assert "1. https://www.ansa.it/meteo/roma" in appendix
    assert "2. https://corriere.it/meteo/lazio" in appendix
    # Numerazione allineata all'elenco parlato: «il secondo link» = «la seconda fonte».
    assert appendix.index("1. https://www.ansa.it") < appendix.index("2. https://corriere.it")


def test_web_search_omits_url_section_when_no_result_has_one() -> None:
    """Nessun indirizzo nel payload: nessuna appendice, l'esito resta una riga."""
    client = _FakeTavily(
        {"results": [{"title": "Senza fonte", "content": "Testo."}]}
    )
    result = web_search("x", client=client)
    assert "\n" not in result
    assert URL_SECTION_INTRO not in result


def test_web_search_url_section_skips_results_without_address() -> None:
    """Risultato senza URL: salta il suo numero, niente voce vuota in appendice."""
    client = _FakeTavily(
        {
            "results": [
                {"title": "Senza indirizzo", "content": "Testo."},
                _row("Con indirizzo", "https://ansa.it/ok", "Altro testo."),
            ]
        }
    )
    _, appendix = web_search("x", client=client).split("\n", 1)
    # Il numero è quello del risultato (il secondo), non un contatore a parte.
    assert "2. https://ansa.it/ok" in appendix
    assert "1. http" not in appendix


def test_web_search_singular_noun_for_one_result() -> None:
    """Un risultato solo: «1 risultato», non la stonatura «1 risultati»."""
    client = _FakeTavily({"results": [_row("Titolo", "https://ansa.it/x", "Testo.")]})
    result = web_search("chi ha vinto", client=client)
    assert result.startswith("OK: 1 risultato per chi ha vinto.")
    assert "1 risultati" not in result


def test_web_search_no_results_is_ok_not_error() -> None:
    """Lista vuota: esito `OK:` dedicato, non `ERRORE:` (la ricerca è riuscita)."""
    client = _FakeTavily({"results": []})
    result = web_search("notizie da marte", client=client)
    assert result == "OK: nessun risultato trovato per notizie da marte."
    assert not result.startswith("ERRORE:")
    # Nessun risultato in memoria: lo stdout dell'agente non deve stampare righe.
    assert get_last_web_results() == ()


def test_web_search_sends_basic_depth_and_no_answer() -> None:
    """Kwargs verso Tavily: `basic` (un credito), niente answer né pagina intera."""
    client = _FakeTavily({"results": [_row("T", "https://ansa.it/x", "C")]})
    web_search("prezzo del gas", client=client)

    assert len(client.calls) == 1
    sent = client.calls[0]
    # La query arriva già normalizzata negli spazi, non quella grezza dello STT.
    assert sent["query"] == "prezzo del gas"
    # Profondità esplicita: `auto_parameters` potrebbe promuovere ad advanced (2 crediti).
    assert sent["search_depth"] == SEARCH_DEPTH == "basic"
    # La sintesi la fa Gemini nel loop: la risposta confezionata di Tavily non serve.
    assert sent["include_answer"] is False
    assert sent["include_raw_content"] is False
    # Default vocale: cinque risultati, senza che il modello debba chiederlo.
    assert sent["max_results"] == DEFAULT_MAX_RESULTS
    # Enum omessi dall'utterance: None, così l'SDK non li mette nel payload.
    assert sent["topic"] is None
    assert sent["time_range"] is None


def test_web_search_collapses_stt_spaces_in_query() -> None:
    """«  meteo   Roma » dallo STT è la stessa query di «meteo Roma»."""
    client = _FakeTavily({"results": []})
    result = web_search("  meteo   Roma ", client=client)
    assert client.calls[0]["query"] == "meteo Roma"
    # Anche la frase parlata cita la query pulita, non il doppio spazio.
    assert result == "OK: nessun risultato trovato per meteo Roma."


def test_web_search_passes_enum_values_and_ignores_invented_ones() -> None:
    """`news`/`day` passano (anche maiuscoli); un enum inventato si omette."""
    client = _FakeTavily({"results": []})
    web_search("notizie di oggi", topic="News", time_range="DAY", client=client)
    assert client.calls[0]["topic"] == "news"
    assert client.calls[0]["time_range"] == "day"

    # Valore fuori whitelist: non è un ERRORE in faccia all'utente, si degrada
    # alla ricerca generica (una risposta è comunque meglio di un rifiuto).
    web_search("borsa", topic="finance", time_range="oggi", client=client)
    assert client.calls[1]["topic"] is None
    assert client.calls[1]["time_range"] is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, DEFAULT_MAX_RESULTS),
        (3, 3),
        ("3", 3),
        (0, 1),
        (-7, 1),
        (99, MAX_MAX_RESULTS),
        # Il modello a volte scrive il numero a parole: default, non eccezione.
        ("cinque", DEFAULT_MAX_RESULTS),
        # bool è sottoclasse di int: True non deve diventare «un risultato».
        (True, DEFAULT_MAX_RESULTS),
    ],
)
def test_clamp_max_results_default_floor_and_cap(value: object, expected: int) -> None:
    """Default 5, minimo 1, tetto 8: una risposta parlata, non una SERP."""
    assert clamp_max_results(value) == expected


def test_web_search_clamps_max_results_out_of_range() -> None:
    """`max_results` fuori range arriva già clampato a Tavily, non rifiutato."""
    client = _FakeTavily({"results": []})
    web_search("meteo", max_results=99, client=client)
    assert client.calls[0]["max_results"] == MAX_MAX_RESULTS

    web_search("meteo", max_results=0, client=client)
    assert client.calls[1]["max_results"] == 1


def test_web_search_truncates_long_snippet_and_strips_markup() -> None:
    """Snippet lunghi tagliati sotto il tetto; asterischi e URL interni via."""
    long_content = "Lorem ipsum dolor sit amet. " * 60
    client = _FakeTavily(
        {
            "results": [
                _row(
                    "**Titolo in grassetto**",
                    "https://ansa.it/lungo",
                    f"{long_content} vedi https://ansa.it/altro per il resto.",
                )
            ]
        }
    )
    result = web_search("lorem", client=client)
    spoken = _spoken_part(result)

    item = get_last_web_results()[0]
    # Tetto sullo snippet: il contesto mandato a Gemini resta piccolo.
    assert len(item.snippet) <= MAX_SNIPPET_CHARS
    # Markdown e URL non sopravvivono al parlato: la riga è già pronunciabile.
    assert "**" not in spoken
    assert "https://" not in spoken
    assert "Titolo in grassetto" in spoken
    # L'URL citato dentro lo snippet resta un dominio anche in appendice: là
    # sotto vanno solo gli indirizzi delle fonti, non i link interni alle pagine.
    assert "https://ansa.it/altro" not in result


def test_web_search_skips_rows_without_title_and_snippet() -> None:
    """Riga senza titolo né testo: saltata, invece di parlare un «1. , fonte .»."""
    client = _FakeTavily(
        {
            "results": [
                # Solo URL: non c'è niente da leggere a voce.
                {"url": "https://ansa.it/vuoto"},
                # Non-dict in mezzo alla lista: difensivo, non deve far esplodere.
                "spazzatura",
                _row("Titolo buono", "https://ansa.it/ok", "Contenuto vero."),
            ]
        }
    )
    result = web_search("qualcosa", client=client)
    assert result.startswith("OK: 1 risultato per qualcosa.")
    assert "Titolo buono, fonte ansa.it. Contenuto vero." in result
    assert len(get_last_web_results()) == 1


def test_web_search_result_without_url_has_no_dangling_source() -> None:
    """URL assente: niente «fonte» monca nella frase parlata."""
    client = _FakeTavily({"results": [{"title": "Senza fonte", "content": "Testo."}]})
    result = web_search("x", client=client)
    assert "1. Senza fonte. Testo." in result
    assert "fonte ." not in result


def test_web_search_missing_key_speaks_italian(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chiave assente: `WebSearchError` parlante, nessuna richiesta HTTP.

    Qui si lascia agire il vero `get_tavily_client` (senza il sabotaggio della
    fixture) ma con Settings senza chiave: deve alzare prima di costruire il
    client, e `web_search` deve tradurre in una frase italiana.
    """
    # Si rimette la fabbrica vera (memoizzata) al posto del sabotaggio di fixture.
    monkeypatch.setattr(search_mod, "get_tavily_client", _REAL_GET_TAVILY_CLIENT)
    monkeypatch.setattr(
        search_mod,
        "get_settings",
        lambda: Settings(_env_file=None, tavily_api_key=None),
    )
    assert web_search("meteo Roma") == MSG_MISSING_KEY

    # Cambio di chiave: il client memoizzato va buttato, altrimenti il processo
    # continuerebbe a usare quello costruito con la configurazione precedente.
    reset_tavily_client()
    # Anche la riga `TAVILY_API_KEY=` vuota nel .env è chiave assente.
    monkeypatch.setattr(
        search_mod,
        "get_settings",
        lambda: Settings(_env_file=None, tavily_api_key="   "),
    )
    assert web_search("meteo Roma") == MSG_MISSING_KEY
    # Il tipo dell'eccezione resta quello del contratto del tool.
    with pytest.raises(WebSearchError):
        _REAL_GET_TAVILY_CLIENT()


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (InvalidAPIKeyError("Invalid API key provided"), MSG_INVALID_KEY),
        (MissingAPIKeyError(), MSG_MISSING_KEY),
        # Keyless: sottoclasse di UsageLimitExceededError, ma il rimedio è la chiave.
        (TavilyKeylessLimitError("Keyless limit reached"), MSG_MISSING_KEY),
        (KeylessUnsupportedEndpointError("search"), MSG_MISSING_KEY),
        (UsageLimitExceededError("Usage limit exceeded"), MSG_QUOTA),
        (ForbiddenError("Endpoint not included in your plan"), MSG_FORBIDDEN),
        (BadRequestError("Bad request"), MSG_BAD_REQUEST),
        (TavilyTimeoutError(30.0), MSG_TIMEOUT),
        # `requests` è l'HTTP di tavily-python: DNS, TLS e 5xx risalgono da qui.
        (requests.exceptions.ConnectionError("dns failure"), MSG_UNREACHABLE),
        (requests.exceptions.HTTPError("500 Server Error"), MSG_UNREACHABLE),
    ],
)
def test_web_search_translates_sdk_errors_to_spoken_italian(
    error: BaseException,
    expected: str,
) -> None:
    """Ogni eccezione dell'SDK diventa una frase italiana, senza testo inglese."""
    client = _FakeTavily(error=error)
    result = web_search("meteo Roma", client=client)

    assert result == expected
    assert result.startswith("ERRORE:")
    # Niente stacktrace né messaggio inglese dell'SDK dentro il TTS.
    assert "Traceback" not in result
    assert "Error" not in result
    assert str(error) not in result


def test_web_search_error_keeps_previous_results_untouched() -> None:
    """Ricerca fallita: l'elenco dell'ultima riuscita non viene azzerato.

    Lo stdout `[WEB]` deve poter ancora mostrare gli URL della ricerca buona;
    una chiamata andata in errore non ha nuovi indirizzi da stampare.
    """
    ok_client = _FakeTavily({"results": [_row("Buono", "https://ansa.it/ok", "Testo.")]})
    web_search("prima", client=ok_client)
    assert [item.url for item in get_last_web_results()] == ["https://ansa.it/ok"]

    ko_client = _FakeTavily(error=UsageLimitExceededError("no credits"))
    assert web_search("seconda", client=ko_client) == MSG_QUOTA
    assert [item.url for item in get_last_web_results()] == ["https://ansa.it/ok"]


def test_print_web_tool_result_shows_full_urls_on_stdout(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A schermo gli indirizzi interi; a voce restano i soli domini."""
    client = _FakeTavily(
        {
            "results": [
                _row("Uno", "https://www.ansa.it/meteo/roma", "Sereno."),
                # Senza URL: nessuna riga vuota nell'elenco a schermo.
                {"title": "Due", "content": "Testo."},
            ]
        }
    )
    result = web_search("meteo Roma", client=client)
    _print_web_tool_result("web_search", result)

    out = capsys.readouterr().out
    assert "[WEB] OK: 2 risultati per meteo Roma." in out
    assert "1. https://www.ansa.it/meteo/roma" in out
    # Un solo indirizzo stampato: il risultato senza URL non produce «2. ».
    assert "2. http" not in out
    # L'appendice per il modello non finisce a schermo: qui l'elenco numerato
    # sotto è già più leggibile, e stamparli entrambi sarebbe un doppione.
    assert URL_SECTION_INTRO not in out
    assert out.count("https://www.ansa.it/meteo/roma") == 1


def test_print_web_tool_result_stays_silent_on_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Esito `ERRORE:` o tool altrui: niente stdout, lo dice già il TTS."""
    _print_web_tool_result("web_search", MSG_QUOTA)
    _print_web_tool_result("append_note", "OK: nota aggiornata.")
    assert capsys.readouterr().out == ""


def test_web_declarations_are_enum_typed_and_isolated() -> None:
    """Un solo tool `web_search`; enum nello schema, niente placeholder `<...>`."""
    names = {item.name for item in WEB_TOOL_DECLARATIONS}
    assert names == {"web_search"}
    assert set(WEB_TOOL_MAP) == names

    props = WEB_TOOL_DECLARATIONS[0].parameters["properties"]
    # Stessa chiave `query` di find_file e list_emails: dati uguali, nomi uguali.
    assert WEB_TOOL_DECLARATIONS[0].parameters["required"] == ["query"]
    assert props["query"]["type"] == "string"
    assert "<" not in props["query"]["description"]
    # Enum dichiarati: Gemini smette di inventare topic=finance o time_range=oggi.
    assert props["topic"]["enum"] == ["general", "news"]
    assert props["time_range"]["enum"] == ["day", "week", "month", "year"]
    assert props["max_results"]["type"] == "integer"
    # Involucro Gemini, non lo wrapper OpenAI.
    assert WEB_GEMINI_TOOLS[0]["functionDeclarations"][0]["name"] == "web_search"
    assert '"type": "function"' not in str(WEB_GEMINI_TOOLS)


def test_web_loop_spec_has_no_hitl_and_speaks_intro() -> None:
    """Ricerca read-only: nessuna conferma da chiedere, intro con la via d'uscita."""
    assert WEB_LOOP_SPEC.hitl_after_tool is None
    assert WEB_LOOP_SPEC.hitl_on_utterance is None
    assert WEB_LOOP_SPEC.dispatch is dispatch_web_tool
    assert "Di' esci per terminare" in WEB_LOOP_SPEC.intro_text
    prompt = WEB_LOOP_SPEC.system_prompt
    # Contratto nativo: gli schemi stanno nelle declaration, non come JSON nel prompt.
    assert '{"tool": "web_search"' not in prompt
    assert "web_search" in prompt
    # Regole non negoziabili: un tool per enunciato e, di default, niente indirizzi.
    assert "Un solo tool per enunciato" in prompt
    assert "non leggere l'indirizzo completo" in prompt
    # Deroga esplicita: su richiesta dell'utente i link si possono dare, ma solo
    # quelli che il tool ha davvero elencato. Le due frasi devono restare insieme.
    assert "sei autorizzato a darglieli" in prompt
    assert "non inventarne mai uno" in prompt
    # Isolamento: il prompt web non conosce i tool FS né la mailbox.
    assert "create_text_file" not in prompt
    assert "list_emails" not in prompt


class _ScriptedLLM:
    """LLM fake a coda di `LlmTurn`: zero rete, come nei test Gmail."""

    def __init__(self, replies: list[LlmTurn]) -> None:
        self.last_usage = TokenUsage()
        self._replies = list(replies)
        self.calls = 0

    def chat(
        self,
        messages: list[dict[str, Any]],
        *args: object,
        **kwargs: object,
    ) -> LlmTurn:
        self.calls += 1
        # Primo round: in testa alla storia deve stare lo spec web, non il master.
        if self.calls == 1:
            system = messages[0]["content"]
            assert system == WEB_LOOP_SPEC.system_prompt
            assert "web_search" in system
            assert "create_text_file" not in system
        if not self._replies:
            raise AssertionError(f"chiamata LLM extra #{self.calls} su {messages[-1]!r}")
        return self._replies.pop(0)

    def close(self) -> None:
        return None


def test_web_loop_search_then_spoken_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Turno completo: functionCall web_search, dispatch reale, sintesi parlata."""
    client = _FakeTavily(
        {
            "results": [
                _row(
                    "Sereno sul Lazio",
                    "https://www.ansa.it/meteo/roma",
                    "Domani sereno, massime 30 gradi.",
                )
            ]
        }
    )
    # Il dispatch non inietta il client: qui si sostituisce la fabbrica del modulo.
    monkeypatch.setattr(search_mod, "get_tavily_client", lambda: client)

    llm = _ScriptedLLM(
        [
            LlmTurn(
                function_calls=(
                    FunctionCall(
                        name="web_search",
                        args={"query": "meteo Roma domani"},
                    ),
                ),
            ),
            LlmTurn(text="Domani a Roma è sereno con massime di 30 gradi, secondo ansa punto it."),
        ]
    )
    outfile = StringIO()
    code = run_chat_loop(
        MockSTT(
            infile=StringIO("che tempo fa domani a Roma\nesci\n"),
            outfile=outfile,
            prompt="",
        ),
        MockTTS(outfile=outfile, prefix="[TTS] "),
        llm,
        report_latency=False,
        telemetry_db=tmp_path / "telemetry.db",
        spec=WEB_LOOP_SPEC,
    )
    assert code == 0
    spoken = outfile.getvalue()
    # Intro dello specialista web; il master FS e Gmail non devono comparire.
    assert "Agente di ricerca web" in spoken
    assert "Assistente file sul Desktop" not in spoken
    assert "secondo ansa punto it" in spoken
    # Vincolo TTS: nessun indirizzo da spellare nel parlato.
    assert "https://" not in spoken
    assert llm.calls == 2
    # Il tool è partito davvero e ha ricevuto la query dell'utterance.
    assert client.calls[0]["query"] == "meteo Roma domani"
    # Stdout `[WEB]` come `[FS]`/`[RAG]`/`[GMAIL]`, con l'URL per esteso.
    stdout = capsys.readouterr().out
    assert "[WEB] OK: 1 risultato per meteo Roma domani." in stdout
    assert "1. https://www.ansa.it/meteo/roma" in stdout


def test_web_loop_speaks_error_without_second_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Tool in errore: la frase è italiana e lo stdout non stampa nulla."""
    client = _FakeTavily(error=UsageLimitExceededError("Usage limit exceeded"))
    monkeypatch.setattr(search_mod, "get_tavily_client", lambda: client)

    llm = _ScriptedLLM(
        [
            LlmTurn(
                function_calls=(
                    FunctionCall(name="web_search", args={"query": "prezzo del gas"}),
                ),
            ),
            LlmTurn(text="Non riesco a cercare adesso: i crediti della ricerca web sono esauriti."),
        ]
    )
    outfile = StringIO()
    code = run_chat_loop(
        MockSTT(infile=StringIO("prezzo del gas\nesci\n"), outfile=outfile, prompt=""),
        MockTTS(outfile=outfile, prefix="[TTS] "),
        llm,
        report_latency=False,
        telemetry_db=tmp_path / "telemetry.db",
        spec=WEB_LOOP_SPEC,
    )
    assert code == 0
    spoken = outfile.getvalue()
    assert "crediti della ricerca web sono esauriti" in spoken
    assert llm.calls == 2
    # Una sola chiamata a Tavily: l'errore non deve innescare un retry a crediti.
    assert len(client.calls) == 1
    # `[WEB]` stampa solo gli esiti OK: l'errore lo dice il TTS.
    assert "[WEB]" not in capsys.readouterr().out
