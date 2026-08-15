# Lavora e Guida

Ecosistema agentico vocale hands-free: orchestratore in WSL2, I/O audio sul Windows host.

Loop attuale: Mock/HTTP STT → LLM locale (Ollama) o Gemini → tool filesystem/RAG sul Desktop → TTS.

## Quick start (Mock, senza microfono)

```bash
source .venv/bin/activate
pip install -e ".[dev]"
lavora-e-guida
# Digita una frase (crea/aggiorna/leggi/cerca file), poi `esci`.
```

Equivalente: `python -m lavora_e_guida`. Provider cloud: `lavora-e-guida --llm gemini` (serve `GEMINI_API_KEY` in `.env`).

## Workspace e indice

| Concetto | Path | Ruolo |
| --- | --- | --- |
| Data workspace | `WORKSPACE_ROOT` (default Desktop `Ollama_test`) | File utente: note, PDF, create/read/append |
| Indice RAG | `INDEX_ROOT` (`ollama_lab/` nel repo) | SQLite + Chroma; path in DB relativi al data workspace |

Dettagli Ollama, modelli e variabili: [docs/ollama.md](docs/ollama.md).

## Test

```bash
.venv/bin/pytest -q
```
