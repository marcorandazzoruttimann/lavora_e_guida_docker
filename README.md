# Lavora e Guida

Ecosistema agentico vocale hands-free: orchestratore in WSL2, I/O audio sul Windows host.

Loop attuale: Mock/HTTP STT → Gemini (function calling nativo: `functionDeclarations` + `functionCall` / `parts[].text`) → router master (`ask_fs` / `ask_gmail` / `ask_web`) → specialista nested → TTS.

Qwen 2.5 3B (Ollama) è extra di studio: `--llm ollama` sul loop vocale è fail-fast parlante. Il modulo `local_ollama.py` può restare per prove isolate.

## Ambienti: WSL2 vs Host Windows

Due Python, due venv. Non mescolare e non fare `pip install -e .` dal `python.exe` Windows (né il contrario).

| Dove | File dipendenze | Cosa installa |
| --- | --- | --- |
| **WSL2** (orchestratore, default) | [`pyproject.toml`](pyproject.toml) | Gemini REST, RAG, Gmail, Tavily, client HTTP verso l’host audio |
| **Host Windows** (`windows_audio`) | [`windows_audio/requirements.txt`](windows_audio/requirements.txt) | TTS, playback, dettatura Vosk. **Non** è un extra di `pyproject.toml` |

### WSL2

Python 3.12+ in un venv Linux. Dalla root del repo:

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

Serve solo per il loop vocale reale (`AUDIO_DRIVER=http`). Si esegue con **Python Windows** (`py -3.12`), da un clone su disco `C:\` (non `\\wsl$\`). Procedura completa: [docs/audio_host.md](docs/audio_host.md).

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

Fuori da pip (una volta, a mano):

- **SDK .NET 8** (workload desktop) se non c’è già `stt_helper.exe` compilato: l’helper C# fa la wake WinRT su `:8766`. Non si compila da WSL.
- **Pacchetto lingua Italiano** e FOD Speech (`Language.Speech~~~it-IT`) per la wake list WinRT.
- Microfono predefinito e privacy «app desktop» accesa.
- Se `vosk` o `pygame` non caricano le DLL native: [Visual C++ Redistributable](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist) recente (x64).

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

Host audio Windows (install, rete, troubleshooting): [docs/audio_host.md](docs/audio_host.md). Dipendenze Host vs WSL: sezione [Ambienti](#ambienti-wsl2-vs-host-windows) qui sopra.

Mappe Mermaid del runtime (avvio, loop, master, audio, Gmail, RAG): [docs/flowchart.md](docs/flowchart.md).

## Test

```bash
.venv/bin/pytest -q
```
