"""Tool `find_file` dello specialista FS: ricerca semantica RAG sul Desktop.

Stessa forma di `gmail/read.py`: il retrieval vive nel pacchetto specialista,
non in `tools/`. Contratto: query in linguaggio naturale → path relativo +
chunk rilevante. Nessun LLM nel retrieval: sync SQLite hash + embed MiniLM
ONNX via Chroma (`rag/` resta la libreria indice).
"""

from __future__ import annotations

import logging
from pathlib import Path

from lavora_e_guida.config import INDEX_ROOT, WORKSPACE_ROOT
from lavora_e_guida.fs.files import FsToolError
from lavora_e_guida.rag.chroma_store import ChromaStore
from lavora_e_guida.rag.index_db import db_path
from lavora_e_guida.rag.index_sync import sync_workspace_index

logger = logging.getLogger(__name__)

# Distanza cosine Chroma: 0 = identico; oltre soglia → “non rilevante” per TTS.
_MAX_COSINE_DISTANCE = 1.25
# Quanti hit chiedere a Chroma (top-1 + path alternativi distinti).
_QUERY_N_RESULTS = 3


class FindToolError(FsToolError):
    """Errore find_file / indice RAG: messaggio adatto al modello e al TTS."""


def _ensure_index(workspace: Path, index_root: Path = INDEX_ROOT) -> None:
    """Sync lazy: se manca files.db, indicizza tutto il workspace una volta.

    Side-effect: può creare `runtime/` sotto il repo e caricare embed (CPU, one-shot).
    """
    if not db_path(index_root).is_file():
        logger.info("indice assente: sync completo workspace %s → index %s", workspace, index_root)
        stats = sync_workspace_index(workspace, index_root=index_root)
        if stats.errors:
            logger.warning("sync iniziale con errori: %s", stats.errors)


def find_file(
    query: str,
    *,
    workspace: Path | None = None,
    index_root: Path = INDEX_ROOT,
) -> str:
    """Cerca nel workspace per contenuto semantico; ritorna path + chunk top-1.

    Output esito:
    - `OK: trovato {rel_path} (distanza …)\n---\n{chunk}`
    - eventuale riga `Altri file: …` con path distinti (max 3).
    - `ERRORE: …` via FindToolError se query vuota / nessun hit / troppo debole.

    I path nel risultato sono relativi a `workspace` (Desktop); l'indice vive in `index_root` (repo).
    """
    q = (query or "").strip()
    if not q:
        raise FindToolError("query vuota: indica cosa cercare (es. cetrioli nella spesa).")

    root = (workspace if workspace is not None else WORKSPACE_ROOT).resolve()
    # Lazy full sync se DB non esiste ancora (primo find dopo install lab).
    _ensure_index(root, index_root=index_root)

    store = ChromaStore(index_root)
    try:
        hits = store.query(q, n_results=_QUERY_N_RESULTS)
    except ImportError as exc:
        raise FindToolError(
            "chromadb non installato: esegui "
            "`pip install -e .` dalla root del repo."
        ) from exc

    if not hits:
        raise FindToolError(
            f"nessun file indicizzato rilevante per la query {q!r}. "
            "Aggiungi note in Ollama_test o attendi la sync."
        )

    best = hits[0]
    if not best.rel_path or not best.document.strip():
        raise FindToolError(
            f"risultato RAG incompleto per {q!r} (metadata o testo chunk assente)."
        )

    # Soglia distanza: evita risposte inventate su match deboli.
    if best.distance > _MAX_COSINE_DISTANCE:
        raise FindToolError(
            f"nessun file rilevante per {q!r} "
            f"(miglior distanza {best.distance:.3f}, soglia {_MAX_COSINE_DISTANCE})."
        )

    # Path alternativi distinti (escluso top-1), compatto per TTS.
    alt_paths: list[str] = []
    seen = {best.rel_path}
    for hit in hits[1:]:
        if hit.rel_path and hit.rel_path not in seen:
            seen.add(hit.rel_path)
            alt_paths.append(hit.rel_path)
        if len(alt_paths) >= 2:
            break

    lines = [
        f"OK: trovato {best.rel_path} (distanza {best.distance:.3f})",
        "---",
        best.document.strip(),
    ]
    if alt_paths:
        lines.append(f"Altri file: {', '.join(alt_paths)}")
    return "\n".join(lines)
