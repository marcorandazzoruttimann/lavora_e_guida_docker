# Host audio Windows (WinRT STT + edge-tts)

L’orchestratore vocale resta in **WSL**. Microfono e altoparlanti vivono sull’**host Windows**: un processo Python ascolta `0.0.0.0:8765` (raggiungibile da WSL) e spawn un helper C# su `127.0.0.1:8766` (loopback, WSL non lo vede).

Flowchart: [flowchart_audio.md](flowchart_audio.md) (indice [flowchart.md](flowchart.md)).

Il client WSL [`http_bridge.py`](../src/lavora_e_guida/audio/http_bridge.py) non cambia forma:

- `POST /listen` → `{"transcript": "..."}`
- `POST /speak` → `{"text": "..."}` → 200 a playback finito
- `GET /health` → liveness Python + probe helper

Niente transcript vuoto: l’helper resta in ascolto della wake di apertura; il tetto è il timeout HTTP di WSL. Un `""` farebbe uscire il loop (`Nessun input. Uscita.`).

Si edita in questo repo (anche da WSL). Si **esegue** con `python.exe` / `dotnet` su Windows, idealmente da un clone o una cartella su `C:\`, non solo `\\wsl$\`.

## Architettura

Due processi **solo su Windows**, avvio unico dal Python host.

| Processo | Bind | Ruolo |
| --- | --- | --- |
| Python [`windows_audio/server.py`](../windows_audio/server.py) | `0.0.0.0:8765` | Contratto HTTP verso WSL, mutex half-duplex, TTS `edge-tts` + playback pygame, **dettatura Vosk** dopo la wake |
| Helper C# [`windows_audio/stt_helper/`](../windows_audio/stt_helper/) | `127.0.0.1:8766` | Solo wake WinRT (list constraint) su thread STA; poi `{"awake":true}` |

Half-duplex: niente `/listen` mentre parla Elsa. `/health` non prende il lock.

Flusso tipico:

1. WSL `POST /speak` → Python sintetizza (`it-IT-ElsaNeural`) e riproduce fino alla fine → 200.
2. WSL `POST /listen` → Python inoltra a `:8766` e aspetta.
3. Helper: list-constraint sulla wake → beep → Dispose. Python: Vosk it-IT (il silenzio **non** chiude il turno).
4. «assistente chiudi» (o timeout 45s con testo) → JSON senza la wake → WSL.
5. «assistente annulla» → scarta il buffer, beep diverso, **stesso** `POST /listen` ancora in volo. WSL e Gemini non vedono la frase.

## Prerequisiti Windows

Da fare a mano, una volta. L’helper fallisce in modo parlante se mancano.

1. **Microfono** collegato e selezionato come dispositivo di input predefinito.
2. **Privacy microfono:** Impostazioni → Privacy e sicurezza → Microfono → accesso alle app **acceso** (anche «app desktop»).
3. **Riconoscimento vocale online:** non serve alla dettatura (Vosk è locale). La wake list WinRT usa il pacchetto lingua it-IT.
4. **Pacchetto lingua italiano:** Impostazioni → Ora e lingua → aggiungere **Italiano** e il FOD Speech (`Language.Speech~~~it-IT`). Non serve il riconoscitore SAPI desktop `MS-xxxx-80-DESK` (su Windows 11 spesso c’è solo en-US).
5. **SDK .NET 8** (workload desktop): [download](https://dotnet.microsoft.com/download/dotnet/8.0). Target dell’helper: `net8.0-windows10.0.19041.0` (Windows 10 2004 / Windows 11).
6. **Python 3.12+** per Windows (`py -3.12`), distinto dal venv WSL. Non aggiungere `edge-tts` / `pygame` a [`pyproject.toml`](../pyproject.toml).

Windows 10 2004+ o Windows 11. WinRT `SpeechRecognizer` + `ContinuousRecognitionSession` non girano da WSL.

## Avvio sull’host (PowerShell)

Cartella del clone su disco Windows, non un path `\\wsl$\...` (I/O lento e `dotnet`/pygame ci inciampano).

```powershell
cd C:\percorso\lavora_e_guida\windows_audio
py -3.12 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python server.py
```

All’avvio il Python host:

- fail-fast se non è `win32` (da WSL: messaggio e codice 2);
- spawn `stt_helper.exe` sotto `stt_helper/bin/` se c’è, altrimenti `dotnet run` sul csproj (la prima compilazione può richiedere fino a ~90s);
- **riscalda edge-tts** (sintesi silenziosa: il primo giro a freddo può durare 2-3 minuti);
- ascolta `http://0.0.0.0:8765`;
- all’uscita (Ctrl+C) termina l’helper che ha spawnato.

Attendi il log `Host audio in ascolto` **prima** di lanciare `lavora-e-guida` da WSL. Non rilanciare WSL se il primo `/speak` è ancora in corso: il mutex half-duplex tiene Elsa e il secondo processo sente **due intro**. L’avvio può restare un paio di minuti su «Riscaldo edge-tts»: è voluto.

Verifica locale (sempre su Windows):

```powershell
curl.exe -sS http://127.0.0.1:8765/health
```

Atteso: `{"ok": true, "helper": true, "speaking": false}`. Se `ok` è false, l’helper non è pronto: vedi `%TEMP%\audio_host_helper_spawn.log` e `%TEMP%\stt_helper.log`.

### Firewall porta 8765

Il bind è `0.0.0.0`: WSL in NAT raggiunge l’IP dell’host, non `127.0.0.1` di Windows. Al primo ascolto Windows può chiedere di consentire **Python** in rete privata. Se il prompt non compare:

```powershell
New-NetFirewallRule -DisplayName "lavora_e_guida audio host" -Direction Inbound -Protocol TCP -LocalPort 8765 -Action Allow -Profile Private
```

La 8766 resta loopback: non va aperta sul firewall.

### Flag e env del processo host

Non passano dal `.env` WSL: valgono per il processo Windows che lancia `server.py`.

| Variabile / flag | Default | Ruolo |
| --- | --- | --- |
| `AUDIO_HOST_BIND` / `--bind` | `0.0.0.0` | Ascolto verso WSL |
| `AUDIO_HOST_PORT` / `--port` | `8765` | Porta verso WSL |
| `STT_HELPER_PORT` / `--helper-port` | `8766` | Loopback helper |
| `STT_HELPER_EXE` / `--helper-exe` | ricerca in `bin/` | Exe già compilato |
| `--no-spawn-helper` | off | Helper già avviato a mano |
| `TTS_VOICE` / `--voice` | `it-IT-ElsaNeural` | Voce edge-tts |
| `WAKE_OPEN` | `ehi assistente` | Ereditata dal figlio C# |
| `WAKE_CLOSE` | `assistente chiudi` | idem |
| `WAKE_CANCEL` | `assistente annulla` | idem |

## Rete WSL: NAT vs mirrored

WSL2 di default è **NAT**: la macchina virtuale ha un IP proprio; l’IP dell’host Windows è il `nameserver` di `/etc/resolv.conf`. `WINDOWS_HOST` vuoto in `.env` usa quella discovery ([`discover_windows_host`](../src/lavora_e_guida/config.py)).

| Networking WSL | `WINDOWS_HOST` | URL tipico |
| --- | --- | --- |
| **NAT** (default) | vuoto | `http://<nameserver>:8765` |
| **Mirrored** (Windows 11, `.wslconfig`) | `127.0.0.1` | `http://127.0.0.1:8765` |

In mirrored, localhost di WSL e di Windows coincidono: senza override la discovery da `resolv.conf` può puntare a un nameserver che **non** è il bind del Python host.

Controllo da WSL, host già avviato:

```bash
# NAT: IP del nameserver
curl -sS "http://$(awk '/^nameserver/{print $2;exit}' /etc/resolv.conf):8765/health"
# Mirrored:
curl -sS http://127.0.0.1:8765/health
```

L’helper `:8766` non è raggiungibile da WSL in NAT (e non deve esserlo).

## Loop vocale da WSL (`AUDIO_DRIVER=http`)

Nel `.env` del repo (lato WSL), con l’host già in ascolto:

```env
AUDIO_DRIVER=http
WINDOWS_HOST=
WINDOWS_AUDIO_PORT=8765
# AUDIO_LISTEN_TIMEOUT_SEC=300
# AUDIO_SPEAK_TIMEOUT_SEC=120
```

Poi, venv WSL attivo: `lavora-e-guida` come al quick start del [README](../README.md). `AUDIO_DRIVER=mock` resta il default (tastiera, niente microfono).

Timeout HTTP WSL → host ([`config.py`](../src/lavora_e_guida/config.py), [`http_bridge.py`](../src/lavora_e_guida/audio/http_bridge.py)):

| Operazione | Default | Env | Note |
| --- | --- | --- | --- |
| connect | 5s | fisso nel client | Porta chiusa / firewall: non ereditare i minuti del listen |
| `POST /listen` | 300s | `AUDIO_LISTEN_TIMEOUT_SEC` | Wake + dettatura; l’host non chiude su silenzio |
| `POST /speak` | 120s | `AUDIO_SPEAK_TIMEOUT_SEC` | Sintesi + playback fino alla fine |

## Tre wake e timeout dettatura

Frasi da **tre parole**, così in dettatura un «chiudi» o «annulla» isolato (es. in una mail) non chiude il turno. Configurabili da env/argomenti dell’helper (`WAKE_OPEN` / `WAKE_CLOSE` / `WAKE_CANCEL`, o `--wake-open` ecc.; gli argomenti vincono).

| Fase | Default | Effetto |
| --- | --- | --- |
| Apertura | **ehi assistente** | Beep Asterisk, passa a dictation it-IT |
| Chiusura | **assistente chiudi** | Stop, transcript senza la wake, JSON a Python |
| Annulla | **assistente annulla** | Stop, buffer vuoto, beep Hand, torna all’apertura. HTTP ancora aperto |

Timeout **45 secondi** dopo la wake di apertura (non dopo ogni hypotesis):

- c’è testo accumulato → chiusura implicita, stesso JSON;
- buffer vuoto → non chiude verso Python, torna alla wake di apertura.

Il silenzio tra le parole **non** termina il turno: il tetto è il timer 45s (o «assistente chiudi»). In dettatura `EndSilenceTimeout` è ~2s (una frase per `ResultGenerated`); `AutoStopSilenceTimeout` resta lungo.

Ordine sul testo normalizzato: prima annulla, poi chiudi.

## Troubleshooting STT (WinRT wake + dettatura continua)

Log: `%TEMP%\stt_helper.log` (helper WinExe, niente console) e `%TEMP%\audio_host_helper_spawn.log` (`dotnet run` / spawn).

| Sintomo | Cosa controllare |
| --- | --- |
| `Impossibile creare SpeechRecognizer it-IT` | Pacchetto lingua italiano installato; a volte serve un sign-out. |
| `CompileConstraintsAsync=…` sulla wake | Lingua it-IT installata; riconoscimento vocale online per WinRT. |
| Dettatura: hypotesis WinRT / `UserCanceled` | Vecchio helper topic cloud. Ora dopo il beep gira **Vosk**. Log host: `Wake ok, dettatura Vosk` e `Vosk frase`. |
| `pip install vosk` / ImportError Vosk | Nel venv **Windows**: `pip install -r requirements.txt`. Primo ascolto scarica il modello it (~50 MB) in `%LOCALAPPDATA%\\lavora_e_guida\\`. |
| `Nessun riconoscitore SAPI it-IT` | Vecchio helper. Ricompila (log spawn: sorgenti C# più recenti). |
| `RecognizeAsync status=Success conf=Rejected text=''` | Vecchio pump RecognizeAsync. Serve ricompilare. |
| Dettatura taglia sul silenzio | Il turno resta aperto 45s; parla «assistente chiudi» dopo il comando. |
| Due frasi introduttive | WSL ha fatto timeout sul primo `/speak` e l’hai rilanciato mentre Elsa era ancora in mutex. Attendi `Host audio in ascolto` e un solo `POST /speak ok`. |
| `StartAsync del riconoscimento continuo fallito` | Consenso microfono alle app desktop; altro processo che tiene il mic in exclusive mode. |
| Wake non scatta | Parlare la frase intera («ehi assistente»). List-constraint ignora il resto. Volume / mic di default. |
| `GET /health` 503 | Helper morto o ancora in compilazione. Attendere 90s al primo `dotnet run`; se esce subito, log di spawn. |
| Bind `8765` fallito | Altro `server.py` già in ascolto. |
| Helper «già in ascolto» ma morto | Zombie su 8766: Task Manager → `stt_helper` / `dotnet`, oppure `--helper-port` diverso. |
| WSL: connect timeout / `ConnectError` | Host non avviato; firewall 8765; NAT vs mirrored (`WINDOWS_HOST`). |
| WSL: read timeout su `/listen` | 300s senza wake. Alzare `AUDIO_LISTEN_TIMEOUT_SEC` o dire la wake prima. |
| TTS 502 / MP3 vuoto | `pip install -r requirements.txt` nel venv **Windows**; rete verso edge-tts; voce `it-IT-ElsaNeural`. |
| Nessun suono in uscita | Dispositivo playback predefinito Windows; pygame mixer all’init (primo `/speak`). |

Da WSL **non** avviare `server.py` (fail-fast). I test del package (`pytest`) restano in WSL con mock HTTP; non esercitano WinRT né pygame.
