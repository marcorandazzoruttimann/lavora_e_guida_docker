"""Tool PDF del lab: estrazione testo da file sotto WORKSPACE_ROOT.

Step 6: `read_pdf` con pypdf — l'utente copia a mano i PDF (tipicamente in
`inbox/`); l'agente estrae testo (con tetto caratteri) e Ollama risponde.
Path risolto da `resolve_file_path` (solo `.pdf`); fuori root → errore parlante.
"""

from __future__ import annotations

from sandbox.ollama_fs_lab.config import WORKSPACE_ROOT
from sandbox.ollama_fs_lab.file_resolver import resolve_file_path
from sandbox.ollama_fs_lab.tools_fs import FsToolError, resolve_in_workspace

# Tetto contesto per qwen2.5:3b: PDF lunghi non devono saturare il prompt.
# Oltre questo limite tronchiamo e segnaliamo nel messaggio di esito.
_MAX_PDF_CHARS = 8000

# Solo PDF: evita collisioni stem con omonimi .txt/.md nello stesso workspace.
_PDF_SUFFIXES: frozenset[str] = frozenset({".pdf"})


def read_pdf(name: str, *, max_chars: int = _MAX_PDF_CHARS) -> str:
    """Estrae testo da un PDF sotto WORKSPACE_ROOT e lo restituisce al modello.

    Side-effect: nessuno in scrittura. Dipendenza: `pypdf` (extra leggero).
    Path da `resolve_file_path` (allowed `.pdf`); assente / non leggibile /
    senza testo → FsToolError parlante. Oltre `max_chars` tronca e annota.
    """
    # Import lazy: messaggio chiaro se manca l'extra senza rompere Step 1–5.
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise FsToolError(
            "pypdf non installato: esegui `pip install pypdf` "
            '(oppure `pip install -e ".[lab]"` dalla root del repo).'
        ) from exc

    # Nome blank: messaggio chiaro senza dipendere dal None del resolver.
    if not (name or "").strip():
        raise FsToolError(
            "nome PDF vuoto: indica un nome relativo (es. verbale.pdf)."
        )

    # Scan + fuzzy: nome sporco / senza inbox/ né .pdf → Python decide il path.
    root = WORKSPACE_ROOT.resolve()
    rel = resolve_file_path(name, root, allowed_suffixes=_PDF_SUFFIXES)
    if rel is None:
        display = (name or "").strip()
        raise FsToolError(
            f"PDF '{display}' non trovato nel workspace (solo file .pdf)."
        )

    # Relativo → assoluto confinato; safety su traversal anche dopo il resolver.
    target = resolve_in_workspace(rel.as_posix())
    if not target.is_file():
        raise FsToolError(
            f"PDF '{rel.as_posix()}' non trovato nel workspace (solo file .pdf)."
        )

    # Safety ridondante: il filtro suffix del resolver già esclude non-PDF.
    if target.suffix.casefold() != ".pdf":
        raise FsToolError(
            f"file {rel.as_posix()!r} non è un PDF (.pdf richiesto per read_pdf)."
        )

    # Apertura binaria: pypdf gestisce stream; errori tipici → messaggio parlante.
    try:
        reader = PdfReader(str(target))
    except Exception as exc:  # noqa: BLE001 — PDF corrotti variano molto
        raise FsToolError(f"PDF {rel.as_posix()!r} non leggibile: {exc}") from exc

    # PDF cifrati senza password: pypdf espone is_encrypted; non tentiamo crack.
    if getattr(reader, "is_encrypted", False):
        raise FsToolError(
            f"PDF {rel.as_posix()!r} è protetto da password: "
            "non posso estrarre il testo."
        )

    # Concateniamo pagina per pagina: spazio tra pagine evita parole attaccate.
    parts: list[str] = []
    for page in reader.pages:
        # extract_text può tornare None su pagine solo-immagine.
        chunk = page.extract_text() or ""
        if chunk.strip():
            parts.append(chunk)
    text = "\n\n".join(parts).strip()

    if not text:
        raise FsToolError(
            f"PDF {rel.as_posix()!r} senza testo estraibile "
            "(forse solo immagini: serve OCR, fuori scope del lab)."
        )

    # Troncamento esplicito: il modello sa che il contesto è parziale.
    truncated = False
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    # Prefisso OK: l'agente lo usa per Q&A / riassunto con tool=none.
    note = " (troncato)" if truncated else ""
    return (
        f"OK: testo estratto da {rel.as_posix()} "
        f"({len(text)} caratteri{note}):\n{text}"
    )
