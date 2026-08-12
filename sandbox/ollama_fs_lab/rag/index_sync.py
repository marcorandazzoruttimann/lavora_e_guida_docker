"""Sync data workspace Desktop → SQLite hash + Chroma chunks nel repo.

Algoritmo:
1. Lista file eleggibili sotto il data workspace (`rglob`).
2. Hash SHA-256 bytes grezzi; se uguale a SQLite → skip.
3. Altrimenti: estrai testo → chunk → upsert Chroma + aggiorna SQLite.
4. Path in SQLite assenti dal disco → delete chunk Chroma + row SQLite.

Chiamare `sync_workspace_index` all'avvio lab e dopo create/append;
`upsert_indexed_file` per sync mirato post-scrittura di un solo path.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sandbox.ollama_fs_lab.config import INDEX_ROOT
from sandbox.ollama_fs_lab.rag.chroma_store import ChromaStore
from sandbox.ollama_fs_lab.rag.chunking import chunk_id_for, split_text
from sandbox.ollama_fs_lab.rag.index_db import (
    ChunkRecord,
    IndexDB,
    log_legacy_workspace_index_hint,
)

# Tipi indicizzati in v1: testo UTF-8 + PDF estraibile (niente .docx).
INDEXABLE_SUFFIXES: frozenset[str] = frozenset({".txt", ".md", ".json", ".pdf"})

# Stesso tetto del lab `read_file` per PDF: non saturare embed/CPU su PDF enormi.
_MAX_PDF_CHARS = 8000

logger = logging.getLogger(__name__)


@dataclass
class SyncStats:
    """Contatori di un giro di sync (debug / test / log avvio)."""

    scanned: int = 0
    skipped_unchanged: int = 0
    upserted: int = 0
    deleted: int = 0
    errors: list[str] = field(default_factory=list)


def iter_indexable_files(data_workspace: Path) -> list[Path]:
    """File regolari eleggibili sotto il data workspace (path relativi posix-ready).

    Ritorna path *assoluti* risolti; il caller deriva `rel_path` con relative_to.
    L'indice RAG vive nel repo (INDEX_ROOT), non sotto il data workspace.
    """
    root = Path(data_workspace).resolve()
    if not root.is_dir():
        return []

    found: list[Path] = []
    # rglob ricorsivo: notes/, inbox/, eventuali sotto-cartelle utente.
    for abs_path in root.rglob("*"):
        if not abs_path.is_file():
            continue
        try:
            abs_path.relative_to(root)
        except ValueError:
            # Symlink fuori root: non candidato sicuro.
            continue
        # Solo suffix ammessi (casefold: `.TXT` su FS Windows montato).
        if abs_path.suffix.lower() not in INDEXABLE_SUFFIXES:
            continue
        found.append(abs_path)
    return found


def file_content_hash(path: Path) -> str:
    """SHA-256 hex del contenuto grezzo (bytes) — chiave skip re-embed."""
    h = hashlib.sha256()
    # Lettura a blocchi: PDF/inbox grandi senza caricare tutto in RAM.
    with path.open("rb") as fh:
        while True:
            block = fh.read(1024 * 1024)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def extract_indexable_text(path: Path) -> str:
    """Estrae testo UTF-8 (o PDF via pypdf) da indicizzare; stringa vuota se fail.

    Non solleva verso il sync globale: un file corrotto non blocca gli altri.
    """
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf_text_quiet(path)
    # Testo: errors=replace così byte sporchi STT/export non abortiscono sync.
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("lettura fallita %s: %s", path, exc)
        return ""


def _extract_pdf_text_quiet(path: Path) -> str:
    """Estrae testo PDF con pypdf; fallimenti → stringa vuota + log."""
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf assente: skip PDF %s", path)
        return ""

    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # noqa: BLE001 — PDF corrotti variano
        logger.warning("PDF non leggibile %s: %s", path, exc)
        return ""

    # Cifrati: non tentiamo password; skip silenzioso per il batch.
    if getattr(reader, "is_encrypted", False):
        logger.warning("PDF cifrato, skip: %s", path)
        return ""

    parts: list[str] = []
    for page in reader.pages:
        chunk = page.extract_text() or ""
        if chunk.strip():
            parts.append(chunk)
    text = "\n\n".join(parts).strip()
    # Tetto lab: allinea read_file / indice (niente OCR, niente PDF giganti).
    if _MAX_PDF_CHARS > 0 and len(text) > _MAX_PDF_CHARS:
        text = text[:_MAX_PDF_CHARS]
    return text


def sync_workspace_index(
    data_workspace: Path,
    *,
    index_root: Path = INDEX_ROOT,
) -> SyncStats:
    """Sincronizza il data workspace: upsert nuovi/modificati, delete orfani.

    Side-effect: crea `index_root/`, scrive `files.db` e directory `chroma/`.
    I path in SQLite restano relativi a `data_workspace`.
    """
    root = Path(data_workspace).resolve()
    idx = Path(index_root).resolve()
    stats = SyncStats()
    log_legacy_workspace_index_hint(root, idx)
    db = IndexDB(idx)
    store = ChromaStore(idx)

    try:
        db.connect()
        # Apri Chroma subito: fallisce early se manca chromadb.
        store.connect()

        on_disk: set[str] = set()
        for abs_path in iter_indexable_files(root):
            stats.scanned += 1
            rel_path = abs_path.relative_to(root).as_posix()
            on_disk.add(rel_path)
            try:
                changed = _sync_one_file(db, store, abs_path, rel_path)
            except Exception as exc:
                # Un file corrotto non deve bloccare l'intero batch di sync.
                stats.errors.append(f"{rel_path}: {exc}")
                logger.exception("sync fallito per %s", rel_path)
                continue
            if changed:
                stats.upserted += 1
            else:
                stats.skipped_unchanged += 1

        # Orfani: in SQLite ma non più sul disco → pulizia Chroma + SQLite.
        for stale in db.list_file_paths() - on_disk:
            try:
                _delete_indexed_file(db, store, stale)
                stats.deleted += 1
            except Exception as exc:
                stats.errors.append(f"delete {stale}: {exc}")
                logger.exception("delete indice fallito per %s", stale)
    finally:
        db.close()

    return stats


def upsert_indexed_file(
    data_workspace: Path,
    rel_path: str,
    *,
    index_root: Path = INDEX_ROOT,
) -> bool:
    """Indicizza (o re-indicizza) un singolo path relativo al data workspace.

    Pensato per hook post-`create_text_file` / `append_note`.
    Ritorna True se ha scritto chunk; False se hash invariato o file assente.
    """
    root = Path(data_workspace).resolve()
    idx = Path(index_root).resolve()
    # Normalizza: niente leading slash / backslash Windows nel mirror SQLite.
    rel = Path(rel_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"rel_path non sicuro per indice: {rel_path!r}")
    rel_posix = rel.as_posix()

    abs_path = (root / rel).resolve()
    # Fuori workspace (symlink / traversal): rifiuta.
    try:
        abs_path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path fuori workspace: {rel_path!r}") from exc

    if not abs_path.is_file():
        # File cancellato subito dopo write: allinea indice come orphan.
        with IndexDB(idx) as db:
            store = ChromaStore(idx)
            if db.get_file(rel_posix) is not None:
                _delete_indexed_file(db, store, rel_posix)
        return False

    if abs_path.suffix.lower() not in INDEXABLE_SUFFIXES:
        return False

    with IndexDB(idx) as db:
        store = ChromaStore(idx)
        return _sync_one_file(db, store, abs_path, rel_posix)


def _sync_one_file(
    db: IndexDB,
    store: ChromaStore,
    abs_path: Path,
    rel_path: str,
) -> bool:
    """Sync di un file; True se Chroma/SQLite aggiornati, False se skip hash."""
    content_hash = file_content_hash(abs_path)
    existing = db.get_file(rel_path)
    # Hash uguale: niente re-embed (CPU-bound sul MiniLM ONNX).
    if existing is not None and existing.content_hash == content_hash:
        return False

    text = extract_indexable_text(abs_path)
    pieces = split_text(text)
    # File vuoto / PDF senza testo: comunque aggiorna SQLite e rimuovi chunk vecchi
    # così non restano embedding di contenuti precedenti.
    new_ids: list[str] = []
    documents: list[str] = []
    metadatas: list[dict[str, str | int]] = []
    records: list[ChunkRecord] = []

    for idx, piece in enumerate(pieces):
        cid = chunk_id_for(rel_path, idx)
        new_ids.append(cid)
        documents.append(piece)
        # Metadata contratto piano + find_file.
        metadatas.append(
            {
                "rel_path": rel_path,
                "chunk_index": idx,
                "content_hash": content_hash,
            }
        )
        records.append(
            ChunkRecord(
                chunk_id=cid,
                rel_path=rel_path,
                chunk_index=idx,
                content_hash=content_hash,
            )
        )

    # SQLite: sostituisci chunk e ottieni id vecchi da cancellare in Chroma.
    old_ids = db.replace_chunks(rel_path, records)
    # Cancella id obsoleti (re-index con numero chunk diverso).
    stale = [i for i in old_ids if i not in set(new_ids)]
    if stale:
        store.delete_ids(stale)
    # Anche delete by path: copertura se SQLite e Chroma erano disallineati.
    if not new_ids:
        store.delete_by_rel_path(rel_path)
    else:
        store.upsert_chunks(ids=new_ids, documents=documents, metadatas=metadatas)

    # Stat file: mtime/size per debug e future sync leggere.
    try:
        st = abs_path.stat()
        mtime = float(st.st_mtime)
        size = int(st.st_size)
    except OSError:
        mtime = 0.0
        size = 0

    db.upsert_file(
        rel_path=rel_path,
        content_hash=content_hash,
        mtime=mtime,
        size=size,
    )
    return True


def _delete_indexed_file(db: IndexDB, store: ChromaStore, rel_path: str) -> None:
    """Rimuove file da SQLite e tutti i relativi chunk da Chroma."""
    chunk_ids = db.delete_file(rel_path)
    if chunk_ids:
        store.delete_ids(chunk_ids)
    # Doppio colpo: where su metadata se restano id fantasma.
    store.delete_by_rel_path(rel_path)
