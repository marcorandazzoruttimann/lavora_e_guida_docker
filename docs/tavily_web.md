# Ricerca web con Tavily (`--agent web`)

Collega l’assistente vocale a [Tavily](https://tavily.com), un motore di ricerca pensato per gli LLM: restituisce titolo, URL e uno snippet già estratto, non una pagina HTML da ripulire.

L’agente `web` è uno **specialista isolato**, come Gmail: non tocca i file del Desktop né l’indice RAG, e il master non lo importa. Gira su **Gemini** con function calling nativo (`functionDeclarations` + `functionCall` / `parts[].text`). Qwen 2.5 3B resta extra di studio: `--llm ollama` sul loop vocale è fail-fast parlante.

Il pattern è quello Gmail, non il grounding nativo di Google: Gemini emette una `functionCall`, Python esegue la REST Tavily, l’esito rientra nel loop come `functionResponse` e **la sintesi parlata la scrive Gemini**. Per questo chiediamo a Tavily gli snippet grezzi (`include_answer=False`) e non la sua risposta già confezionata: il nostro LLM ce l’abbiamo già.

## 1. Chiave API (una tantum)

1. Registrarsi su [app.tavily.com](https://app.tavily.com) (il piano gratuito include un pacchetto mensile di crediti, sufficiente per lo studio).
2. Copiare la chiave dalla dashboard: ha il prefisso `tvly-`.
3. Incollarla in `.env` (già gitignored):

```bash
TAVILY_API_KEY=tvly-...
```

Non serve nessun consenso OAuth né alcun file token: a differenza di Gmail qui c’è solo una chiave statica. Non committare la chiave e non metterla in `.env.example`.

La chiave è **obbligatoria solo per `--agent web`**: gli altri agenti partono anche senza. Il campo `tavily_api_key` in `Settings` resta opzionale proprio per questo, e il fail-fast vive nella CLI.

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

### Fonti: dominio a voce, URL a schermo

La stringa che torna a Gemini cita le fonti come **dominio** (`ansa.it`), mai l’indirizzo completo: `SPOKEN_REPLY_RULE` vieta di spellare URL a voce, e anche gli URL dentro i titoli e gli snippet vengono sostituiti dal loro dominio.

Gli indirizzi interi servono però a chi guarda lo schermo, per aprire la pagina: finiscono sullo stdout col prefisso `[WEB]`, numerati nello stesso ordine della frase parlata.

```
[WEB] OK: 3 risultati per meteo Roma domani. 1. Previsioni Roma, fonte ilmeteo.it. ...
  1. https://www.ilmeteo.it/meteo/Roma
  2. https://www.3bmeteo.com/meteo/roma
```

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
- timeout a 30 secondi invece dei 60 di default dell’SDK: in un loop vocale un minuto di silenzio sembra un blocco.

Una query vuota viene fermata in Python **prima** della rete: query malposte non consumano crediti.

## 5. Test senza rete

I test iniettano un client finto (`web_search(..., client=...)`) oppure monkeypatchano `get_tavily_client`, quindi la suite non fa mai una richiesta reale e non consuma crediti.

```bash
.venv/bin/pytest -q tests/test_web_agent.py
```
