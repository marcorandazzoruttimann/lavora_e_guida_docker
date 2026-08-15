"""SQLite mirror dell'indice RAG: file (hash) + chunk_id allineati a Chroma.

Persistenza sotto `index_root/files.db` (cartella indice nel repo, non sul Desktop).
I `rel_path` in SQLite restano relativi al data workspace utente.
Lo schema è il contratto con `index_sync`: se l'hash grezzo del file non
cambia → skip re-embed.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

# Nome file SQLite dentro la cartella indice (INDEX_ROOT nel repo).
DB_FILENAME = "files.db"
# Nomi legacy sul data workspace Desktop: solo per hint migrazione manuale.
LEGACY_WORKSPACE_INDEX_NAMES: frozenset[str] = frozenset({"ollama_lab", ".ollama_lab"})

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileRecord:
    """Riga tabella `files`: path relativo + hash contenuto grezzo."""

    rel_path: str
    content_hash: str
    mtime: float
    size: int
    indexed_at: str


@dataclass(frozen=True)
class ChunkRecord:
    """Riga tabella `chunks`: specchio degli id documento in Chroma."""

    chunk_id: str
    rel_path: str
    chunk_index: int
    content_hash: str


def ensure_index_dir(index_root: Path) -> Path:
    """Crea `index_root/` se assente e ritorna il path risolto.

    Side-effect: mkdir parents; non tocca il data workspace sul Desktop.
    """
    target = Path(index_root).resolve()
    target.mkdir(parents=True, exist_ok=True)
    return target


def db_path(index_root: Path) -> Path:
    """Path assoluto di `files.db` sotto la cartella indice."""
    return ensure_index_dir(index_root) / DB_FILENAME


def log_legacy_workspace_index_hint(
    data_workspace: Path,
    index_root: Path,
) -> None:
    """Avvisa se l'indice era sul Desktop e INDEX_ROOT nel repo è ancora vuoto.

    Non sposta file cross-filesystem (WSL): solo log per migrazione manuale.
    """
    root = Path(data_workspace).resolve()
    idx = Path(index_root).resolve()
    # Indice repo già popolato: niente da segnalare.
    if db_path(idx).is_file() or (idx / "chroma").is_dir():
        return
    for name in LEGACY_WORKSPACE_INDEX_NAMES:
        legacy = root / name
        if not legacy.is_dir():
            continue
        logger.warning(
            "trovato indice legacy sul data workspace (%s); "
            "copia manualmente in %s se serve migrare",
            legacy,
            idx,
        )
        return


def _utc_now_iso() -> str:
    """Timestamp ISO-8601 UTC per `indexed_at` (audit / debug sync)."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class IndexDB:
    """CRUD SQLite per files/chunks; una connessione per istanza.

    Side-effect: crea directory indice e schema al primo `connect`.
    Usare come context manager oppure chiamare `close()` esplicitamente.
    """

    def __init__(self, index_root: Path) -> None:
        # Path risolto una volta: evita ambiguity relative/absolute tra call.
        self.index_root = Path(index_root).resolve()
        # db_path → ensure_index_dir: crea INDEX_ROOT prima di aprire SQLite.
        self.path = db_path(self.index_root)
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        """Apre (o riusa) la connessione; crea dir + schema se mancano."""
        if self._conn is not None:
            return self._conn
        # parents: index_root può non esistere al primo sync.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: sync può girare da thread lab futuri.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        # Row factory: accesso per nome colonna nei SELECT.
        self._conn.row_factory = sqlite3.Row
        # FK logiche: non usiamo FOREIGN KEY strict (delete gestito a mano).
        self._ensure_schema(self._conn)
        return self._conn

    def close(self) -> None:
        """Chiude la connessione se aperta (idempotente)."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Self:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        """Crea tabelle `files` e `chunks` se assenti (idempotente)."""
        # files: chiave = path relativo posix; hash = sha256 bytes grezzi.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS files (
                rel_path TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                mtime REAL,
                size INTEGER,
                indexed_at TEXT
            )
            """
        )
        # chunks: chunk_id = stesso id Chroma; content_hash tipicamente del file padre.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                chunk_id TEXT PRIMARY KEY,
                rel_path TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                content_hash TEXT NOT NULL
            )
            """
        )
        # Indice su rel_path: delete/replace per file senza full scan.
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_chunks_rel_path
            ON chunks (rel_path)
            """
        )
        conn.commit()

    # --- files -----------------------------------------------------------------

    def get_file(self, rel_path: str) -> FileRecord | None:
        """Ritorna il record file o None se mai indicizzato."""
        conn = self.connect()
        row = conn.execute(
            "SELECT rel_path, content_hash, mtime, size, indexed_at "
            "FROM files WHERE rel_path = ?",
            (rel_path,),
        ).fetchone()
        if row is None:
            return None
        return FileRecord(
            rel_path=row["rel_path"],
            content_hash=row["content_hash"],
            mtime=float(row["mtime"] or 0.0),
            size=int(row["size"] or 0),
            indexed_at=row["indexed_at"] or "",
        )

    def list_file_paths(self) -> set[str]:
        """Insieme di tutti i `rel_path` noti (per orphan delete)."""
        conn = self.connect()
        rows = conn.execute("SELECT rel_path FROM files").fetchall()
        return {str(r["rel_path"]) for r in rows}

    def upsert_file(
        self,
        *,
        rel_path: str,
        content_hash: str,
        mtime: float,
        size: int,
        indexed_at: str | None = None,
    ) -> None:
        """Inserisce o aggiorna la riga `files` (hash / mtime / size)."""
        conn = self.connect()
        stamp = indexed_at if indexed_at is not None else _utc_now_iso()
        conn.execute(
            """
            INSERT INTO files (rel_path, content_hash, mtime, size, indexed_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(rel_path) DO UPDATE SET
                content_hash = excluded.content_hash,
                mtime = excluded.mtime,
                size = excluded.size,
                indexed_at = excluded.indexed_at
            """,
            (rel_path, content_hash, mtime, size, stamp),
        )
        conn.commit()

    def delete_file(self, rel_path: str) -> list[str]:
        """Cancella file + chunk SQLite; ritorna gli id chunk rimossi (per Chroma)."""
        conn = self.connect()
        # Prima gli id: Chroma va aggiornato dal caller con questa lista.
        chunk_ids = self.list_chunk_ids(rel_path)
        conn.execute("DELETE FROM chunks WHERE rel_path = ?", (rel_path,))
        conn.execute("DELETE FROM files WHERE rel_path = ?", (rel_path,))
        conn.commit()
        return chunk_ids

    # --- chunks ----------------------------------------------------------------

    def list_chunk_ids(self, rel_path: str) -> list[str]:
        """Id chunk SQLite per un path (ordine di `chunk_index`)."""
        conn = self.connect()
        rows = conn.execute(
            "SELECT chunk_id FROM chunks WHERE rel_path = ? "
            "ORDER BY chunk_index ASC",
            (rel_path,),
        ).fetchall()
        return [str(r["chunk_id"]) for r in rows]

    def replace_chunks(self, rel_path: str, records: Iterable[ChunkRecord]) -> list[str]:
        """Sostituisce tutti i chunk di un file; ritorna gli id *vecchi* rimossi.

        Caller: cancella i vecchi id da Chroma, poi upsert dei nuovi.
        """
        conn = self.connect()
        old_ids = self.list_chunk_ids(rel_path)
        conn.execute("DELETE FROM chunks WHERE rel_path = ?", (rel_path,))
        rows = list(records)
        if rows:
            conn.executemany(
                """
                INSERT INTO chunks (chunk_id, rel_path, chunk_index, content_hash)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (r.chunk_id, r.rel_path, r.chunk_index, r.content_hash)
                    for r in rows
                ],
            )
        conn.commit()
        return old_ids
