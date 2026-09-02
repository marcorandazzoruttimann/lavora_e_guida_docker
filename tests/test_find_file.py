"""Test tool find_file: query semantica → path + chunk."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("chromadb")

from lavora_e_guida.config import EMAIL_ATTACHMENTS_DIRNAME
from lavora_e_guida.fs.find import FindToolError, find_file
from lavora_e_guida.rag.index_sync import sync_workspace_index


def _seed_two_notes(data_ws: Path, index_root: Path) -> None:
    """Due note con contenuti distinti per discriminare la query semantica."""
    notes = data_ws / "notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "spesa.txt").write_text(
        "lista della spesa: latte, pane, uova, cetrioli\n",
        encoding="utf-8",
    )
    (notes / "vacanza.txt").write_text(
        "piano vacanza estate: mare, spiaggia, hotel a Rimini\n",
        encoding="utf-8",
    )
    # Indicizza dal data workspace verso l'indice separato.
    sync_workspace_index(data_ws, index_root=index_root)


@pytest.fixture
def dual_paths(tmp_path: Path) -> tuple[Path, Path]:
    """Data workspace e index root separati, come in produzione."""
    data_ws = tmp_path / "data"
    data_ws.mkdir()
    index_root = tmp_path / "index"
    index_root.mkdir()
    return data_ws, index_root


def test_find_file_returns_path_and_chunk(dual_paths: tuple[Path, Path]) -> None:
    """Query su cetrioli punta a spesa.txt e include il chunk."""
    data_ws, index_root = dual_paths
    _seed_two_notes(data_ws, index_root)
    out = find_file("dove ho scritto dei cetrioli", workspace=data_ws, index_root=index_root)

    assert out.startswith("OK: trovato notes/spesa.txt")
    assert "---" in out
    _header, chunk_body = out.split("\n---\n", 1)
    chunk_text = chunk_body.split("\nAltri file:", 1)[0]
    assert "cetrioli" in chunk_text.lower()
    assert "vacanza" not in chunk_text.lower()


def test_find_file_prefers_vacation_note_for_mare_query(dual_paths: tuple[Path, Path]) -> None:
    """Query su mare/spiaggia → nota vacanza, non spesa."""
    data_ws, index_root = dual_paths
    _seed_two_notes(data_ws, index_root)
    out = find_file("informazioni sulla spiaggia e il mare", workspace=data_ws, index_root=index_root)

    assert "notes/vacanza.txt" in out
    assert "Rimini" in out or "spiaggia" in out.lower()


def test_find_file_empty_query_raises(dual_paths: tuple[Path, Path]) -> None:
    """Query blank → FindToolError parlante."""
    data_ws, index_root = dual_paths
    _seed_two_notes(data_ws, index_root)
    with pytest.raises(FindToolError, match="query vuota"):
        find_file("   ", workspace=data_ws, index_root=index_root)


def test_find_file_no_index_yet(dual_paths: tuple[Path, Path]) -> None:
    """Workspace vuoto: sync lazy ma nessun hit → errore."""
    data_ws, index_root = dual_paths
    (data_ws / "notes").mkdir()
    with pytest.raises(FindToolError, match="nessun file"):
        find_file("qualcosa", workspace=data_ws, index_root=index_root)


def test_find_file_ignores_email_attachments(dual_paths: tuple[Path, Path]) -> None:
    """Query da fattura Gmail non deve restituire il file sotto email_attachments/."""
    data_ws, index_root = dual_paths
    notes = data_ws / "notes"
    notes.mkdir()
    (notes / "spesa.txt").write_text("lista spesa latte pane uova\n", encoding="utf-8")
    day = data_ws / EMAIL_ATTACHMENTS_DIRNAME / "2026-08-19"
    day.mkdir(parents=True)
    (day / "fattura.txt").write_text(
        "fattura acme importo centoventi euro scadenza agosto\n",
        encoding="utf-8",
    )
    try:
        out = find_file(
            "fattura acme importo centoventi",
            workspace=data_ws,
            index_root=index_root,
        )
    except FindToolError as exc:
        # Nessun hit utile sulle note: l'allegato non deve comparire nemmeno nell'errore.
        assert EMAIL_ATTACHMENTS_DIRNAME not in str(exc)
        return
    assert EMAIL_ATTACHMENTS_DIRNAME not in out
    assert "fattura acme" not in out.lower()
