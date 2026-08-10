"""Tool filesystem del lab: solo sotto WORKSPACE_ROOT (Desktop Ollama_test).

Step 2: `create_text_file` (default `.txt` se manca l'estensione);
Step 3: `append_note` — resolve RapidFuzz, create-on-miss sotto notes/;
Step 4: `read_text_file` — resolve RapidFuzz (suffix testo).
Ogni path utente è risolto e verificato: fuori dal root → errore parlante, niente I/O.
"""

from __future__ import annotations

from pathlib import Path

from sandbox.ollama_fs_lab.config import WORKSPACE_ROOT
from sandbox.ollama_fs_lab.file_resolver import resolve_file_path

# Suffix ammessi in lettura testo: evita collisioni stem con PDF/binari.
_READ_TEXT_SUFFIXES: frozenset[str] = frozenset({".txt", ".md", ".json"})


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
    return (
        f"OK: nota {action} {out_rel.as_posix()} "
        f"(+{len(chunk)} caratteri) nel workspace Ollama_test."
    )


def read_text_file(name: str) -> str:
    """Legge un file di testo UTF-8 sotto WORKSPACE_ROOT e ne restituisce il contenuto.

    Ritorna una stringa di esito per il modello (prefisso OK + corpo).
    Side-effect: nessuno in scrittura. Path risolto da `resolve_file_path`
    (suffix `.txt`/`.md`/`.json`); nessuno match → FsToolError parlante.
    """
    # Nome blank: messaggio chiaro senza dipendere dal None del resolver.
    if not (name or "").strip():
        raise FsToolError("nome file vuoto: indica un nome relativo al workspace.")

    # Scan + fuzzy: l'LLM può passare nome sporco / senza cartella né estensione.
    root = WORKSPACE_ROOT.resolve()
    rel = resolve_file_path(
        name,
        root,
        allowed_suffixes=_READ_TEXT_SUFFIXES,
    )
    if rel is None:
        display = (name or "").strip()
        raise FsToolError(
            f"File '{display}' non trovato nel workspace "
            "(testo: .txt, .md, .json)."
        )

    # Relativo → assoluto confinato; safety su traversal anche dopo il resolver.
    target = resolve_in_workspace(rel.as_posix())
    # Il resolver ha già visto is_file(); race/delete → messaggio parlante.
    if not target.is_file():
        raise FsToolError(
            f"File '{rel.as_posix()}' non trovato nel workspace "
            "(testo: .txt, .md, .json)."
        )

    # UTF-8: stesso encoding di create/append; binari filtrati dai suffix.
    try:
        text = target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise FsToolError(
            f"file {rel.as_posix()!r} trovato ma non leggibile come testo UTF-8."
        ) from exc

    # Path relativo: il modello lo cita in TTS senza esporre /mnt/c/….
    # Corpo dopo il marker: l'agente lo rilegge a voce (Step 4 Done).
    return (
        f"OK: contenuto di {rel.as_posix()} "
        f"({len(text)} caratteri):\n{text}"
    )

