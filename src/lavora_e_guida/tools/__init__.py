"""Catalogo tool condiviso: declaration Gemini, non implementazioni di dominio.

Le implementazioni FS vivono in `fs/` (come `gmail/read.py` e `web/search.py`).
Questo pacchetto espone solo il contratto `ToolDeclaration` / `to_gemini_tools`
usato da tutti gli specialisti per le `functionDeclarations`.
"""

from lavora_e_guida.tools.catalog import ToolDeclaration, to_gemini_tools

__all__ = [
    "ToolDeclaration",
    "to_gemini_tools",
]
