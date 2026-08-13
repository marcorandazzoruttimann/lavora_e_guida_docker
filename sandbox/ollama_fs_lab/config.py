"""Costanti del lab Ollama FS (data workspace Desktop + indice RAG nel repo).

Due root distinti:
- WORKSPACE_ROOT: file utente (note, PDF, create/read/append) sul Desktop Windows.
- INDEX_ROOT: SQLite + Chroma nel progetto Cursor (path indicizzati restano relativi al data workspace).
  Telemetria token STT: `TELEMETRY_DB` = INDEX_ROOT / telemetry.db (file dedicato, non files.db).

Solo path e URL fissi: niente email, niente Cursor, niente framework multi-agente.
"""

from __future__ import annotations

import os
from pathlib import Path

# Root del repo lavora_e_guida (due livelli sopra sandbox/ollama_fs_lab/config.py).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Data workspace: cartella Desktop Windows montata in WSL.
# File utente indicizzati e manipolati dagli tool FS; path traversal oltre questo root è rifiutato.
# Al primo avvio `ensure_workspace()` crea anche notes/ e inbox/ sotto questo root.
WORKSPACE_ROOT = Path("/mnt/c/Users/User/Desktop/Ollama_test")

# Indice RAG persistente nel progetto (non sul Desktop).
# Contiene files.db (metadati) e chroma/ (embedding); i path in DB restano relativi a WORKSPACE_ROOT.
INDEX_ROOT = PROJECT_ROOT / "ollama_lab"

# Telemetria token per turno STT: SQLite dedicato, separato dall'indice RAG.
TELEMETRY_DB = INDEX_ROOT / "telemetry.db"

# Daemon verificato in Step 0: Ollama gira in WSL su loopback, non sull'host Windows.
# (Host da /etc/resolv.conf:11434 non risponde; processo `ollama serve` in WSL.)
OLLAMA_URL = "http://127.0.0.1:11434"

# Modello di lab: tag già pullato e presente in `ollama list` (Step 0).
OLLAMA_MODEL = "qwen2.5:3b"

# Default cloud per `--llm gemini` (override CLI `--model` o env GEMINI_MODEL).
# Chiave API: GEMINI_API_KEY da ambiente / .env (vedi .env.example).
GEMINI_MODEL = (os.environ.get("GEMINI_MODEL") or "").strip() or "gemini-3.5-flash"
