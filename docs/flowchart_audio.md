# Flowchart: host audio Windows

Indice: [flowchart.md](flowchart.md). Operativo: [audio_host.md](audio_host.md). Codice: [`windows_audio/server.py`](../windows_audio/server.py), [`helper_spawn.py`](../windows_audio/helper_spawn.py), [`stt_helper/`](../windows_audio/stt_helper/), client WSL [`http_bridge.py`](../src/lavora_e_guida/audio/http_bridge.py).

Due processi **solo su Windows**, avvio unico dal Python host. WSL non deve eseguire `server.py` (fail-fast, codice 2).

## 1. Avvio host: spawn helper e bind

```mermaid
flowchart TB
  PY["python.exe server.py"]
  PY --> WIN{"sys.platform == win32?"}
  WIN -->|no, senza --allow-non-windows| E2["stderr: avvia con python.exe su Windows<br/>exit 2"]
  WIN -->|sì| SUP["HelperSupervisor"]

  SUP --> READY{"GET 127.0.0.1:8766/health<br/>già 2xx?"}
  READY -->|sì| REUSE["non spawn: helper già in ascolto"]
  READY -->|no| SPAWNF{"--no-spawn-helper?"}
  SPAWNF -->|sì| E1["RuntimeError: avvia stt_helper a mano"]
  SPAWNF -->|no| CMD["_build_command"]

  CMD --> EXE{"stt_helper.exe in bin/<br/>e sorgenti non più nuove?"}
  EXE -->|sì o --helper-exe| RUNEXE["Popen exe --port=8766"]
  EXE -->|no o .cs più recente| DOT{"dotnet nel PATH?"}
  DOT -->|no| E3["Né exe né SDK .NET 8"]
  DOT -->|sì| RUNNET["dotnet run --project csproj<br/>--port=8766"]

  RUNEXE --> WAIT
  RUNNET --> WAIT
  WAIT["_wait_until_ready fino a 90s"]
  WAIT --> DIE{"processo uscito?"}
  DIE -->|sì| E4["Helper uscito subito<br/>vedi TEMP spawn.log"]
  DIE -->|no| HOK{"/health 2xx?"}
  HOK -->|sì| BIND
  HOK -->|timeout| STOP["terminate figlio"] --> E5["non ha risposto in 90s"]

  REUSE --> BIND
  BIND["HTTPServer 0.0.0.0:8765<br/>ThreadingMixIn"]
  BIND -->|porta occupata| E6["Bind fallito"]
  BIND -->|ok| SERVE["serve_forever"]
  SERVE --> CTRL["Ctrl+C"]
  CTRL --> FIN["server_close + helper.stop"]
```

`CREATE_NEW_PROCESS_GROUP` su Windows: Ctrl+C nella console Python non abbatte il figlio prima del `finally`. `stop` termina solo il pid spawnato da questo processo, non un helper riusato.

Log: `%TEMP%\audio_host_helper_spawn.log` (dotnet/spawn) e `%TEMP%\stt_helper.log` (WinExe, niente console).

## 2. Contratto HTTP verso WSL

```mermaid
flowchart TB
  REQ["thread per request"]
  REQ --> M{"metodo e path"}

  M -->|GET /health o /| H["_handle_health<br/>SENZA mutex"]
  H --> PROBE["helper_is_ready timeout 0.8s"]
  PROBE --> JSONH["ok, helper, speaking<br/>200 o 503"]

  M -->|POST /listen| L["_handle_listen"]
  L --> LOCKL["acquire half_duplex"]
  LOCKL --> FWD["_forward_listen_abortable"]

  M -->|POST /speak| S["_handle_speak"]
  S --> BODY{"JSON text non vuoto?"}
  BODY -->|no| B400["400"]
  BODY -->|sì| LOCKS["acquire half_duplex"]
  LOCKS --> SET["speaking.set"]
  SET --> PLAY["synthesize_and_play"]
  PLAY -->|ok| CLR["speaking.clear"] --> S200["200 ok true"]
  PLAY -->|eccezione| CLR2["speaking.clear"] --> S502["502 tts fallito"]

  M -->|altro| N404["404"]
```

Half-duplex: niente `/listen` mentre parla Elsa (il mic WinRT non deve sentirla). `/health` non prende il lock, così un probe non resta in coda dietro un ascolto di minuti.

## 3. Inoltro `/listen` e client WSL staccato

Il tetto vero è `AUDIO_LISTEN_TIMEOUT_SEC` lato WSL (300s). L’host inoltra **senza** timeout verso :8766. Un transcript vuoto **non** si inoltra: il loop WSL direbbe «Nessun input. Uscita.»

```mermaid
flowchart TB
  FWD["worker HTTPConnection 127.0.0.1:8766 POST /listen"]
  LOOP["ogni 0.4s: peer_disconnected sul socket WSL?"]
  FWD --> LOOP

  LOOP -->|WSL ha chiuso timeout httpx| GONE["chiudi socket verso C#<br/>ClientGone<br/>rilascia mutex"]
  LOOP -->|worker finito 200| TR{"transcript non vuoto?"}
  TR -->|sì| PASS["pass-through JSON C#"]
  TR -->|no| B502["502 helper transcript vuoto"]
  LOOP -->|worker 503| PRE["503 listen preemptato<br/>secondo /listen C#"]
  LOOP -->|connection refused| H502["502 helper non raggiungibile"]
```

Chiudere la socket verso C# **non** ferma WinRT: serve un nuovo `POST /listen` per preempt. Serve a sbloccare il thread Python e il mutex.

## 4. Helper C#: processo STA

WinRT `SpeechRecognizer` è COM/STA. `Application.Run` su form nascosta pompa i messaggi. Un thread nudo con `Sleep` non consegna hypotesis.

```mermaid
flowchart TB
  MAIN["Program.Main STAThread"]
  MAIN --> SET["HelperSettings<br/>args vincono su env vincono su default"]
  SET --> FORM["HiddenStaForm"]
  FORM --> HWND["EnsureHandle<br/>non Form.Load: SetVisibleCore false non spara Load"]
  HWND --> HTTP["LoopbackListenServer 127.0.0.1:port"]
  HTTP -->|porta occupata| BOX["MessageBox + exit"]
  HTTP -->|ok| RUN["Application.Run form"]
  RUN --> POOL["HttpListener sul thread pool"]
  POOL -->|"GET /health"| OK["200 helper vivo"]
  POOL -->|"POST /listen"| STA["PostToSta SpeechListenLoop.ListenAsync"]
```

Wake default (tre parole, così un «chiudi» isolato in una mail non chiude il turno):

| Fase | Default env | Effetto |
| --- | --- | --- |
| Apertura | `WAKE_OPEN` = ehi assistente | beep Asterisk, passa a dictation |
| Chiusura | `WAKE_CLOSE` = assistente chiudi | JSON senza la wake |
| Annulla | `WAKE_CANCEL` = assistente annulla | scarta buffer, beep Hand, **stesso** HTTP aperto |

Alias: list-constraint accetta anche «hei assistente» (forma parlata di «ehi» in WinRT it-IT).

## 5. Macchina a stati di un `POST /listen`

Un riconoscitore per volta: list-constraint sulla wake, **Dispose**, poi topic Dictation su un `SpeechRecognizer` **nuovo**. Mai due motori sullo stesso mic.

```mermaid
flowchart TB
  HTTP["POST /listen sul pool"]
  HTTP --> PRE["PreemptAndRunStaAsync<br/>runId++<br/>TCS precedente → canceled 503"]
  PRE --> TEAR["TearDownRecognizer"]
  TEAR --> WAKE["WaitForOpenWakeStaAsync<br/>list constraint it-IT"]

  WAKE --> MATCH{"frase contiene<br/>ehi assistente?"}
  MATCH -->|no| WAKE
  MATCH -->|preempt / cancel| ENDC["TCS canceled"]
  MATCH -->|sì| BEEP["SystemSounds.Asterisk<br/>Dispose wake, Delay 700ms"]

  BEEP --> DIC["run_dictation Vosk it-IT<br/>timer 45s dalla wake"]

  DIC --> EVT["Vosk Result / PartialResult"]
  EVT --> NORM["testo normalizzato"]
  NORM --> ORD{"ordine: prima annulla, poi chiudi"}

  ORD -->|assistente annulla| CAN["DictationKind.Cancel<br/>beep Hand, Delay 250ms<br/>buffer vuoto, HTTP ancora aperto"]
  CAN --> WAKE

  ORD -->|assistente chiudi| CLS["Close: transcript senza wake"]
  CLS --> JSON["TCS result JSON transcript"]

  ORD -->|altro testo| BUF["committed + hypotesis<br/>silenzio NON chiude"]
  BUF --> DIC

  DIC --> T45{"Tick 45s"}
  T45 -->|buffer con testo| TOWT["TimeoutWithText<br/>stesso JSON di chiudi"]
  TOWT --> JSON
  T45 -->|buffer vuoto| TOE["TimeoutEmpty<br/>niente beep, niente JSON"]
  TOE --> WAKE
```

Timeout silenzio WinRT: in dettatura `EndSilence` corto (1–2s) così arriva `ResultGenerated`; `AutoStopSilence` al massimo accettato dalla build. Il tetto di **turno** resta il timer 45s. Dopo la wake si **Dispose** e si crea un riconoscitore nuovo con topic Dictation (ricompilare list→topic sullo stesso oggetto fallisce). Compile topic fallisce senza «Riconoscimento vocale online» (Impostazioni → Privacy → Comandi vocali) e rete Microsoft.

## 6. TTS: edge-tts + pygame

200 a WSL solo a **playback finito**. Mixer init al primo speak, quit dopo ogni frase (altrimenti il device resta preso e il mic ronza).

```mermaid
flowchart TB
  SPEAK["POST /speak text"]
  SPEAK --> SYN["_save_mp3 via edge-tts<br/>voce it-IT-ElsaNeural"]
  SYN -->|rete / MP3 vuoto| E502["502"]
  SYN -->|ok| MIX["_ensure_mixer 24kHz stereo"]
  MIX --> PLAY["pygame.mixer.music<br/>sleep durata, tetto 90s"]
  PLAY --> REL["_release_mixer"]
  REL --> OK["200"]
```

Il testo arriva già pulito da `prepare_spoken_text` in WSL. Dipendenze nel venv **Windows** (`windows_audio/requirements.txt`), non in `pyproject.toml` WSL.

## 7. Turno vocale end-to-end con `AUDIO_DRIVER=http`

```mermaid
sequenceDiagram
  participant L as Loop WSL
  participant B as http_bridge
  participant P as server.py :8765
  participant C as stt_helper :8766
  participant M as Mic / speaker

  L->>B: tts.speak intro
  B->>P: POST /speak
  P->>M: Elsa playback
  P-->>B: 200
  L->>B: stt.listen
  B->>P: POST /listen long-poll
  P->>C: POST /listen
  C->>M: attesa ehi assistente
  M-->>C: wake, beep, dettatura
  M-->>C: assistente chiudi
  C-->>P: transcript
  P-->>B: 200 transcript
  B-->>L: stringa
  Note over L: Gemini + tool
  L->>B: tts.speak reply
  B->>P: POST /speak mutex
```

## 8. NAT vs mirrored (solo discovery)

```mermaid
flowchart TB
  ENV{"WINDOWS_HOST in .env?"}
  ENV -->|valorizzato| URL["http://WINDOWS_HOST:8765"]
  ENV -->|vuoto| NAT{"WSL networking"}
  NAT -->|NAT default| NS["nameserver di /etc/resolv.conf"]
  NAT -->|Mirrored| WARN["127.0.0.1 del nameserver può essere sbagliato<br/>forzare WINDOWS_HOST=127.0.0.1"]
```

La 8766 non va aperta sul firewall. La 8765 sì, verso WSL (rete privata).
