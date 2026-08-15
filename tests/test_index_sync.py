"""Test sync workspace → SQLite hash + Chroma chunks (dual-path: data + index)."""

from __future__ import annotations

from pathlib import Path

import pytest

chromadb = pytest.importorskip("chromadb")

from lavora_e_guida.rag.chroma_store import ChromaStore
from lavora_e_guida.rag.index_db import IndexDB, db_path
from lavora_e_guida.rag.index_sync import (
    file_content_hash,
    sync_workspace_index,
    upsert_indexed_file,
)


@pytest.fixture
def dual_paths(tmp_path: Path) -> tuple[Path, Path]:
    """Data workspace e index root separati, come in produzione."""
    data_ws = tmp_path / "data"
    data_ws.mkdir()
    index_root = tmp_path / "index"
    index_root.mkdir()
    return data_ws, index_root


def test_sync_creates_sqlite_and_chroma_chunks(dual_paths: tuple[Path, Path]) -> None:
    """Nuovo file .txt → riga files + almeno un chunk in Chroma."""
    data_ws, index_root = dual_paths
    notes = data_ws / "notes"
    notes.mkdir()
    note = notes / "spesa.txt"
    note.write_text("latte\npane\nuova\n", encoding="utf-8")

    stats = sync_workspace_index(data_ws, index_root=index_root)
    assert stats.scanned == 1
    assert stats.upserted == 1
    assert stats.deleted == 0
    assert db_path(index_root).is_file()

    with IndexDB(index_root) as db:
        rec = db.get_file("notes/spesa.txt")
        assert rec is not None
        assert rec.content_hash == file_content_hash(note)
        chunk_ids = db.list_chunk_ids("notes/spesa.txt")
        assert len(chunk_ids) >= 1

    store = ChromaStore(index_root)
    hits = store.query("latte", n_results=1)
    assert hits
    assert hits[0].rel_path == "notes/spesa.txt"
    assert "latte" in hits[0].document.lower()


def test_sync_skips_unchanged_hash(dual_paths: tuple[Path, Path]) -> None:
    """Secondo sync senza modifiche → skipped_unchanged, niente re-upsert."""
    data_ws, index_root = dual_paths
    note = data_ws / "lista.txt"
    note.write_text("contenuto fisso\n", encoding="utf-8")

    first = sync_workspace_index(data_ws, index_root=index_root)
    assert first.upserted == 1

    second = sync_workspace_index(data_ws, index_root=index_root)
    assert second.skipped_unchanged == 1
    assert second.upserted == 0


def test_sync_reindexes_on_content_change(dual_paths: tuple[Path, Path]) -> None:
    """Modifica contenuto → hash nuovo e upsert di nuovo."""
    data_ws, index_root = dual_paths
    note = data_ws / "notes" / "diario.txt"
    note.parent.mkdir(parents=True)
    note.write_text("versione uno\n", encoding="utf-8")

    sync_workspace_index(data_ws, index_root=index_root)
    with IndexDB(index_root) as db:
        hash_v1 = db.get_file("notes/diario.txt")
        assert hash_v1 is not None
        h1 = hash_v1.content_hash

    note.write_text("versione due con cetrioli\n", encoding="utf-8")
    stats = sync_workspace_index(data_ws, index_root=index_root)
    assert stats.upserted == 1

    with IndexDB(index_root) as db:
        hash_v2 = db.get_file("notes/diario.txt")
        assert hash_v2 is not None
        assert hash_v2.content_hash != h1


def test_sync_deletes_orphan_from_index(dual_paths: tuple[Path, Path]) -> None:
    """File rimosso dal disco → rimosso da SQLite e Chroma."""
    data_ws, index_root = dual_paths
    note = data_ws / "temp.txt"
    note.write_text("da cancellare\n", encoding="utf-8")
    sync_workspace_index(data_ws, index_root=index_root)

    note.unlink()
    stats = sync_workspace_index(data_ws, index_root=index_root)
    assert stats.deleted == 1

    with IndexDB(index_root) as db:
        assert db.get_file("temp.txt") is None

    store = ChromaStore(index_root)
    assert store.query("cancellare", n_results=1) == []


def test_upsert_single_file_after_write(dual_paths: tuple[Path, Path]) -> None:
    """Hook post-scrittura: upsert_indexed_file indicizza un solo path."""
    data_ws, index_root = dual_paths
    note = data_ws / "notes" / "hook.txt"
    note.parent.mkdir(parents=True)
    note.write_text("sync mirato\n", encoding="utf-8")

    changed = upsert_indexed_file(data_ws, "notes/hook.txt", index_root=index_root)
    assert changed is True

    with IndexDB(index_root) as db:
        assert db.get_file("notes/hook.txt") is not None

    changed_again = upsert_indexed_file(data_ws, "notes/hook.txt", index_root=index_root)
    assert changed_again is False
