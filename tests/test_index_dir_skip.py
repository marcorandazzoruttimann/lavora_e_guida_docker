"""Test ensure_index_dir: crea la cartella indice RAG nel repo."""

from __future__ import annotations

from pathlib import Path

from sandbox.ollama_fs_lab.rag.index_db import (
    LEGACY_WORKSPACE_INDEX_NAMES,
    ensure_index_dir,
    log_legacy_workspace_index_hint,
)


def test_ensure_index_dir_creates_target(tmp_path: Path) -> None:
    """ensure_index_dir crea la directory se non esiste e ritorna il path risolto."""
    # Simula INDEX_ROOT come sotto-cartella in tmp_path.
    target = tmp_path / "ollama_lab"
    result = ensure_index_dir(target)
    assert result == target.resolve()
    assert result.is_dir()


def test_ensure_index_dir_idempotent(tmp_path: Path) -> None:
    """Chiamate ripetute non falliscono e ritornano lo stesso path."""
    target = tmp_path / "ollama_lab"
    first = ensure_index_dir(target)
    # Aggiungiamo un file per verificare che non venga perso.
    (first / "files.db").write_text("placeholder", encoding="utf-8")
    second = ensure_index_dir(target)
    assert first == second
    assert (second / "files.db").is_file()


def test_log_legacy_hint_when_desktop_has_old_index(
    tmp_path: Path,
    caplog: object,
) -> None:
    """Se il data workspace ha ollama_lab/ e l'indice repo è vuoto → log warning."""
    # Simula data workspace con cartella indice legacy.
    data_ws = tmp_path / "desktop"
    data_ws.mkdir()
    for name in LEGACY_WORKSPACE_INDEX_NAMES:
        legacy = data_ws / name
        legacy.mkdir()
        (legacy / "files.db").write_text("old", encoding="utf-8")

    # Indice repo vuoto.
    idx = tmp_path / "repo_index"
    idx.mkdir()

    import logging

    with caplog.at_level(logging.WARNING):  # type: ignore[union-attr]
        log_legacy_workspace_index_hint(data_ws, idx)
    # Almeno un warning emesso sulla migrazione.
    assert any("legacy" in r.message for r in caplog.records)  # type: ignore[union-attr]
