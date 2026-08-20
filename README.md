# Lavora e Guida

Ecosistema agentico vocale hands-free: orchestratore in WSL2, I/O audio sul Windows host.

Loop attuale: Mock/HTTP STT → Gemini (default) o Ollama (backup) → tool filesystem/RAG sul Desktop → TTS.

## Quick start (Mock, senza microfono)

```bash
source .venv/bin/activate
pip install -e ".[dev]"
# Serve GEMINI_API_KEY in `.env`
lavora-e-guida
# Digita una frase (crea/aggiorna/leggi/cerca file), poi `esci`.
```

Equivalente: `python -m lavora_e_guida`. Backup locale: `lavora-e-guida --llm ollama`. Gmail: `lavora-e-guida --agent gmail`.

## Workspace e indice

| Concetto | Path | Ruolo |
| --- | --- | --- |
| Data workspace | `WORKSPACE_ROOT` (default Desktop `Ollama_test`) | File utente: note, PDF, create/read/append |
| Stato locale | `INDEX_ROOT` (`runtime/` nel repo) | SQLite + Chroma, telemetria STT, token Gmail; path in DB relativi al data workspace |

Dettagli Ollama, modelli e variabili: [docs/ollama.md](docs/ollama.md).

Connessione Gmail (OAuth Desktop a tavolino, non nel loop vocale): [docs/gmail_oauth.md](docs/gmail_oauth.md).

## Test

```bash
.venv/bin/pytest -q
```
