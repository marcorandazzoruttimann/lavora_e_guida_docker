"""SQLite telemetria per turno STT: una riga per richiesta vocale.

File dedicato `telemetry.db` sotto INDEX_ROOT (accanto a `files.db`, non
mescolato all'indice RAG). Un turno con più round tool → una riga, token
sommati dal caller. Fallimento insert → log, nessun raise: il loop vocale
non deve interrompersi per la telemetria.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

# Nome file SQLite nella cartella indice (INDEX_ROOT nel repo).
DB_FILENAME = "telemetry.db"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SttRequestRecord:
    """Riga tabella `stt_requests`: un turno listen → speak."""

    id: int
    started_at: str
    ended_at: str
    token_input: int
    token_output: int
    tts_response: str


def db_path(index_root: Path) -> Path:
    """Path assoluto di `telemetry.db` sotto la cartella indice."""
    return Path(index_root).resolve() / DB_FILENAME


def utc_now_iso() -> str:
    """Timestamp ISO-8601 UTC senza microsecondi (started_at / ended_at)."""
    # Stesso formato di index_db: confrontabile a occhio e stabile nei test.
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class TelemetryDB:
    """Insert/read su `stt_requests`; una connessione per istanza.

    Side-effect: crea directory padre e schema al primo `connect`.
    Usare come context manager oppure chiamare `close()` esplicitamente.
    """

    def __init__(self, path: Path) -> None:
        # Path del file SQLite (es. INDEX_ROOT / telemetry.db), non della cartella.
        self.path = Path(path).resolve()
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        """Apre (o riusa) la connessione; crea dir + schema se mancano."""
        if self._conn is not None:
            return self._conn
        # parents: INDEX_ROOT può non esistere al primo turno vocale.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: il lab può parlare da thread futuri.
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        # Row factory: SELECT per nome colonna nei test e nel list.
        self._conn.row_factory = sqlite3.Row
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
        """Crea tabella `stt_requests` se assente (idempotente)."""
        # Niente colonne costo: solo token e testo TTS del turno.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS stt_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                ended_at TEXT NOT NULL,
                token_input INTEGER NOT NULL DEFAULT 0,
                token_output INTEGER NOT NULL DEFAULT 0,
                tts_response TEXT NOT NULL
            )
            """
        )
        conn.commit()

    def insert_stt_request(
        self,
        *,
        started_at: str,
        ended_at: str,
        token_input: int = 0,
        token_output: int = 0,
        tts_response: str,
    ) -> bool:
        """Inserisce una riga in transazione. Fallimento → log, ritorna False.

        Contratto: non alza eccezioni verso il caller (loop vocale hands-free).
        """
        try:
            conn = self.connect()
            # Transazione esplicita: commit solo se INSERT completa.
            with conn:
                conn.execute(
                    """
                    INSERT INTO stt_requests (
                        started_at, ended_at, token_input, token_output, tts_response
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        started_at,
                        ended_at,
                        int(token_input),
                        int(token_output),
                        tts_response,
                    ),
                )
            return True
        except Exception:
            # Telemetria best-effort: il turno vocale deve comunque chiudersi.
            logger.exception("inserimento telemetria fallito in %s", self.path)
            if self._conn is not None:
                try:
                    self._conn.rollback()
                except Exception:  # noqa: BLE001 — connessione irrecuperabile, si chiude
                    # Connessione irrecuperabile: chiudi, il prossimo insert riapre.
                    self.close()
            return False

    def list_stt_requests(self) -> list[SttRequestRecord]:
        """Tutte le righe in ordine di `id` (per test e audit locale)."""
        conn = self.connect()
        rows = conn.execute(
            "SELECT id, started_at, ended_at, token_input, token_output, "
            "tts_response FROM stt_requests ORDER BY id ASC"
        ).fetchall()
        return [
            SttRequestRecord(
                id=int(row["id"]),
                started_at=str(row["started_at"]),
                ended_at=str(row["ended_at"]),
                token_input=int(row["token_input"] or 0),
                token_output=int(row["token_output"] or 0),
                tts_response=str(row["tts_response"]),
            )
            for row in rows
        ]
