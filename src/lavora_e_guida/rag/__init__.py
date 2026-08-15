"""Indice RAG: SQLite (hash file) + ChromaDB (chunk semantici).

Import tipico:
`from lavora_e_guida.rag import sync_workspace_index`
"""

from __future__ import annotations

from lavora_e_guida.rag.index_sync import (
    SyncStats,
    sync_workspace_index,
    upsert_indexed_file,
)

__all__ = [
    "SyncStats",
    "sync_workspace_index",
    "upsert_indexed_file",
]
