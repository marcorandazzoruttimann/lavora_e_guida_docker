"""Costanti del lab Ollama FS (workspace Desktop + daemon locale).

Solo path e URL fissi: niente email, niente Cursor, niente framework multi-agente.
I file creati dagli step successivi restano sotto WORKSPACE_ROOT (Desktop Windows).
"""

from __future__ import annotations

from pathlib import Path

# Root FS del lab: cartella Desktop Windows montata in WSL.
# Tutto ciò che è fuori da qui deve essere rifiutato (path traversal).
# Al primo avvio `ensure_workspace()` crea anche notes/ e inbox/ sotto questo root.
WORKSPACE_ROOT = Path("/mnt/c/Users/User/Desktop/Ollama_test")

# Daemon verificato in Step 0: Ollama gira in WSL su loopback, non sull'host Windows.
# (Host da /etc/resolv.conf:11434 non risponde; processo `ollama serve` in WSL.)
OLLAMA_URL = "http://127.0.0.1:11434"

# Modello di lab: tag già pullato e presente in `ollama list` (Step 0).
OLLAMA_MODEL = "qwen2.5:3b"
