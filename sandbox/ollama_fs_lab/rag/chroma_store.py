"""Store ChromaDB persistente per i chunk del workspace.

Collection `workspace_chunks` sotto `index_root/chroma/` (nel repo, non sul Desktop).
Embedding: DefaultEmbeddingFunction (ONNX all-MiniLM-L6-v2) — nessun secondo
modello Ollama in RAM accanto a qwen2.5:3b.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sandbox.ollama_fs_lab.rag.index_db import ensure_index_dir

# Sotto-cartella Chroma dentro INDEX_ROOT (separata da files.db).
CHROMA_DIRNAME = "chroma"
# Nome collection unico del lab: un solo indice semantico per workspace.
COLLECTION_NAME = "workspace_chunks"


@dataclass(frozen=True)
class QueryHit:
    """Risultato di una query: path, testo chunk, distanza Chroma, metadati."""

    chunk_id: str
    rel_path: str
    chunk_index: int
    document: str
    distance: float
    content_hash: str


def chroma_persist_dir(index_root: Path) -> Path:
    """Path assoluto della directory PersistentClient Chroma."""
    return ensure_index_dir(index_root) / CHROMA_DIRNAME


class ChromaStore:
    """Wrapper sottile su PersistentClient + collection workspace_chunks.

    Side-effect: crea la directory persist e la collection al primo accesso.
    Import chromadb lazy nel connect: il resto del lab non richiede l'extra
    finché non si sincronizza/interroga l'indice.
    """

    def __init__(self, index_root: Path) -> None:
        self.index_root = Path(index_root).resolve()
        self.persist_dir = chroma_persist_dir(self.index_root)
        self._client: Any = None
        self._collection: Any = None

    def connect(self) -> Any:
        """Ritorna la collection; apre PersistentClient se necessario."""
        if self._collection is not None:
            return self._collection

        # Import lazy: messaggio ImportError chiaro se manca extra `lab`.
        try:
            import chromadb
            from chromadb.utils import embedding_functions
        except ImportError as exc:
            raise ImportError(
                "chromadb non installato: esegui "
                '`pip install -e ".[lab]"` dalla root del repo.'
            ) from exc

        # Directory persist: ONNX/embedding cache vivono qui col DB Chroma.
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(self.persist_dir))

        # DefaultEmbeddingFunction esplicita: contratto piano (non Ollama).
        ef = embedding_functions.DefaultEmbeddingFunction()
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            embedding_function=ef,
            # cosine: distanze interpretabili per soglia “non rilevante” in find_file.
            metadata={"hnsw:space": "cosine"},
        )
        return self._collection

    def upsert_chunks(
        self,
        *,
        ids: Sequence[str],
        documents: Sequence[str],
        metadatas: Sequence[Mapping[str, Any]],
    ) -> None:
        """Upsert batch: stessi id → sovrascrive embed/documento/metadata."""
        if not ids:
            return
        if not (len(ids) == len(documents) == len(metadatas)):
            raise ValueError(
                "ids, documents e metadatas devono avere la stessa lunghezza."
            )
        col = self.connect()
        # Chroma accetta list; metadata values: str/int/float/bool.
        col.upsert(
            ids=list(ids),
            documents=list(documents),
            metadatas=[dict(m) for m in metadatas],
        )

    def delete_ids(self, ids: Sequence[str]) -> None:
        """Rimuove documenti per id; no-op se lista vuota."""
        if not ids:
            return
        col = self.connect()
        col.delete(ids=list(ids))

    def delete_by_rel_path(self, rel_path: str) -> None:
        """Rimuove tutti i chunk con metadata `rel_path` (orphan / re-index)."""
        col = self.connect()
        # where eq: filtra senza dover conoscere gli id a priori.
        col.delete(where={"rel_path": rel_path})

    def query(self, query_text: str, *, n_results: int = 3) -> list[QueryHit]:
        """Query semantica: top-N hit ordinati per distanza crescente.

        `n_results` tipicamente 3 (find_file usa top-1 + path alternativi).
        Collection vuota o query blank → lista vuota (niente eccezioni).
        """
        q = (query_text or "").strip()
        if not q:
            return []

        col = self.connect()
        # count==0: query su collection vuota può fallire o tornare vuoto.
        try:
            total = col.count()
        except Exception:  # noqa: BLE001 — API Chroma varia tra versioni
            total = 0
        if total == 0:
            return []

        # Non chiedere più hit di quanti documenti esistano.
        k = max(1, min(int(n_results), total))
        raw = col.query(
            query_texts=[q],
            n_results=k,
            include=["documents", "metadatas", "distances"],
        )

        # Forma Chroma: liste annidate (una query → una lista di risultati).
        ids = (raw.get("ids") or [[]])[0]
        docs = (raw.get("documents") or [[]])[0]
        metas = (raw.get("metadatas") or [[]])[0]
        dists = (raw.get("distances") or [[]])[0]

        hits: list[QueryHit] = []
        for i, chunk_id in enumerate(ids):
            meta = metas[i] if i < len(metas) and metas[i] is not None else {}
            doc = docs[i] if i < len(docs) and docs[i] is not None else ""
            dist = float(dists[i]) if i < len(dists) and dists[i] is not None else 0.0
            # Metadata contratto: rel_path, chunk_index, content_hash.
            hits.append(
                QueryHit(
                    chunk_id=str(chunk_id),
                    rel_path=str(meta.get("rel_path") or ""),
                    chunk_index=int(meta.get("chunk_index") or 0),
                    document=str(doc),
                    distance=dist,
                    content_hash=str(meta.get("content_hash") or ""),
                )
            )
        return hits
