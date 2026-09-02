"""Tool filesystem dello specialista FS: solo sotto WORKSPACE_ROOT (Desktop).

Stessa forma di `gmail/read.py` e `web/search.py`: l'implementazione di dominio
vive nel pacchetto specialista, non in `tools/` (lì resta solo il catalogo
condiviso). Gemini chiama `create_text_file` / `append_note` / `read_file`;
questo modulo esegue l'I/O e torna `OK:` / `ERRORE:` parlanti.

`create_text_file`: default `.txt` se manca l'estensione.
`append_note`: resolve RapidFuzz, create-on-miss sotto notes/.
`read_file`: resolve unificato (testo + PDF), dispatch pypdf se `.pdf`.
Ogni path utente è risolto e verificato: fuori dal root → errore parlante, niente I/O.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from lavora_e_guida.config import INDEX_ROOT, WORKSPACE_ROOT
from lavora_e_guida.fs.file_resolver import resolve_file_path  # RapidFuzz, niente LLM

logger = logging.getLogger(__name__)

# Suffix leggibili da `read_file`: testo UTF-8 + PDF (estrazione pypdf).
_READ_FILE_SUFFIXES: frozenset[str] = frozenset({".txt", ".md", ".json", ".pdf"})
# Solo testo: preferenza omonimi quando l'utente non cita esplicitamente PDF.
_READ_TEXT_SUFFIXES: frozenset[str] = frozenset({".txt", ".md", ".json"})
# Solo PDF: usato se hint `.pdf` / «punto pdf» sull'input grezzo.
_PDF_SUFFIXES: frozenset[str] = frozenset({".pdf"})

# Tetto contesto per qwen2.5:3b: PDF lunghi non devono saturare il prompt.
_MAX_PDF_CHARS = 4000

# Pronuncia STT dell'estensione PDF: rilevata PRIMA della pulizia stem del resolver.
_PDF_HINT_PRONUNCIATION = re.compile(
    r"\b(?:punto|dot)\s+pdf\b",
    re.IGNORECASE,
)


class FsToolError(ValueError):
    """Errore di contratto tool FS (path, argomenti): messaggio adatto al TTS."""


def _sync_index_after_write(rel_posix: str) -> None:
    """Aggiorna indice RAG per un solo file dopo create/append (best-effort).

    Non fallisce il tool FS se Chroma/SQLite non disponibili: log warning.
    """
    try:
        from lavora_e_guida.rag.index_sync import upsert_indexed_file

        upsert_indexed_file(WORKSPACE_ROOT, rel_posix, index_root=INDEX_ROOT)
    except Exception as exc:  # noqa: BLE001 — indice opzionale, FS deve restare ok
        logger.warning("sync RAG post-scrittura fallita per %s: %s", rel_posix, exc)


def ensure_workspace() -> Path:
    """Crea root + notes/ + inbox/ al primo run (idempotente).

    Side-effect: mkdir sul Desktop Windows via /mnt/c; non tocca il repo.
    """
    # parents=True: se Ollama_test manca, la ricreiamo invece di fallire silenziosi.
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    # notes/: destinazione naturale delle note (Step 3); inbox/: PDF da importare a mano.
    (WORKSPACE_ROOT / "notes").mkdir(exist_ok=True)
    (WORKSPACE_ROOT / "inbox").mkdir(exist_ok=True)
    return WORKSPACE_ROOT.resolve()


def normalize_fs_name(name: str) -> str:
    """Normalizza il nome file proposto da Ollama prima di toccare il disco.

    I 3B spesso emettono spazi (`lista spesa.txt`); sul Desktop Windows
    preferiamo underscore per evitare path scomodi / ambigui in shell.
    """
    # Strip bordi: spazi esterni non sono parte del nome, solo rumore del modello.
    cleaned = (name or "").strip()
    # Ogni spazio → `_` anche nei segmenti di cartella (es. `note varie/a b.txt`).
    return cleaned.replace(" ", "_")


# Default in creazione: senza suffix → `.txt` (Python decide, non qwen).
_DEFAULT_TEXT_EXT = ".txt"


def with_default_text_ext(cleaned: str) -> str:
    """Se il path relativo non ha estensione, appende `.txt`.

    Contratto create: `spesa` → `spesa.txt`; `notes/lista` → `notes/lista.txt`.
    Con suffix già presente → invariato.
    """
    # Path.suffix vuoto = nessuna estensione nel basename.
    if Path(cleaned).suffix:
        return cleaned
    return f"{cleaned}{_DEFAULT_TEXT_EXT}"


def resolve_in_workspace(name: str) -> Path:
    """Risolve `name` relativo al workspace; blocca traversal e path assoluti.

    Contratto: solo path relativi (es. `spesa.txt`, `notes/lista.txt`).
    Spazi nel nome vengono sostituiti con `_` prima della risoluzione.
    `Path.resolve()` + `relative_to` impediscono `../` e symlink fuori root.
    """
    # Prima normalizziamo (spazi→_): la sicurezza lavora sul nome già canonico.
    cleaned = normalize_fs_name(name)
    # Argomento vuoto/blank: non c'è file da creare → errore chiaro per il modello.
    if not cleaned:
        raise FsToolError("nome file vuoto: indica un nome relativo al workspace.")

    # Path assoluti (anche stile Windows) non sono ammessi: tutto è relativo al root.
    candidate = Path(cleaned)
    if candidate.is_absolute() or cleaned.startswith(("/", "\\")) or (
        len(cleaned) >= 2 and cleaned[1] == ":"
    ):
        raise FsToolError(
            "path assoluto non consentito: usa solo un nome relativo "
            f"(es. spesa.txt), ricevuto {name!r}."
        )

    # resolve() espande .. e symlink; relative_to fallisce se usciamo dal root.
    root = WORKSPACE_ROOT.resolve()
    target = (root / candidate).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        # Messaggio parlante: l'agente lo legge a voce senza stacktrace.
        raise FsToolError(
            f"path fuori dal workspace Ollama_test: {name!r}."
        ) from exc
    return target


def create_text_file(name: str, content: str) -> str:
    """Crea (o sovrascrive) un file di testo UTF-8 sotto WORKSPACE_ROOT.

    Ritorna una stringa di esito per il modello (non solleva se I/O ok).
    Side-effect: scrive sul Desktop; crea le directory genitore se mancano.
    Senza estensione nel name → default `.txt` (es. `spesa` → `spesa.txt`).
    """
    # content None → stringa vuota: meglio un file vuoto che crash sul tipo.
    text = "" if content is None else str(content)

    # Normalizziamo + default .txt PRIMA della resolve: un solo path canonico.
    cleaned = normalize_fs_name(name)
    if not cleaned:
        raise FsToolError("nome file vuoto: indica un nome relativo al workspace.")
    # Risoluzione sicura: qui falliscono traversal / assoluti (FsToolError).
    target = resolve_in_workspace(with_default_text_ext(cleaned))

    # mkdir genitori: consente `notes/foo.txt` senza richiedere mkdir esplicito.
    target.parent.mkdir(parents=True, exist_ok=True)

    # write_text UTF-8: overwrite intenzionale così i retry del lab non falliscono.
    target.write_text(text, encoding="utf-8")

    # Path relativo al root: utile in TTS e per verificare a occhio su Windows.
    rel = target.relative_to(WORKSPACE_ROOT.resolve())
    rel_posix = rel.as_posix()
    # Indice semantico: find_file vede subito il nuovo/aggiornato contenuto.
    _sync_index_after_write(rel_posix)
    return (
        f"OK: creato file {rel_posix} "
        f"({len(text)} caratteri) nel workspace Ollama_test."
    )


def _new_note_create_rel(name: str) -> str:
    """Path relativo di sola creazione quando resolve_file_path non trova match.

    Contratto: bare `spesa` / `spesa.txt` → `notes/spesa.txt`;
    path con cartella resta invariato (dopo normalize + default `.txt`).
    Non è ricerca: serve solo al create-on-miss di `append_note`.
    """
    # Normalizziamo subito: spazi→_ e strip, coerente con create_text_file.
    cleaned = normalize_fs_name(name)
    if not cleaned:
        raise FsToolError("nome nota vuoto: indica un nome relativo al workspace.")
    # Default .txt sul basename: create-on-miss non lascia file senza estensione.
    with_ext = with_default_text_ext(cleaned)
    candidate = Path(with_ext)
    # Se c'è già un separatore, l'utente ha scelto la cartella (es. notes/).
    if len(candidate.parts) > 1:
        return with_ext
    # Solo basename: destinazione naturale delle note nuove = notes/.
    return f"notes/{with_ext}"


def append_note(name: str, content: str) -> str:
    """Appende testo UTF-8 a una nota (crea il file se non esiste).

    Side-effect: scrive sotto WORKSPACE_ROOT.
    Match esistente: `resolve_file_path` (tutti i suffix). Miss → crea
    `notes/{stem}.txt` (solo path di creazione, non ricerca).
    Parametro `content` allineato allo schema JSON del lab (come create).
    Se il file esiste e non termina con newline, ne aggiungiamo una prima
    del pezzo nuovo così due append consecutive restano leggibili su Windows.
    """
    # content None → stringa vuota: append no-op ma file creato se mancava.
    chunk = "" if content is None else str(content)

    # Nome blank: errore chiaro prima dello scan (stesso contratto degli altri tool).
    if not (name or "").strip():
        raise FsToolError("nome nota vuoto: indica un nome relativo al workspace.")

    # 1) Cerca file esistente ovunque nel workspace (fuzzy STT, tutti i tipi).
    root = WORKSPACE_ROOT.resolve()
    rel = resolve_file_path(name, root)
    if rel is not None:
        # Path relativo dal resolver → assoluto confinato (traversal bloccato).
        target = resolve_in_workspace(rel.as_posix())
    else:
        # 2) Nessun match: crea notes/<stem>.txt (o path con cartella già nel name).
        target = resolve_in_workspace(_new_note_create_rel(name))

    # mkdir genitori: notes/ può già esserci; serve se name era notes/sub/….
    target.parent.mkdir(parents=True, exist_ok=True)

    # Esisteva già? Serve per il messaggio e per decidere il separatore newline.
    existed = target.is_file()
    if existed:
        # Lettura minima: serve sapere se manca `\n` finale prima dell'append.
        previous = target.read_text(encoding="utf-8")
        # Separatore solo se c'è contenuto precedente senza newline finale.
        prefix = "" if (not previous or previous.endswith("\n")) else "\n"
        # append mode: non sovrascriviamo create_text_file / append precedenti.
        with target.open("a", encoding="utf-8") as fh:
            fh.write(prefix + chunk)
        action = "aggiornata"
    else:
        # Prima scrittura: equivalente a create sotto notes/, senza overwrite.
        target.write_text(chunk, encoding="utf-8")
        action = "creata"

    out_rel = target.relative_to(root)
    rel_posix = out_rel.as_posix()
    # Re-embed dopo append: hash cambia, find_file resta allineato.
    _sync_index_after_write(rel_posix)
    return (
        f"OK: nota {action} {rel_posix} "
        f"(+{len(chunk)} caratteri) nel workspace Ollama_test."
    )


def _has_pdf_hint(raw_name: str) -> bool:
    """True se l'input grezzo cita esplicitamente un PDF (prima della pulizia stem).

    Contratto: `.pdf` nel basename/path, oppure pronuncia STT «punto/dot pdf».
    Senza hint, `read_file` preferisce omonimi testo sullo stesso stem.
    """
    text = (raw_name or "").strip().lower()
    if not text:
        return False
    # Basename con suffix .pdf (anche path `inbox/spesa.pdf` o `spesa.PDF`).
    as_path = Path(text.replace("\\", "/"))
    if as_path.suffix.lower() == ".pdf":
        return True
    # Suffisso citato a metà frase (es. `leggi spesa.pdf per favore`).
    if ".pdf" in text:
        return True
    # Pronuncia STT: stessa famiglia di `_EXT_PRONUNCIATION` ma solo su pdf.
    return _PDF_HINT_PRONUNCIATION.search(text) is not None


def _resolve_readable_rel(name: str, root: Path) -> Path | None:
    """Risolve un path relativo leggibile con preferenza testo / hint PDF.

    Hint PDF → solo `.pdf`. Altrimenti prova testo (`.txt`/`.md`/`.json`) e,
    se assente, fallback PDF: così omonimi stesso stem preferiscono il testo.
    """
    # Hint esplicito: restringiamo lo scan così WRatio non sceglie lo .txt omonimo.
    if _has_pdf_hint(name):
        return resolve_file_path(name, root, allowed_suffixes=_PDF_SUFFIXES)

    # Preferenza testo: omonimi notes/spesa.txt + inbox/spesa.pdf → il .txt.
    rel = resolve_file_path(name, root, allowed_suffixes=_READ_TEXT_SUFFIXES)
    if rel is not None:
        return rel
    # Solo PDF (o nessun testo match): seconda passata su suffix PDF.
    return resolve_file_path(name, root, allowed_suffixes=_PDF_SUFFIXES)


def _extract_pdf_text(
    path: Path,
    max_chars: int,
    *,
    display: str,
) -> tuple[str, bool]:
    """Estrae testo da un PDF con pypdf; ritorna `(testo, troncato)`.

    Side-effect: nessuno in scrittura. Import lazy di pypdf (extra opzionale).
    `display` = path relativo parlante nei messaggi FsToolError.
    PDF cifrato / vuoto / corrotto → FsToolError parlante.
    """
    # Import lazy: messaggio chiaro se manca l'extra senza rompere Step 1–5.
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise FsToolError(
            "pypdf non installato: esegui `pip install -e .` "
            "dalla root del repo."
        ) from exc

    # Apertura binaria: pypdf gestisce stream; errori tipici → messaggio parlante.
    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise FsToolError(f"PDF {display!r} non leggibile: {exc}") from exc

    # PDF cifrati senza password: pypdf espone is_encrypted; non tentiamo crack.
    if getattr(reader, "is_encrypted", False):
        raise FsToolError(
            f"PDF {display!r} è protetto da password: "
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
            f"PDF {display!r} senza testo estraibile "
            "(forse solo immagini: serve OCR, fuori scope del lab)."
        )

    # Troncamento esplicito: il modello sa che il contesto è parziale.
    truncated = False
    if max_chars > 0 and len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    return text, truncated


def read_file(name: str, *, max_chars: int = _MAX_PDF_CHARS) -> str:
    """Legge un file testo o PDF sotto WORKSPACE_ROOT e ne restituisce il contenuto.

    Ritorna stringa di esito (prefisso `OK: contenuto di …` + corpo).
    Side-effect: nessuno in scrittura. Resolve su
    `{.txt,.md,.json,.pdf}`; hint PDF → preferenza `.pdf`, altrimenti testo.
    Branch `.pdf` → pypdf (tetto `max_chars`); resto → UTF-8.
    """
    # Nome blank: messaggio chiaro senza dipendere dal None del resolver.
    if not (name or "").strip():
        raise FsToolError("nome file vuoto: indica un nome relativo al workspace.")

    # Scan + fuzzy: l'LLM può passare nome sporco / senza cartella né estensione.
    root = WORKSPACE_ROOT.resolve()
    rel = _resolve_readable_rel(name, root)
    if rel is None:
        display = (name or "").strip()
        raise FsToolError(
            f"File '{display}' non trovato nel workspace "
            "(leggibili: .txt, .md, .json, .pdf)."
        )

    # Relativo → assoluto confinato; safety su traversal anche dopo il resolver.
    target = resolve_in_workspace(rel.as_posix())
    # Il resolver ha già visto is_file(); race/delete → messaggio parlante.
    if not target.is_file():
        raise FsToolError(
            f"File '{rel.as_posix()}' non trovato nel workspace "
            "(leggibili: .txt, .md, .json, .pdf)."
        )

    # Safety: solo suffix ammessi (il filtro resolve già esclude il resto).
    suffix = target.suffix.casefold()
    if suffix not in _READ_FILE_SUFFIXES:
        raise FsToolError(
            f"file {rel.as_posix()!r} non leggibile "
            "(ammessi: .txt, .md, .json, .pdf)."
        )

    # Branch PDF: estrazione pypdf + annotazioni (estratto)/(troncato) nel prefisso.
    if suffix == ".pdf":
        text, truncated = _extract_pdf_text(
            target,
            max_chars,
            display=rel.as_posix(),
        )
        # Prefisso unificato: stesso marker dei file testo, più note PDF.
        notes = ["estratto"]
        if truncated:
            notes.append("troncato")
        note_s = ", ".join(notes)
        return (
            f"OK: contenuto di {rel.as_posix()} "
            f"({note_s}, {len(text)} caratteri):\n{text}"
        )

    # Branch testo: stesso encoding di create/append.
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise FsToolError(
            f"file {rel.as_posix()!r} trovato ma non leggibile come testo UTF-8."
        ) from exc

    # Path relativo: il modello lo cita in TTS senza esporre /mnt/c/….
    return (
        f"OK: contenuto di {rel.as_posix()} "
        f"({len(text)} caratteri):\n{text}"
    )

