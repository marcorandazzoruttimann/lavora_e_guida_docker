# Ricerca web con Tavily (`--agent web` e `ask_web` del master)

Collega l’assistente vocale a [Tavily](https://tavily.com), un motore di ricerca pensato per gli LLM: restituisce titolo, URL e uno snippet già estratto, non una pagina HTML da ripulire.

| Agente | Comando | Ruolo rispetto a Tavily |
| --- | --- | --- |
| master (default) | `lavora-e-guida` | Router: `ask_web` smista qui. La chiave si chiede al primo `ask_web`, non all’avvio |
| fs | `lavora-e-guida --agent fs` | File e RAG sul Desktop: non cerca online |
| gmail | `lavora-e-guida --agent gmail` | Mailbox: non cerca online |
| web | `lavora-e-guida --agent web` | Specialista isolato: un solo tool `web_search`, fail-fast se manca `TAVILY_API_KEY` |

L’agente `web` è uno **specialista isolato**, come Gmail e FS: non tocca i file del Desktop né l’indice RAG, e il catalogo Gemini del master non importa `web_search` (solo `ask_web`). Gira su **Gemini** con function calling nativo (`functionDeclarations` + `functionCall` / `parts[].text`). Qwen 2.5 3B resta extra di studio: `--llm ollama` sul loop vocale è fail-fast parlante.

Flusso composto dal router: «Cerca il meteo di Roma e mandalo a mario@x.it» → round 1 `ask_web` (questo specialista) → round 2 `ask_gmail` con destinatario e testo trovato. Nessun `create_text_file` se l’utente non ha chiesto un file.

Il pattern è quello Gmail, non il grounding nativo di Google: Gemini emette una `functionCall`, Python esegue la REST Tavily, l’esito rientra nel loop come `functionResponse` e **la sintesi parlata la scrive Gemini**. Per questo chiediamo a Tavily gli snippet grezzi (`include_answer=False`) e non la sua risposta già confezionata: il nostro LLM ce l’abbiamo già.

## 1. Chiave API (una tantum)

1. Registrarsi su [app.tavily.com](https://app.tavily.com) (il piano gratuito include un pacchetto mensile di crediti, sufficiente per lo studio).
2. Copiare la chiave dalla dashboard: ha il prefisso `tvly-`.
3. Incollarla in `.env` (già gitignored):

```bash
TAVILY_API_KEY=tvly-...
```

Non serve nessun consenso OAuth né alcun file token: a differenza di Gmail qui c’è solo una chiave statica. Non committare la chiave e non metterla in `.env.example`.

La chiave è **obbligatoria all’avvio solo per `--agent web`**: gli altri specialisti (`fs`, `gmail`) partono anche senza. Il router (`--agent master`) parte comunque (serve il Desktop per `ask_fs`); al primo `ask_web` senza chiave lo specialista risponde con un `ERRORE:` parlante, senza nested Gemini. Il campo `tavily_api_key` in `Settings` resta opzionale proprio per questo, e il fail-fast CLI vive solo sul ramo `--agent web`.

## 2. Avvio

Dalla root del repo, venv attivo:

```bash
source .venv/bin/activate
lavora-e-guida --agent web
```

Con `AUDIO_DRIVER=mock` si digita la frase e si legge la risposta a schermo; con `http` parla il bridge audio sull’host Windows. Si esce dicendo (o scrivendo) `esci`.

Se `TAVILY_API_KEY` manca o è vuota, il loop **non parte**: messaggio su stderr e uscita con codice 1, prima ancora di aprire il microfono.

```
TAVILY_API_KEY assente. Prendi una chiave su app.tavily.com, mettila nel .env (TAVILY_API_KEY=tvly-...) e riprova.
```

All’avvio non si fa nessun ping di rete: Tavily non ha un endpoint di verifica gratuito, e un ping a pagamento brucerebbe un credito a ogni avvio. Una chiave presente ma sbagliata si scopre quindi alla prima ricerca, con un `ERRORE:` parlato.

## 3. Il tool `web_search`

Un solo tool, un solo tool eseguito per enunciato.

| Parametro | Tipo | Note |
| --- | --- | --- |
| `query` | string (obbligatorio) | Cosa cercare, in lingua naturale. Stessa chiave di `find_file` e `list_emails`. |
| `topic` | enum `general` \| `news` | `news` per cronaca e attualità. Se omesso vale il default dell’API. |
| `time_range` | enum `day` \| `week` \| `month` \| `year` | Finestra temporale dei risultati. |
| `max_results` | integer | Default 5, clampato tra 1 e 8. Di norma si omette. |

Gli enum sono dichiarati nello schema OpenAPI, così Gemini smette di inventare `topic=finance` o `time_range=oggi`. Un valore fuori whitelist comunque **non** fa fallire il tool: Python lo ignora e lascia decidere il default dell’API, perché una ricerca generica è una risposta più sensata di un errore in faccia all’utente.

Esempi vocali:

- «cerca chi ha vinto il campionato» → `web_search` con `query` soltanto;
- «che notizie ci sono oggi» → `topic=news`, `time_range=day`;
- «novità sull’intelligenza artificiale questa settimana» → `topic=news`, `time_range=week`.

### Fonti: dominio a voce, URL su richiesta

L’esito del tool ha **due parti**, separate da un a capo.

La prima riga è quella che Gemini riassume a voce e cita le fonti come **dominio** (`ansa.it`), mai l’indirizzo completo: `SPOKEN_REPLY_RULE` vieta di spellare URL a voce, e anche i link che compaiono dentro titoli e snippet vengono sostituiti dal loro dominio.

La seconda riga è un’appendice etichettata (`URL_SECTION_INTRO`) con gli indirizzi interi, numerati come l’elenco parlato. Serve solo a una cosa: se l’utente chiede espressamente i link, il modello deve avere quelli veri. Il system prompt lo autorizza a darli su richiesta e gli vieta di ricostruirli a memoria; senza l’appendice non avrebbe nessun URL in mano e li inventerebbe.

```
OK: 2 risultati per meteo Roma. 1. Previsioni Roma, fonte ilmeteo.it. Domani sereno. 2. …
Indirizzi completi delle fonti, nello stesso ordine: dalli all'utente solo se li chiede espressamente, … 1. https://www.ilmeteo.it/meteo/Roma 2. …
```

A schermo l’appendice non si ripete: lo stdout `[WEB]` stampa solo la riga parlata e sotto gli indirizzi uno per riga, che si leggono meglio.

```
[WEB] OK: 2 risultati per meteo Roma. 1. Previsioni Roma, fonte ilmeteo.it. Domani sereno. 2. …
  1. https://www.ilmeteo.it/meteo/Roma
  2. https://www.3bmeteo.com/meteo/roma
```

Attenzione al canale: quando Gemini legge un indirizzo su richiesta, quel testo passa comunque da edge-tts. Con `AUDIO_DRIVER=mock` si legge a schermo ed è comodo; a voce un URL lungo resta faticoso da ascoltare, ed è il motivo per cui la deroga vale solo su richiesta esplicita.

### Esiti

Come per gli altri specialisti, il tool non solleva mai eccezioni verso il loop: restituisce testo già parlabile.

| Situazione | Risposta del tool |
| --- | --- |
| Risultati trovati | `OK: 3 risultati per meteo Roma. 1. …` |
| Nessun risultato | `OK: nessun risultato trovato per {query}.` (esito, non errore) |
| Query vuota | `ERRORE: dimmi cosa devo cercare sul web` — nessuna chiamata, nessun credito |
| Chiave assente / non valida | `ERRORE: chiave Tavily assente…` / `ERRORE: chiave Tavily non valida…` |
| Crediti finiti (HTTP 429) | `ERRORE: crediti Tavily esauriti, riprova più tardi` |
| Endpoint fuori piano (403) | `ERRORE: ricerca web non consentita dal piano Tavily` |
| Richiesta rifiutata (400) | `ERRORE: Tavily ha rifiutato la ricerca, prova a riformularla` |
| Timeout o rete giù | `ERRORE: la ricerca web ci ha messo troppo…` / `ERRORE: Tavily non raggiungibile…` |

Su `ERRORE:` il prompt impone a Gemini di dire cosa è andato storto e fermarsi: mai inventare risultati o contenuti di pagine che non ha letto.

## 4. Crediti e scelte di costo

Tavily fattura a **crediti**, uno per ricerca `basic` e due per una `advanced`. Il progetto tiene fissa la profondità:

- `search_depth="basic"` esplicito, così una ricerca costa sempre e solo un credito;
- **niente** `auto_parameters`: sarebbe comodo, ma può promuovere da sé la ricerca ad `advanced` e raddoppiare il costo senza che si veda dal codice;
- `include_raw_content=False`: le pagine intere gonfierebbero il contesto mandato a Gemini per produrre poi tre frasi parlate;
- tetto di 8 risultati e snippet troncati a 400 caratteri, per lo stesso motivo;
- timeout a 60 secondi, come il default dell’SDK. Era stato abbassato a 30 perché in un loop vocale mezzo minuto di silenzio sembra già un blocco, ma è un timeout di orologio sulla socket: sotto debugger, o con rete lenta, scadeva su ricerche che Tavily aveva già servito. Il credito in quel caso è speso lo stesso — la richiesta era arrivata, siamo noi ad aver smesso di aspettare — e per giunta il messaggio di errore spingeva il modello a cercare di nuovo.

Attenzione a una conseguenza del punto precedente: **una ricerca in errore costa comunque**, ma `[WEB]` stampa solo gli esiti `OK:`. Per contare i crediti davvero spesi vale la dashboard di app.tavily.com, non le righe a schermo.

Nota su cosa il codice **non** limita: Gemini può chiamare `web_search` più volte nello stesso enunciato, raffinando la query dopo aver letto i primi risultati. Il loop glielo concede fino a `_MAX_TOOL_ROUNDS` (quattro giri), e la guardia anti-ripetizione di `run_chat_loop` blocca solo la stessa query ripetuta di fila. Un singolo enunciato può quindi costare più di un credito.

Una query vuota viene fermata in Python **prima** della rete: query malposte non consumano crediti.

## 5. Test senza rete

I test iniettano un client finto (`web_search(..., client=...)`) oppure monkeypatchano `get_tavily_client`, quindi la suite non fa mai una richiesta reale e non consuma crediti.

```bash
.venv/bin/pytest -q tests/test_web_agent.py
```
