# Lavora e Guida

Ecosistema agentico vocale hands-free: orchestratore in WSL2, I/O audio sul Windows host.

Loop attuale: Mock/HTTP STT → Gemini (function calling nativo: `functionDeclarations` + `functionCall` / `parts[].text`) → router master (`ask_fs` / `ask_gmail` / `ask_web`) → specialista nested → TTS.

Qwen 2.5 3B (Ollama) è extra di studio: `--llm ollama` sul loop vocale è fail-fast parlante. Il modulo `local_ollama.py` può restare per prove isolate.

## Prerequisiti di sistema

Prima dei venv. Python richiesto: **3.12 o superiore** (`requires-python` in [`pyproject.toml`](pyproject.toml), ruff `py312`). **3.10 e 3.11 non bastano.**

### WSL2

- **WSL2** (non WSL1), distro Debian/Ubuntu. Su Ubuntu 24.04 `python3` è già 3.12; su 22.04 installare 3.12 (es. deadsnakes) prima del venv.
- Pacchetti APT per il loop di prodotto (Gemini, RAG, Gmail, client HTTP audio):

```bash
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv python3-pip libgomp1
```

`python3.12-venv` abilita `python3.12 -m venv`. `libgomp1` è il runtime OpenMP di ONNX dentro Chroma: senza, il RAG può fallire all’import con `libgomp.so.1` mancante.

Opzionale: `zstd` solo se si installa Ollama in WSL ([docs/ollama.md](docs/ollama.md)). `build-essential` e `python3.12-dev` solo se pip ricompila un wheel (con 3.12 e Ubuntu recente di solito non servono).

- Rete in uscita verso Gemini (e Tavily/Gmail se usati). `GEMINI_API_KEY` nel `.env` WSL.
- Workspace file sul Desktop Windows, visibile in WSL sotto `/mnt/c/...`.
- Per il consenso Gmail dal browser Windows: networking **mirrored** (o port forward); dettaglio [docs/gmail_oauth.md](docs/gmail_oauth.md).

### Host Windows (solo `AUDIO_DRIVER=http`)

WinRT **non** è un pacchetto pip: la wake è l’helper C# (`stt_helper`, `:8766`). Il `python.exe` host fa TTS, playback e dettatura Vosk. Nessun `winrt` / `winsdk` nel venv.

- Windows **10 2004+** o Windows 11 (API `SpeechRecognizer` / `ContinuousRecognitionSession`).
- Python **3.12+ a 64 bit** per Windows, distinto dal Python WSL. Installer python.org o Store; avvio con il launcher `py -3.12`. Pacchetti pip: sezione [Ambienti](#ambienti-wsl2-vs-host-windows) / [`windows_audio/requirements.txt`](windows_audio/requirements.txt).
- **SDK .NET 8** (workload desktop) se manca già `stt_helper.exe` compilato. Non si compila da WSL. [Download](https://dotnet.microsoft.com/download/dotnet/8.0).
- Pacchetto lingua **Italiano** e FOD Speech (`Language.Speech~~~it-IT`) per la wake list WinRT. La dettatura è Vosk (locale): il riconoscitore vocale *online* di Windows non serve.
- Microfono predefinito; Impostazioni → Privacy e sicurezza → Microfono → accesso alle **app desktop**.
- Rete HTTPS verso i server Microsoft (`edge-tts`, voce `it-IT-ElsaNeural`). Altoparlante/cuffie predefiniti per pygame.
- Se `vosk` o `pygame` non caricano le DLL native: [Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist) x64.
- Clone su disco `C:\`, non solo `\\wsl$\` (I/O lento; `dotnet` e pygame ci inciampano).
- Firewall: al primo bind Windows può chiedere di consentire Python in rete privata (porta **8765** verso WSL). La 8766 resta loopback.

Troubleshooting: [docs/audio_host.md](docs/audio_host.md).

## Ambienti: WSL2 vs Host Windows

Due Python, due venv. Non mescolare e non fare `pip install -e .` dal `python.exe` Windows (né il contrario). Interprete, APT e WinRT: [Prerequisiti di sistema](#prerequisiti-di-sistema).

| Dove | File dipendenze | Cosa installa |
| --- | --- | --- |
| **WSL2** (orchestratore, default) | [`pyproject.toml`](pyproject.toml) | Gemini REST, RAG, Gmail, Tavily, client HTTP verso l’host audio |
| **Host Windows** (`windows_audio`) | [`windows_audio/requirements.txt`](windows_audio/requirements.txt) | TTS, playback, dettatura Vosk. **Non** è un extra di `pyproject.toml` |

### WSL2

Dopo i prerequisiti APT, dalla root del repo:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

| Pacchetto (`pyproject.toml`) | Ruolo nel loop WSL |
| --- | --- |
| `httpx` | Gemini `generateContent`, Gmail REST, `POST /listen` e `/speak` verso Windows |
| `pydantic` / `pydantic-settings` / `python-dotenv` | Config e `.env` (`GEMINI_API_KEY`, `AUDIO_DRIVER`, …) |
| `rich` | Console nel driver mock |
| `pypdf` | Testo dai PDF del workspace |
| `rapidfuzz` | Nomi file e mittenti |
| `chromadb` | RAG (collection unica sotto `INDEX_ROOT`) |
| `google-auth` / `google-auth-oauthlib` | Mailbox già collegata; consenso a tavolino |
| `tavily-python` | Ricerca web |
| extra `dev`: `pytest`, `ruff` | Test e lint in WSL (HTTP mockato, niente WinRT) |

Niente SDK Gemini ufficiale: il client è HTTP. Niente `edge-tts` / `pygame` / `vosk` qui: il loop con `AUDIO_DRIVER=http` è solo client; con `mock` è la tastiera.

Chiavi in `.env` lato WSL: almeno `GEMINI_API_KEY`. Gmail e Tavily al primo `ask_*`. Ollama (studio): [docs/ollama.md](docs/ollama.md).

### Host Windows (`windows_audio`)

Serve solo per il loop vocale reale (`AUDIO_DRIVER=http`). Si esegue con **Python Windows** (`py -3.12`), da un clone su disco `C:\` (non `\\wsl$\`). WinRT, .NET e microfono: [Prerequisiti di sistema](#prerequisiti-di-sistema). Procedura completa: [docs/audio_host.md](docs/audio_host.md).

Pacchetti Python (venv **Win32**, cartella `windows_audio/`):

```powershell
cd C:\percorso\lavora_e_guida\windows_audio
py -3.12 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python server.py
```

| Pacchetto (`windows_audio/requirements.txt`) | Ruolo sull’host |
| --- | --- |
| `edge-tts` | Sintesi neurale Microsoft (`it-IT-ElsaNeural`); serve rete |
| `pygame` | Playback MP3 fino alla fine (mutex half-duplex). Il wheel Windows porta SDL_mixer |
| `vosk` | Dettatura italiana locale dopo la wake WinRT |
| `sounddevice` | Mic WASAPI verso Vosk. Il wheel Windows porta di solito PortAudio |
| `numpy` | Buffer audio per Vosk |
| `mutagen` | Durata MP3; se manca si stima dal bitrate |

Al primo ascolto Vosk scarica da solo `vosk-model-small-it-0.22` (~50 MB) in `%LOCALAPPDATA%\lavora_e_guida\`. WSL non deve lanciare `server.py` (fail-fast, codice 2).

## Quick start (Mock, senza microfono)

Solo WSL: host audio non serve.

```bash
source .venv/bin/activate
pip install -e ".[dev]"
# Serve GEMINI_API_KEY in `.env`
lavora-e-guida
# Digita una frase (file, posta o ricerca), poi `esci`.
```

Equivalente: `python -m lavora_e_guida`.

| Agente | Comando | Cosa fa |
| --- | --- | --- |
| master (default) | `lavora-e-guida` | Router: smista a FS, Gmail o web. Workspace + RAG all’avvio; Gmail e Tavily si chiedono al primo `ask_*` |
| fs | `lavora-e-guida --agent fs` | Specialista file e RAG sul Desktop: crea, aggiorna, legge, cerca |
| gmail | `lavora-e-guida --agent gmail` | Mailbox: lettura, allegati, invio con conferma vocale |
| web | `lavora-e-guida --agent web` | Ricerca online via Tavily, riassunto parlato con le fonti |

Flusso composto (stesso enunciato, due round del master): «Cerca il meteo di Roma e mandalo a mario@x.it» → `ask_web` poi `ask_gmail` con destinatario e testo trovato. Nessun file sul Desktop se non è stato chiesto. La conferma di invio (sì/no) resta sul loop esterno.

`--llm ollama` sul loop vocale non avvia il 3B: messaggio parlato e uscita.

## Workspace e indice

| Concetto | Path | Ruolo |
| --- | --- | --- |
| Data workspace | `WORKSPACE_ROOT` (default Desktop `Ollama_test`) | File utente: note, PDF, create/read/append |
| Stato locale | `INDEX_ROOT` (`runtime/` nel repo) | SQLite + Chroma, telemetria STT, token Gmail; path in DB relativi al data workspace |

Dettagli Ollama (studio, daemon, variabili): [docs/ollama.md](docs/ollama.md).

Connessione Gmail (OAuth Desktop a tavolino, non nel loop vocale): [docs/gmail_oauth.md](docs/gmail_oauth.md).

Ricerca web (chiave Tavily, tool `web_search`, costo in crediti): [docs/tavily_web.md](docs/tavily_web.md).

Host audio Windows (install, rete, troubleshooting): [docs/audio_host.md](docs/audio_host.md). OS: [Prerequisiti](#prerequisiti-di-sistema). Pacchetti pip Host vs WSL: [Ambienti](#ambienti-wsl2-vs-host-windows).

Mappe Mermaid del runtime (avvio, loop, master, audio, Gmail, RAG): [docs/flowchart.md](docs/flowchart.md).

## Test

```bash
.venv/bin/pytest -q
```
