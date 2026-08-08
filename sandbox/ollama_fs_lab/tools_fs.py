"""Tool filesystem del lab: solo sotto WORKSPACE_ROOT (Desktop Ollama_test).

Step 2: `create_text_file`; Step 3: `append_note` (notes/); read/list nello Step 4.
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
    """
    # content None → stringa vuota: meglio un file vuoto che crash sul tipo.
    text = "" if content is None else str(content)

    # Risoluzione sicura: qui falliscono traversal / assoluti (FsToolError).
    target = resolve_in_workspace(name)

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
