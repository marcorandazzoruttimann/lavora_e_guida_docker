# Flowchart: router master e specialisti nested

Indice: [flowchart.md](flowchart.md). Codice: [`master/agent.py`](../src/lavora_e_guida/master/agent.py). Loop interno: [avvio e loop](flowchart_avvio_e_loop.md).

Il master è l’unico possessore di STT/TTS quando `--agent master`. Gemini del master vede **solo** `ask_fs`, `ask_gmail`, `ask_web`. I cataloghi di dominio restano negli specialisti.

## 1. Dispatch `ask_*`

```mermaid
flowchart TB
  CALL["functionCall del master"]
  CALL --> WL{"tool in ask_fs ask_gmail ask_web?"}
  WL -->|no| E1["ERRORE: tool sconosciuto"]
  WL -->|sì| LOG["print MASTER ask_*"]
  LOG --> Q{"query non vuota?"}
  Q -->|no| E2["ERRORE: manca la richiesta"]
  Q -->|sì| GATE{"quale tool?"}

  GATE -->|ask_web e Tavily assente| E3["ERRORE chiave Tavily<br/>niente nested Gemini"]
  GATE -->|ask_gmail e token assente| E4["ERRORE Gmail non collegata<br/>niente nested Gemini"]
  GATE -->|ask_fs sempre<br/>oppure gate ok| LLM{"get_active_llm<br/>o llm iniettato nei test"}
  LLM -->|None| E5["ERRORE: client LLM assente"]
  LLM -->|ok| DEL["_delegate_to_specialist"]
```

Gate lazy: il processo master è già partito (serve il Desktop per `ask_fs`). L’errore è parlante, non `SystemExit`.

## 2. Nested: storia isolata, niente TTS

Ogni specialista ha una lista di messaggi propria, con **il suo** system prompt. Persiste per la sessione: «leggi la seconda» dopo «ultime email» vede la lista Gmail, non il prompt FS.

```mermaid
flowchart TB
  DEL["_delegate_to_specialist name query llm"]
  DEL --> HIST["_history_for<br/>prima volta: solo system dello spec"]
  HIST --> APP["append user = query del master<br/>non lo STT grezzo"]
  APP --> NEST["run_specialist_task<br/>report_latency False"]
  NEST --> KIND{"kind"}
  KIND -->|hitl| RET["return hitl_spoken<br/>conferma bozza"]
  KIND -->|text error exhausted| RET2["return result.text"]
  RET --> FR["functionResponse del master"]
  RET2 --> FR
```

Stdout nested resta: `[FS]`, `[RAG]`, `[GMAIL]`, `[WEB]`. La latenza chat del nested non si ristampa: quella del round master è già sul loop esterno.

Dopo il `functionResponse`, Gemini del master formula la reply **con i fatti**, non «ho smistato». Se l’esito è `ERRORE:`, deve dirlo e fermarsi.

## 3. HITL Gmail sul loop esterno

Lo specialista nested ferma il **suo** Gemini (`kind=hitl`). Il master deve fermare anche il proprio, altrimenti riformula «ho chiesto a Gmail».

```mermaid
flowchart TB
  AFTER["master_hitl_after_tool"]
  AFTER --> T{"tool == ask_gmail?"}
  T -->|no| NONE["None: Gemini master parla"]
  T -->|sì| SES{"draft session<br/>awaiting_confirm?"}
  SES -->|no| NONE
  SES -->|sì| SPOKEN["return result o<br/>spoken_draft_confirm"]
  SPOKEN --> LOOP["loop: tts.speak, niente Gemini"]
  LOOP --> NEXT["prossimo stt.listen"]
  NEXT --> ON["gmail_hitl_on_utterance"]
```

Sì / no: [flowchart Gmail](flowchart_gmail.md).

## 4. Flusso composto: meteo + email

Enunciato: «Cerca il meteo di Roma e mandalo a mario@x.it». Nessun `create_text_file`.

```mermaid
sequenceDiagram
  participant U as Utente
  participant M as Gemini master
  participant W as Specialista web
  participant T as Tavily
  participant G as Specialista Gmail
  participant H as HITL loop

  U->>M: enunciato STT
  M->>W: ask_web query meteo Roma
  W->>T: web_search
  T-->>W: OK con snippet e URL
  W-->>M: functionResponse fatti
  Note over M: secondo round stesso enunciato
  M->>G: ask_gmail query destinatario + testo meteo
  G-->>M: kind hitl, conferma Python
  M-->>H: hitl_after_tool
  H->>U: Di sì per inviare...
  U->>H: sì
  H->>G: send_email REST
  G-->>U: inviato
```

## 5. Cosa vede ogni Gemini

```mermaid
flowchart TB
  subgraph MASTER["Catalogo master"]
    A1["ask_fs query"]
    A2["ask_gmail query"]
    A3["ask_web query"]
  end

  subgraph FS["Catalogo fs"]
    F1["create_text_file name content"]
    F2["append_note name content"]
    F3["read_file name"]
    F4["find_file query"]
  end

  subgraph GMAIL["Catalogo gmail"]
    G1["list_emails query"]
    G2["read_email name"]
    G3["save_attachments name"]
    G4["draft_email to subject body"]
    G5["reply_email / reply_all_email"]
    G6["send_email senza args"]
  end

  subgraph WEB["Catalogo web"]
    W1["web_search query topic time_range max_results"]
  end
```

Il master **non** importa `web_search` né `create_text_file` nelle declaration. Se Gemini inventa un nome di dominio, il dispatch risponde `ERRORE: tool sconosciuto`.

## 6. Specialista web in due parole

Niente HITL: la ricerca è read-only. `include_answer=False`: la sintesi parlata la scrive Gemini, non Tavily. Fonti a voce = dominio (`ansa.it`); URL completi solo in appendice, da leggere se l’utente li chiede. Dettaglio operativo: [tavily_web.md](tavily_web.md).

```mermaid
flowchart TB
  WS["web_search"]
  WS --> Q{"query vuota?"}
  Q -->|sì| E0["ERRORE: dimmi cosa cercare<br/>niente rete, niente credito"]
  Q -->|no| REST["Tavily basic, max 8, timeout 60s"]
  REST -->|OK risultati| OK["OK: n risultati... dominio"]
  REST -->|zero hit| OK0["OK: nessun risultato"]
  REST -->|401/429/403/400/rete| ER["ERRORE parlante"]
```

## 7. Specialista fs in due parole

Path risolto in Python: Gemini passa solo `name` o `query`. `find_file` usa Chroma. Create/append dopo la scrittura chiamano `upsert_indexed_file`. Dettaglio: [file e RAG](flowchart_fs_rag.md).
