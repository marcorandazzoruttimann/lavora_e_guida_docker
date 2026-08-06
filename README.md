# Lavora e Guida

Ecosistema agentico vocale hands-free: orchestratore in WSL2, I/O audio sul Windows host.

## Stato fasi

| Fase | Stato |
|------|--------|
| Phase 0 — Bootstrap | fatto |
| Phase 1 — Audio Mock/HTTP | fatto |
| Phase 2 — Ollama + state machine + intent stub | fatto |
| Phase 3+ | non iniziata |

## Quick start (Mock, senza microfono)

```bash
source .venv/bin/activate
pip install -e ".[dev]"
lavora-e-guida
# Digita una frase, poi `esci`.
```

## Ollama (Phase 2)

Vedi [docs/ollama.md](docs/ollama.md). Benchmark intent:

```bash
.venv/bin/python scripts/bench_ollama_intent.py
```

Topologia agenti (checkpoint 2.4): [docs/phase2_topology.md](docs/phase2_topology.md).

## Test

```bash
.venv/bin/pytest -q
```
