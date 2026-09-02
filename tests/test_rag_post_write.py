"""Test hook post-scrittura: create/append aggiornano l'indice RAG."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from lavora_e_guida.fs.files import append_note, create_text_file
from lavora_e_guida.rag.index_db import IndexDB


@pytest.fixture
def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Reindirizza WORKSPACE_ROOT e INDEX_ROOT al tmp_path per i test FS."""
    import lavora_e_guida.fs.files as fs_files

    # Data workspace: dove i tool creano i file.
    data_ws = tmp_path / "data"
    data_ws.mkdir()
    monkeypatch.setattr(fs_files, "WORKSPACE_ROOT", data_ws)

    # Indice RAG: separato dal data workspace, come in produzione.
    index_root = tmp_path / "index"
    index_root.mkdir()
    monkeypatch.setattr(fs_files, "INDEX_ROOT", index_root)

    (data_ws / "notes").mkdir()
    (data_ws / "inbox").mkdir()
    return data_ws


def test_create_text_file_triggers_index_upsert(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    """Dopo create_text_file la riga SQLite esiste per il path creato."""
    out = create_text_file("prova_rag.txt", "contenuto indicizzabile\n")
    assert out.startswith("OK:")

    index_root = tmp_path / "index"
    with IndexDB(index_root) as db:
        rec = db.get_file("prova_rag.txt")
        assert rec is not None
        assert db.list_chunk_ids("prova_rag.txt")


def test_append_note_triggers_index_upsert(
    workspace_root: Path,
    tmp_path: Path,
) -> None:
    """Dopo append_note la nota è presente nell'indice SQLite."""
    out = append_note("spesa", "latte\n")
    assert "notes/spesa.txt" in out

    index_root = tmp_path / "index"
    with IndexDB(index_root) as db:
        rec = db.get_file("notes/spesa.txt")
        assert rec is not None
