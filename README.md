# Lavora e Guida

Ecosistema agentico vocale hands-free: orchestratore in WSL2, I/O audio sul Windows host.

Loop attuale: Mock/HTTP STT → Gemini (function calling nativo: `functionDeclarations` + `functionCall` / `parts[].text`) → tool filesystem/RAG sul Desktop → TTS.

Qwen 2.5 3B (Ollama) è extra di studio: `--llm ollama` sul loop vocale è fail-fast parlante. Il modulo `local_ollama.py` può restare per prove isolate.

## Quick start (Mock, senza microfono)

```bash
source .venv/bin/activate
pip install -e ".[dev]"
# Serve GEMINI_API_KEY in `.env`
lavora-e-guida
# Digita una frase (crea/aggiorna/leggi/cerca file), poi `esci`.
```

Equivalente: `python -m lavora_e_guida`.

| Agente | Comando | Cosa fa |
| --- | --- | --- |
| master (default) | `lavora-e-guida` | File e RAG sul Desktop: crea, aggiorna, legge, cerca |
| gmail | `lavora-e-guida --agent gmail` | Mailbox: lettura, allegati, invio con conferma vocale |
| web | `lavora-e-guida --agent web` | Ricerca online via Tavily, riassunto parlato con le fonti |

`--llm ollama` sul loop vocale non avvia il 3B: messaggio parlato e uscita.

## Workspace e indice

| Concetto | Path | Ruolo |
| --- | --- | --- |
| Data workspace | `WORKSPACE_ROOT` (default Desktop `Ollama_test`) | File utente: note, PDF, create/read/append |
| Stato locale | `INDEX_ROOT` (`runtime/` nel repo) | SQLite + Chroma, telemetria STT, token Gmail; path in DB relativi al data workspace |

Dettagli Ollama (studio, daemon, variabili): [docs/ollama.md](docs/ollama.md).

Connessione Gmail (OAuth Desktop a tavolino, non nel loop vocale): [docs/gmail_oauth.md](docs/gmail_oauth.md).

Ricerca web (chiave Tavily, tool `web_search`, costo in crediti): [docs/tavily_web.md](docs/tavily_web.md).

## Test

```bash
.venv/bin/pytest -q
```
