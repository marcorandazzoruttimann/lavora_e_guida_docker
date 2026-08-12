"""Chunking testo per indice RAG: finestre ~800 caratteri con overlap 100.

Id chunk stabile: `sha256(rel_path)[:16]_{idx}` — stesso id in SQLite e Chroma,
così re-index e delete restano allineati senza UUID casuali.
"""

from __future__ import annotations

import hashlib
import re

# Dimensione target del chunk: abbastanza contesto per MiniLM, poca RAM su CPU.
CHUNK_SIZE = 800
# Overlap tra finestre consecutive: evita di spezzare una frase a metà al bordo.
CHUNK_OVERLAP = 100

# Separatore paragrafi: una o più linee vuote (markdown / note utente).
_PARA_SPLIT = re.compile(r"\n\s*\n+")


def chunk_id_for(rel_path: str, chunk_index: int) -> str:
    """Id deterministico per un chunk: prefisso path + indice intero.

    `rel_path` deve essere posix relativo (es. `notes/spesa.txt`).
    Il prefisso sha256 tronca a 16 hex: collisioni path praticamente nulle in lab.
    """
    # Hash sul path relativo: se il file si sposta di cartella, gli id cambiano
    # (corretto: i vecchi chunk vengono cancellati dal sync).
    digest = hashlib.sha256(rel_path.encode("utf-8")).hexdigest()[:16]
    return f"{digest}_{chunk_index}"


def split_text(
    text: str,
    *,
    chunk_size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> list[str]:
    """Spezza `text` in chunk di ~`chunk_size` caratteri con overlap.

    Strategia: prima impacchetta paragrafi (`\\n\\n`); se un pezzo supera
    `chunk_size`, scorre una finestra a step `chunk_size - overlap`.
    Testo vuoto / solo whitespace → lista vuota (nessun embed inutile).
    """
    # Normalizza bordi: niente chunk fatti solo di spazi.
    cleaned = (text or "").strip()
    if not cleaned:
        return []

    # Vincoli: overlap < size altrimenti lo step sarebbe 0 o negativo.
    size = max(1, int(chunk_size))
    ov = max(0, min(int(overlap), size - 1))

    # Paragrafi non vuoti: note utente tipicamente spezzate da linee bianche.
    paragraphs = [p.strip() for p in _PARA_SPLIT.split(cleaned) if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    # Buffer corrente: accumula paragrafi finché resta sotto `size`.
    buf = ""

    for para in paragraphs:
        # Paragrafo più lungo del tetto: flush buffer, poi finestre scorrevoli.
        if len(para) > size:
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.extend(_sliding_windows(para, size=size, overlap=ov))
            continue

        # Buffer vuoto: inizia un nuovo pezzo con questo paragrafo.
        if not buf:
            buf = para
            continue

        # Prova ad accodare con separatore paragrafo (stesso stile del sorgente).
        candidate = f"{buf}\n\n{para}"
        if len(candidate) <= size:
            buf = candidate
            continue

        # Non ci sta: emetti il buffer e riparti da questo paragrafo.
        chunks.append(buf)
        buf = para

    # Ultimo buffer residuo (file corto o coda dopo packing).
    if buf:
        chunks.append(buf)

    return chunks


def _sliding_windows(text: str, *, size: int, overlap: int) -> list[str]:
    """Finestra scorrevole su un blocco già più lungo di `size`.

    Step = size - overlap: ogni pezzo successivo ripete gli ultimi `overlap`
    caratteri del precedente (continuità semantica al bordo).
    """
    if not text:
        return []
    # Blocco corto: una sola finestra (caller di solito non arriva qui).
    if len(text) <= size:
        return [text]

    step = max(1, size - overlap)
    out: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        piece = text[start : start + size]
        if piece:
            out.append(piece)
        # Ultima finestra (anche parziale): esci senza oltrepassare.
        if start + size >= length:
            break
        start += step
    return out
