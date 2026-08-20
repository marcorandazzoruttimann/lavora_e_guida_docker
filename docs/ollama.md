# Ollama e avvio vocale

## Dove gira il daemon

| Opzione | URL tipico | Note |
| --- | --- | --- |
| **WSL2 (scelta attuale)** | `http://127.0.0.1:11434` | Install con `curl -fsSL https://ollama.com/install.sh \| sh` (serve `zstd`) |
| **Host Windows** | `http://<WINDOWS_HOST>:11434` | Esporre Ollama sulla LAN/WSL; in `.env` impostare `OLLAMA_BASE_URL` |

Un solo modello caricato alla volta: hardware target Ryzen 3 / poca RAM.

## Prerequisiti install (WSL)

```bash
sudo apt-get update && sudo apt-get install -y zstd
curl -fsSL https://ollama.com/install.sh | sh
# Se systemd non avvia il servizio:
#   ollama serve &
ollama pull gemma2:2b
ollama pull qwen2.5:3b
```

Verifica:

```bash
curl -sS http://127.0.0.1:11434/api/tags | python3 -m json.tool
ollama list
```

Se il ping fallisce: avviare il daemon in WSL con `ollama serve` (non confondere con Ollama su Windows). L'host Windows `:11434` in questo ambiente non è raggiungibile.

## Variabili `.env`

```env
OLLAMA_MODEL=qwen2.5:3b
# OLLAMA_BASE_URL=http://127.0.0.1:11434
AUDIO_DRIVER=mock
# WORKSPACE_ROOT=/mnt/c/Users/User/Desktop/Ollama_test
# INDEX_ROOT=   # default: <repo>/runtime
# GEMINI_API_KEY=
# GEMINI_MODEL=gemini-3.5-flash
GMAIL_CLIENT_ID=
GMAIL_CLIENT_SECRET=
GMAIL_USER=account@gmail.com
# GMAIL_TOKEN_FILE=   # default: INDEX_ROOT/gmail_token.json
```

Gmail: consenso OAuth a tavolino (non nel loop vocale). Dettagli in [docs/gmail_oauth.md](gmail_oauth.md).

Se WSL ha ~5 Gi RAM e `qwen2.5:3b` provoca swap pesante, usare `gemma2:2b`.

## Avvio

```bash
# dalla root del repo, venv attivo (default: Gemini, serve GEMINI_API_KEY)
lavora-e-guida
# oppure
python -m lavora_e_guida

# Backup locale Qwen/Ollama
lavora-e-guida --llm ollama

# Override modello Gemini
lavora-e-guida --model gemini-3.5-flash
```

Banner su stderr: `provider=… modello=… data=… index=…`. Digiti la frase dopo `Tu (mock STT)>`; la risposta compare come `[TTS] …`. Uscita: `esci` / `exit` / `quit`, Enter a vuoto, Ctrl+D.

Smoke non interattivo:

```bash
printf 'Ciao, rispondi in una frase.\nesci\n' | lavora-e-guida
```

Tool disponibili (JSON, un tool per turno): `create_text_file`, `append_note`, `read_file` (testo + PDF), `find_file` (RAG). All'avvio `ensure_workspace()` crea `notes/` e `inbox/` sotto `WORKSPACE_ROOT`.

## Port mirror (Host → WSL)

Se Ollama gira su Windows e WSL non raggiunge `localhost:11434` del host:

1. Scopri IP host: nameserver in `/etc/resolv.conf` (stesso meccanismo di `WINDOWS_HOST`).
2. Su Windows, assicurati che Ollama ascolti su `0.0.0.0` (non solo loopback).
3. Imposta `OLLAMA_BASE_URL=http://<ip-host>:11434`.
