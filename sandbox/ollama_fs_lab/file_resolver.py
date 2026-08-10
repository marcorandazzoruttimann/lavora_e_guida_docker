"""Resolver deterministico path file (RapidFuzz), senza LLM.

Da una traccia STT / nome tool (“leggimi spesa punto txt”) restituisce il
`Path` **relativo** reale nel workspace Desktop, oppure `None` se sotto soglia.

Contratto: nessun I/O di scrittura; solo `rglob` in lettura. I tool chiamanti
fanno poi `(workspace_dir / rel).resolve()` + check `relative_to`.
"""

from __future__ import annotations

import re
from pathlib import Path

from rapidfuzz import fuzz, process

# Stopword di comando/STT italiano: rumore da togliere prima del match sullo stem.
# Ordine lungo→corto nelle frasi multi-token gestito a parte (estensioni pronunciate).
_STOPWORDS: frozenset[str] = frozenset(
    {
        "leggimi",
        "leggi",
        "apri",
        "cerca",
        "trova",
        "mostrami",
        "mostra",
        "il",
        "lo",
        "la",
        "le",
        "un",
        "una",
        "file",
        "nota",
        "note",
        "documento",
        "documenti",
        "puntini",
        "per",
        "di",
        "del",
        "della",
        "dei",
        "delle",
        "mi",
    }
)

# Pronunce STT dell’estensione: “punto txt”, “dot md”, … → tolte prima dello stem.
# Regex case-insensitive; cattura spazi multipli tra “punto” e il suffix.
_EXT_PRONUNCIATION = re.compile(
    r"\b(?:punto|dot)\s+(txt|md|json|pdf|docx|doc|csv)\b",
    re.IGNORECASE,
)

# Suffix noti da strippare se l’input tool arriva già come `spesa.txt`.
_KNOWN_SUFFIXES: frozenset[str] = frozenset(
    {".txt", ".md", ".json", ".pdf", ".docx", ".doc", ".csv"}
)


def _normalize_stem_key(raw: str) -> str:
    """Canonizza uno stem per la mappa: lower, spazi/trattini → `_`.

    Coerente con `normalize_fs_name` (spazi→`_`) più lower e trattini,
    così STT e basename disco condividono la stessa chiave.
    """
    # Strip + lower: confronti case-insensitive senza dipendere dal FS.
    s = (raw or "").strip().lower()
    # Trattini e spazi → underscore (Windows/shell-friendly, come create_text_file).
    s = s.replace("-", "_").replace(" ", "_")
    # Collassa `__` ripetuti da doppia sostituzione o rumore STT.
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("_")


def _scan_stem_map(workspace_dir: Path) -> dict[str, list[Path]]:
    """Scansiona i file sotto workspace → stem_normalizzato → path relativi.

    Solo `is_file()`; directory ignorate. Collisioni stesso stem → lista
    (es. `notes/spesa.txt` e `inbox/spesa.pdf`).
    """
    root = workspace_dir.resolve()
    stem_map: dict[str, list[Path]] = {}

    # rglob: ricorsivo sotto notes/, inbox/, eventuali sotto-cartelle.
    for abs_path in root.rglob("*"):
        # Solo file regolari: niente directory né symlink rotto come “file”.
        if not abs_path.is_file():
            continue
        # Relativo al root: contratto API (`Path("notes/spesa.txt")`).
        try:
            rel = abs_path.relative_to(root)
        except ValueError:
            # Fuori root (symlink): skip silenzioso, non è candidato sicuro.
            continue
        # Chiave = stem del basename, non l’intero path (cartella non conta).
        key = _normalize_stem_key(abs_path.stem)
        if not key:
            continue
        stem_map.setdefault(key, []).append(rel)

    return stem_map


def _clean_stt_input(stt_input: str) -> str:
    """Pulisce la traccia STT/tool fino a uno stem confrontabile con la mappa.

    Passi: lower → togli “punto/dot + ext” → togli stopword → spazi/trattini → `_`.
    Path con cartella o suffix noti: resta solo lo stem del basename.
    """
    text = (stt_input or "").strip().lower()
    if not text:
        return ""

    # Pronunce estensione prima della tokenizzazione (frasi multi-parola).
    text = _EXT_PRONUNCIATION.sub(" ", text)

    # Se arriva `notes/spesa.txt` dal tool: match sullo stem, non sulla cartella.
    # replace \\ → / per path stile Windows passati per sbaglio.
    as_path = Path(text.replace("\\", "/"))
    basename = as_path.name
    # Togli suffix noto (`spesa.txt` → `spesa`) senza inventare stem strani.
    if as_path.suffix.lower() in _KNOWN_SUFFIXES:
        basename = as_path.stem

    # Tokenizza su spazi/underscore/trattini: stopword spesso separate.
    tokens = re.split(r"[\s_\-]+", basename)
    kept = [t for t in tokens if t and t not in _STOPWORDS]
    if not kept:
        return ""

    # Unisci e ri-normalizza: stesso formato delle chiavi della mappa.
    return _normalize_stem_key("_".join(kept))


def _filter_by_suffix(
    candidates: list[Path],
    allowed_suffixes: frozenset[str] | None,
) -> list[Path]:
    """Filtra i path per estensione dopo lo scan (evita spesa.txt vs spesa.pdf)."""
    if allowed_suffixes is None:
        return list(candidates)
    # Confronta suffix lower: `.PDF` su disco deve matchare `{".pdf"}`.
    return [p for p in candidates if p.suffix.lower() in allowed_suffixes]


def _pick_best_path(cleaned: str, candidates: list[Path]) -> Path | None:
    """Sceglie un Path da una lista (già filtrata per suffix se richiesto).

    0 candidati → None; 1 → quello; >1 → max `fuzz.WRatio(cleaned, path.name)`.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # Collisione stesso stem: decide il basename completo (estensione inclusa).
    best: Path | None = None
    best_score = -1.0
    for rel in candidates:
        # path.name = `spesa.txt`; cleaned è stem-like → WRatio disambigua.
        score = float(fuzz.WRatio(cleaned, rel.name))
        if score > best_score:
            best_score = score
            best = rel
    return best


def _resolve_from_key(
    cleaned: str,
    key: str,
    stem_map: dict[str, list[Path]],
    allowed_suffixes: frozenset[str] | None,
) -> Path | None:
    """Applica filtro suffix + disambiguazione a tutti i path di una chiave stem."""
    raw = stem_map.get(key) or []
    filtered = _filter_by_suffix(raw, allowed_suffixes)
    return _pick_best_path(cleaned, filtered)


def resolve_file_path(
    stt_input: str,
    workspace_dir: Path,
    threshold: int = 70,
    *,
    allowed_suffixes: frozenset[str] | None = None,
) -> Path | None:
    """Ritorna Path relativo al workspace, oppure None se sotto soglia / mappa vuota.

    Waterfall: scan → pulizia STT → exact stem → fuzzy WRatio ≥ threshold.
    `allowed_suffixes` filtra dopo lo scan (es. `{".pdf"}`); `None` = tutti i file.
    """
    # Workspace assente o non directory: niente da risolvere.
    if not workspace_dir.is_dir():
        return None

    # 1) Scan: stem → lista di path relativi (collisioni preservate).
    stem_map = _scan_stem_map(workspace_dir)
    if not stem_map:
        return None

    # 2) Pulizia STT: stopword + pronunce ext → stem canonico.
    cleaned = _clean_stt_input(stt_input)
    if not cleaned:
        return None

    # 3a) Exact: chiave mappa == cleaned.
    if cleaned in stem_map:
        exact = _resolve_from_key(cleaned, cleaned, stem_map, allowed_suffixes)
        if exact is not None:
            return exact
        # Exact stem ma suffix sbagliato (es. solo .txt, chiediamo .pdf): fall-through fuzzy.

    # 3b) Fuzzy: miglior chiave stem via WRatio; accetta solo se ≥ threshold.
    keys = list(stem_map.keys())
    hit = process.extractOne(cleaned, keys, scorer=fuzz.WRatio)
    if hit is None:
        return None
    # extractOne → (choice, score, index); score 0–100.
    best_key, score, _index = hit
    if float(score) < float(threshold):
        return None

    return _resolve_from_key(cleaned, best_key, stem_map, allowed_suffixes)
