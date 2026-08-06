# Setup Ollama (Phase 2.1)

## Dove gira

| Opzione | URL tipico | Note |
|---------|------------|------|
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
```

## Variabili `.env`

```env
OLLAMA_MODEL=qwen2.5:3b
# OLLAMA_BASE_URL=http://127.0.0.1:11434
```

Se WSL ha ~5 Gi RAM e `qwen2.5:3b` provoca swap pesante, usare `gemma2:2b`.

## Benchmark intent

```bash
.venv/bin/python scripts/bench_ollama_intent.py
```

Scrive `docs/bench_ollama_intent.json` e stampa una raccomandazione. Aggiornare `OLLAMA_MODEL` solo dopo review.

## Port mirror (Host → WSL)

Se Ollama gira su Windows e WSL non raggiunge `localhost:11434` del host:

1. Scopri IP host: nameserver in `/etc/resolv.conf` (stesso meccanismo di `WINDOWS_HOST`).
2. Su Windows, assicurati che Ollama ascolti su `0.0.0.0` (non solo loopback).
3. Imposta `OLLAMA_BASE_URL=http://<ip-host>:11434`.
