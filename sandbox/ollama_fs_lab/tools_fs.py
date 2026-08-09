"""Tool filesystem del lab: solo sotto WORKSPACE_ROOT (Desktop Ollama_test).

Step 2: `create_text_file` (default `.txt` se manca l'estensione);
Step 3: `append_note` (notes/);
Step 4: `read_text_file` — risoluzione deterministica estensioni + notes/.
Ogni path utente è risolto e verificato: fuori dal root → errore parlante, niente I/O.
"""

from __future__ import annotations

from pathlib import Path

from sandbox.ollama_fs_lab.config import WORKSPACE_ROOT


class FsToolError(ValueError):
    """Errore di contratto tool FS (path, argomenti): messaggio adatto al TTS."""


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
    return (
        f"OK: creato file {rel.as_posix()} "
        f"({len(text)} caratteri) nel workspace Ollama_test."
    )


def _note_target_name(name: str) -> str:
    """Mappa un nome nota sul path relativo sotto notes/ se manca una cartella.

    Contratto Step 3: `spesa.txt` → `notes/spesa.txt`; `notes/x.txt` resta così.
    Path con `..` / assoluti restano al caller (`resolve_in_workspace`).
    """
    # Normalizziamo subito: spazi→_ e strip, coerente con create_text_file.
    cleaned = normalize_fs_name(name)
    if not cleaned:
        return cleaned
    # Se c'è già un separatore, l'utente ha scelto la cartella (es. notes/ o root).
    candidate = Path(cleaned)
    if len(candidate.parts) > 1:
        return cleaned
    # Solo basename: destinazione naturale delle note = notes/ (creata da ensure).
    return f"notes/{cleaned}"


def append_note(name: str, text: str) -> str:
    """Appende testo UTF-8 a una nota (crea il file se non esiste).

    Side-effect: scrive sotto WORKSPACE_ROOT (di solito `notes/…`).
    Se il file esiste e non termina con newline, ne aggiungiamo una prima
    del pezzo nuovo così due append consecutive restano leggibili su Windows.
    """
    # text None → stringa vuota: append no-op ma file creato se mancava.
    chunk = "" if text is None else str(text)

    # Bare name → notes/; path con cartella → invariato (sempre via resolve sicuro).
    target = resolve_in_workspace(_note_target_name(name))

    # mkdir genitori: notes/ può già esserci; serve se name era notes/sub/….
    target.parent.mkdir(parents=True, exist_ok=True)

    # Esisteva già? Serve per il messaggio e per decidere il separatore newline.
    existed = target.is_file()
    if existed:
        # Lettura minima: solo ultimo byte (se c'è) per sapere se manca `\n`.
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

    rel = target.relative_to(WORKSPACE_ROOT.resolve())
    return (
        f"OK: nota {action} {rel.as_posix()} "
        f"(+{len(chunk)} caratteri) nel workspace Ollama_test."
    )


# Estensioni provate in lettura se l'LLM omette il suffix (ordine fisso, Python decide).
_READ_EXTENSIONS: tuple[str, ...] = (".txt", ".docx", ".pdf", ".md", ".json")


def _read_name_variants(cleaned: str) -> list[str]:
    """Espande un nome relativo nelle varianti da cercare su disco.

    Con suffix già presente → un solo candidato. Senza → stem + ogni estensione
    in `_READ_EXTENSIONS` (es. `spesa` → spesa.txt, spesa.docx, …).
    Path con cartella (es. `notes/spesa`) ricevono l'estensione solo sul basename.
    """
    candidate = Path(cleaned)
    # L'LLM ha già scelto il tipo: niente tentativi extra.
    if candidate.suffix:
        return [cleaned]

    stem = candidate.name
    parent = candidate.parent
    parent_posix = parent.as_posix()
    variants: list[str] = []
    for ext in _READ_EXTENSIONS:
        # parent == '.' → bare in root; altrimenti notes/spesa + ext.
        if parent_posix in (".", ""):
            variants.append(f"{stem}{ext}")
        else:
            variants.append(f"{parent_posix}/{stem}{ext}")
    return variants


def _resolve_readable_target(name: str) -> Path:
    """Risolve in modo deterministico il file da leggere (estensioni + notes/).

    Per ogni variante: prima root workspace, poi `notes/` se il nome è bare.
    Path già sotto notes/ o con cartella non vengono riprefissati.
    L'LLM non deve ritentare path: qui Python esaurisce le combinazioni.
    """
    cleaned = normalize_fs_name(name)
    if not cleaned:
        raise FsToolError("nome file vuoto: indica un nome relativo al workspace.")

    tried: list[str] = []
    for rel in _read_name_variants(cleaned):
        # 1) Root (o path già relativo, es. notes/x.txt passato dall'LLM).
        target = resolve_in_workspace(rel)
        tried.append(rel)
        if target.is_file():
            return target

        # 2) Fallback notes/ solo per bare name (un segmento): dove scrive append_note.
        if len(Path(rel).parts) == 1:
            notes_rel = f"notes/{rel}"
            notes_target = resolve_in_workspace(notes_rel)
            tried.append(notes_rel)
            if notes_target.is_file():
                return notes_target

    display = (name or "").strip() or cleaned
    exts = ", ".join(_READ_EXTENSIONS)
    raise FsToolError(
        f"ERRORE: File '{display}' non trovato nel workspace né in notes/ "
        f"(estensioni provate: {exts})."
    )


def read_text_file(name: str) -> str:
    """Legge un file di testo UTF-8 sotto WORKSPACE_ROOT e ne restituisce il contenuto.

    Ritorna una stringa di esito per il modello (prefisso OK + corpo).
    Side-effect: nessuno in scrittura. Path/estensioni risolti in Python
    (vedi `_resolve_readable_target`); file assente → FsToolError parlante.
    """
    # Risoluzione + esistenza: qui falliscono path illegali / file mancanti.
    target = _resolve_readable_target(name)

    # UTF-8: stesso encoding di create/append; .docx/.pdf binari → errore chiaro.
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        rel = target.relative_to(WORKSPACE_ROOT.resolve()).as_posix()
        raise FsToolError(
            f"file {rel!r} trovato ma non leggibile come testo UTF-8."
        ) from exc

    # Path relativo: il modello lo cita in TTS senza esporre /mnt/c/….
    rel = target.relative_to(WORKSPACE_ROOT.resolve())
    # Corpo dopo il marker: l'agente lo rilegge a voce (Step 4 Done).
    return (
        f"OK: contenuto di {rel.as_posix()} "
        f"({len(text)} caratteri):\n{text}"
    )

