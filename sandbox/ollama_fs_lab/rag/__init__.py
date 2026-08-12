"""Indice RAG del lab: SQLite (hash file) + ChromaDB (chunk semantici).

Import tipico:
`from sandbox.ollama_fs_lab.rag import sync_workspace_index`
"""

from __future__ import annotations

from sandbox.ollama_fs_lab.rag.index_sync import (
    SyncStats,
    sync_workspace_index,
    upsert_indexed_file,
)

__all__ = [
    "SyncStats",
    "sync_workspace_index",
    "upsert_indexed_file",
]
