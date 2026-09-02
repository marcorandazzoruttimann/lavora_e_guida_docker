"""Specialista filesystem/RAG: tool sul Desktop (notes/, inbox/).

Isolato come `gmail/` e `web/`: il motore del loop (`agent.py`) non importa
questo pacchetto a livello di modulo. Lo spec vocale è in `fs.agent`;
l'implementazione di dominio è qui (`files.py`, `find.py`, `file_resolver.py`),
non più in `tools/`. `rag/` resta la libreria indice (sync all'avvio + find_file).
"""

from lavora_e_guida.fs.file_resolver import resolve_file_path
from lavora_e_guida.fs.files import (
    FsToolError,
    append_note,
    create_text_file,
    ensure_workspace,
    read_file,
)
from lavora_e_guida.fs.find import FindToolError, find_file

__all__ = [
    "FindToolError",
    "FsToolError",
    "append_note",
    "create_text_file",
    "ensure_workspace",
    "find_file",
    "read_file",
    "resolve_file_path",
]
