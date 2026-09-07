# Flowchart dell’ecosistema

Mappe Mermaid del runtime vocale. Ogni pagina è un dominio: i diagrammi restano leggibili in preview Markdown.

| Pagina | Cosa copre |
| --- | --- |
| Questa | Vista d’insieme: processi, porte, agenti, dati |
| [Avvio e loop vocale](flowchart_avvio_e_loop.md) | CLI `lavora-e-guida`, fail-fast, `run_chat_loop`, function calling Gemini |
| [Router master](flowchart_master.md) | `ask_fs` / `ask_gmail` / `ask_web`, storie isolate, flusso composto |
| [Host audio Windows](flowchart_audio.md) | `server.py` :8765, helper C# :8766, half-duplex, tre wake, TTS |
| [Gmail](flowchart_gmail.md) | OAuth a tavolino, tool mailbox, HITL sì/no sull’invio |
| [File e RAG](flowchart_fs_rag.md) | Desktop, sync indice, `find_file`, hook post-scrittura |

Documentazione operativa (non flowchart): [README](../README.md), [audio host](audio_host.md), [OAuth Gmail](gmail_oauth.md), [Tavily](tavily_web.md), [Ollama studio](ollama.md).

## Vista d’insieme: due macchine

L’orchestratore vive in **WSL2**. Microfono e altoparlanti vivono sull’**host Windows**. Gemini, Gmail e Tavily sono servizi cloud. Qwen 2.5 3B via Ollama non entra nel loop vocale (`--llm ollama` è fail-fast parlante).

```mermaid
flowchart TB
  subgraph WSL["WSL2 — Python lavora-e-guida"]
    CLI["main.py<br/>lavora-e-guida"]
    LOOP["run_chat_loop"]
    SPEC["LoopSpec attivo:<br/>master, fs, gmail o web"]
    BRIDGE["http_bridge.py<br/>AUDIO_DRIVER=http"]
    MOCK["mock.py<br/>AUDIO_DRIVER=mock"]
    CFG["config.Settings<br/>.env"]
    RAG["INDEX_ROOT/runtime<br/>SQLite + Chroma"]
    WS["WORKSPACE_ROOT<br/>Desktop Ollama_test"]
  end

  subgraph WIN["Host Windows — due processi"]
    PY["server.py :8765<br/>0.0.0.0"]
    CS["stt_helper.exe :8766<br/>127.0.0.1 STA WinRT"]
    EDGE["edge-tts + pygame"]
    MIC["Microfono"]
    SPK["Altoparlanti"]
  end

  subgraph CLOUD["Cloud"]
    GEM["Gemini generateContent<br/>functionDeclarations"]
    GAPI["Gmail REST"]
    TAV["Tavily search"]
    ETTS["Microsoft edge-tts"]
  end

  CLI --> LOOP
  LOOP --> SPEC
  LOOP --> BRIDGE
  LOOP --> MOCK
  LOOP --> GEM
  SPEC --> RAG
  SPEC --> WS
  SPEC --> GAPI
  SPEC --> TAV
  CFG --> CLI
  BRIDGE -->|"POST /listen POST /speak GET /health"| PY
  PY -->|"POST /listen loopback"| CS
  PY --> EDGE
  EDGE --> ETTS
  EDGE --> SPK
  CS --> MIC
```

## Porte e chi può vederle

```mermaid
flowchart LR
  WSL["Client WSL<br/>http_bridge"]
  H875["Python host<br/>0.0.0.0:8765"]
  H876["Helper C#<br/>127.0.0.1:8766"]

  WSL -->|"NAT: IP nameserver<br/>Mirrored: 127.0.0.1"| H875
  H875 -->|"solo loopback<br/>WSL non arriva"| H876
```

In NAT, `WINDOWS_HOST` vuoto usa il nameserver di `/etc/resolv.conf`. In mirrored va impostato `WINDOWS_HOST=127.0.0.1`. Dettaglio: [audio host](audio_host.md#rete-wsl-nat-vs-mirrored).

## Quattro agenti, un motore

Il motore (`agent.py`) non possiede un catalogo di dominio. Ogni agente passa un `LoopSpec` (prompt, `functionDeclarations`, dispatch, HITL). Il default del loop è il **router master**.

```mermaid
flowchart TB
  USER["Utente vocale o mock"]
  ENGINE["run_chat_loop<br/>STT, TTS, telemetria"]

  USER --> ENGINE

  ENGINE -->|"--agent master default"| M["MASTER_LOOP_SPEC<br/>ask_fs ask_gmail ask_web"]
  ENGINE -->|"--agent fs"| F["FS_LOOP_SPEC<br/>create append read find"]
  ENGINE -->|"--agent gmail"| G["GMAIL_LOOP_SPEC<br/>list read save draft reply send"]
  ENGINE -->|"--agent web"| W["WEB_LOOP_SPEC<br/>web_search"]

  M -->|"nested run_specialist_task<br/>storia isolata"| F
  M -->|"nested + HITL sul loop esterno"| G
  M -->|"nested, gate chiave lazy"| W
```

Un tool **eseguito** per round Gemini. Se il modello ne propone due, Python prende la prima `functionCall`. Più round nello stesso enunciato: fino a 4 (`_MAX_TOOL_ROUNDS`). Esempio composto: «Cerca il meteo e mandalo a Mario» → round 1 `ask_web`, round 2 `ask_gmail`. Nessun file sul Desktop se non è stato chiesto.

## Dati: due root

| Concetto | Path | Chi lo tocca |
| --- | --- | --- |
| File utente | `WORKSPACE_ROOT` (Desktop) | master e `--agent fs`; Gmail scrive solo `email_attachments/` |
| Stato locale | `INDEX_ROOT` (`runtime/` nel repo) | SQLite + Chroma, `telemetry.db`, `gmail_token.json` |

Gmail e web isolati **non** chiamano `ensure_workspace` né il sync RAG all’avvio.

## Turno vocale in una riga

```mermaid
sequenceDiagram
  participant U as Utente
  participant STT as STT mock o HTTP
  participant L as run_chat_loop
  participant G as Gemini
  participant T as Tool Python
  participant TTS as TTS mock o HTTP

  U->>STT: parla o digita
  STT->>L: transcript
  alt esci / vuoto
    L->>TTS: Arrivederci o Nessun input
  else HITL sì/no su bozza Gmail
    L->>T: send_email o annulla
    L->>TTS: esito Python
  else turno Gemini
    L->>G: storia + functionDeclarations
    opt functionCall
      G-->>L: name + args
      L->>T: dispatch dello spec
      T-->>L: OK o ERRORE parlante
      L->>G: functionResponse
    end
    G-->>L: parts text
    L->>TTS: prepare_spoken_text
  end
  TTS->>U: frase
```

Il dettaglio di ogni rombo (fail-fast, nested, wake, OAuth) sta nelle pagine collegate in cima.
