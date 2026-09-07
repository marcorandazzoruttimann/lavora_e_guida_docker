# Flowchart: avvio CLI e loop vocale

Indice: [flowchart.md](flowchart.md). Codice: [`main.py`](../src/lavora_e_guida/main.py), [`agent.py`](../src/lavora_e_guida/agent.py), [`llm/cloud.py`](../src/lavora_e_guida/llm/cloud.py).

## 1. `main()`: dal comando al loop

`--llm ollama` non costruisce Qwen: parla, esce con codice 1. Gemini è l’unico provider del loop.

```mermaid
flowchart TB
  START["lavora-e-guida<br/>o python -m lavora_e_guida"]
  DOTENV["load_dotenv"]
  PARSE["argparse: --agent --llm --model"]
  SETTINGS["get_settings<br/>AUDIO_DRIVER, path, chiavi"]

  START --> DOTENV --> PARSE --> SETTINGS

  SETTINGS --> LLMQ{"--llm ollama?"}
  LLMQ -->|sì| OLL["create_audio_pair<br/>tts.speak fail-fast<br/>Il loop vocale usa Gemini"]
  OLL --> EXIT1["SystemExit 1"]

  LLMQ -->|no, gemini| BUILD["build_llm gemini<br/>GeminiChat"]
  BUILD --> PING["_startup_gemini"]
  PING --> KEY{"GEMINI_API_KEY<br/>presente?"}
  KEY -->|no| STDERR1["stderr: chiave assente"] --> EXIT1
  KEY -->|sì| PING2{"llm.ping<br/>GET /v1beta/models"}
  PING2 -->|fail| STDERR2["stderr: Gemini non raggiungibile"] --> EXIT1
  PING2 -->|ok| AGENT{"--agent"}

  AGENT -->|gmail| GT["_require_gmail_token"]
  GT -->|GmailAuthError| EXIT1
  GT -->|ok| SPEC_G["GMAIL_LOOP_SPEC"]

  AGENT -->|web| TV["_require_tavily_key"]
  TV -->|chiave vuota| EXIT1
  TV -->|ok| SPEC_W["WEB_LOOP_SPEC"]

  AGENT -->|fs| WS["_prepare_workspace"]
  AGENT -->|master default| WS
  WS --> ENSURE["ensure_workspace<br/>notes/ e inbox/"]
  ENSURE -->|OSError Desktop| EXIT1
  ENSURE --> SYNC["sync_workspace_index<br/>SQLite + Chroma"]
  SYNC --> SPEC_FS{"agent fs o master?"}
  SPEC_FS -->|fs| SPEC_F["FS_LOOP_SPEC"]
  SPEC_FS -->|master| SPEC_M["MASTER_LOOP_SPEC"]

  SPEC_G --> AUDIO
  SPEC_W --> AUDIO
  SPEC_F --> AUDIO
  SPEC_M --> AUDIO

  AUDIO["create_audio_pair<br/>mock o http"]
  AUDIO --> BANNER["_print_startup_banner su stderr"]
  BANNER --> LOOP["run_chat_loop"]
  LOOP --> FINALLY["llm.close + stt/tts.close"]
  FINALLY --> EXITC["SystemExit codice del loop"]
```

Note sui gate:

- **master** prepara Desktop/RAG perché `ask_fs` ne ha bisogno. Token Gmail e chiave Tavily sono **lazy** nel dispatch (`ask_gmail` / `ask_web`), non all’avvio.
- **gmail** e **web** isolati non toccano workspace né Chroma.
- Banner master: `tools=ask_fs,ask_gmail,ask_web`. Banner fs: create/append/read/find. Banner gmail: list/read/save/draft/reply/send. Banner web: `web_search`.

## 2. Factory audio

```mermaid
flowchart LR
  S["Settings.audio_driver"]
  S -->|mock default| M["MockSTT + MockTTS<br/>tastiera e stdout"]
  S -->|http| H["HttpBridgeSTT + HttpBridgeTTS<br/>audio_bridge_url"]
  H --> T1["listen: connect 5s<br/>read AUDIO_LISTEN_TIMEOUT_SEC 300s"]
  H --> T2["speak: connect 5s<br/>read AUDIO_SPEAK_TIMEOUT_SEC 120s"]
```

URL: `http://{WINDOWS_HOST o nameserver}:{WINDOWS_AUDIO_PORT}`. Contratto host: [flowchart audio](flowchart_audio.md).

## 3. `run_chat_loop`: un turno

TTS parla solo sull’esito (intro, HITL, reply Gemini, errore). Lo specialista nested **non** chiama `tts.speak`.

```mermaid
flowchart TB
  INIT["messages = system prompt dello spec"]
  INTRO["tts.speak intro_text"]
  STORE["TelemetryDB su INDEX_ROOT"]
  INIT --> INTRO --> STORE --> WAIT

  WAIT["stt.listen"]
  WAIT --> EMPTY{"transcript vuoto?"}
  EMPTY -->|sì| OUT1["tts: Nessun input. Uscita.<br/>return 0"]
  EMPTY -->|no| EXITW{"esci / exit / quit<br/>casefold?"}
  EXITW -->|sì| OUT2["tts: Arrivederci.<br/>return 0"]
  EXITW -->|no| HITL{"hitl_on_utterance<br/>bozza Gmail in attesa?"}

  HITL -->|frase gestita| SPEAKH["tts.speak esito Python<br/>sì invia / no annulla / ripeti"]
  SPEAKH --> TEL1["insert stt_requests"]
  TEL1 --> WAIT

  HITL -->|None| APPEND["append user content"]
  APPEND --> TASK["run_specialist_task"]
  TASK --> KIND{"result.kind"}
  KIND -->|hitl| SPK["tts.speak hitl_spoken"]
  KIND -->|text error exhausted| SPK2["tts.speak result.text"]
  SPK --> TEL2["insert stt_requests<br/>token sommati"]
  SPK2 --> TEL2
  TEL2 --> WAIT
```

Una riga telemetria per turno vocale valido: non intro, non riga vuota, non `esci`.

## 4. `run_specialist_task`: fino a 4 round, senza TTS

Stesso giro per il loop esterno e per il nested del master. Bind `get_active_llm` via contextvar: `ask_*` riusa lo stesso `GeminiChat`.

```mermaid
flowchart TB
  BIND["_active_llm.set llm"]
  BIND --> R["round 0..3"]

  R --> CHAT["llm.chat<br/>tools dello spec<br/>temperature 0.1"]
  CHAT -->|LLMError| POP1["pop ultimo messaggio"]
  POP1 --> ERR["kind=error<br/>Errore LLM: ..."]

  CHAT -->|ok| USAGE["accumulated += last_usage"]
  USAGE --> EMPTY{"turn.is_empty?"}
  EMPTY -->|sì| POP2["pop"] --> ERR2["kind=error<br/>Il modello non ha risposto"]

  EMPTY -->|no| FC{"function_calls?"}
  FC -->|nessuna| TXT{"prepare_spoken_text<br/>ha una frase?"}
  TXT -->|no, markup-only| NUDGE["append nudge user:<br/>Manca la frase da dire"]
  NUDGE --> R
  TXT -->|sì| OK["append assistant content<br/>kind=text"]

  FC -->|sì| FIRST["prima functionCall soltanto"]
  FIRST --> KEY["_tool_loop_key<br/>query o name"]
  KEY --> REP{"stessa chiave del round precedente?"}
  REP -->|sì| ANTI["nudge: hai già l esito,<br/>rispondi a voce senza tool"]
  ANTI --> R
  REP -->|no| DISP["append functionCall<br/>spec.dispatch<br/>print_tool_result opzionale<br/>append functionResponse"]
  DISP --> HIT{"hitl_after_tool<br/>restituisce testo?"}
  HIT -->|sì| HITL["kind=hitl<br/>text = conferma Python"]
  HIT -->|no| R

  R -->|4 round senza text né HITL| EXH["kind=exhausted<br/>Non sono riuscito a completare"]
```

Anti-ripetizione: `find_file` / `list_emails` / `web_search` usano `query`; `read_file` / `read_email` usano `name`. Stessa call di fila → niente secondo dispatch, solo nudge parlato.

## 5. Gemini: storia → `generateContent`

```mermaid
flowchart LR
  subgraph IN["Storia del loop"]
    SYS["role system → systemInstruction"]
    TXT["user/assistant content → parts text"]
    CALL["assistant function_calls → parts functionCall"]
    RESP["user function_response → parts functionResponse"]
    SIG["thought_signature se presente<br/>Gemini 3 lo richiede dopo il tool"]
  end

  subgraph OUT["LlmTurn"]
    FC["functionCall name + args"]
    PT["parts text"]
    US["usageMetadata → TokenUsage"]
  end

  SYS --> API["POST generateContent<br/>header x-goog-api-key"]
  TXT --> API
  CALL --> API
  RESP --> API
  SIG --> API
  API --> FC
  API --> PT
  API --> US
```

Niente `responseMimeType: application/json` insieme ai tool: confligge con `functionCall`. Reply parlata = `parts[].text`, non un JSON `tool=none`. Prima del TTS, `prepare_spoken_text` toglie markdown residuo.

## 6. Errori Gemini mappati in italiano

```mermaid
flowchart TB
  HTTP["HTTP da generateContent"]
  HTTP -->|quota / billing RESOURCE_EXHAUSTED| Q["Quota Gemini esaurita"]
  HTTP -->|429 RPM/TPM| R["Rate limit, riprova"]
  HTTP -->|401 403| K["Chiave non valida"]
  HTTP -->|altro| F["chat fallita HTTP n"]
```

Il loop parla quella frase (`kind=error`) e torna in ascolto. La storia toglie l’ultimo user message così un retry non riparte a metà.
