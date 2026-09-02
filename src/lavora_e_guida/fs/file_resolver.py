"""Resolver deterministico path file (RapidFuzz), senza LLM.

Vive nel pacchetto specialista FS (come `gmail/read.py`): i tool
`append_note` / `read_file` lo usano per mappare la traccia STT sul file
reale. Da una traccia / nome tool («leggimi spesa punto txt») restituisce il
`Path` **relativo** reale nel workspace Desktop, oppure `None` se sotto soglia.

Contratto: nessun I/O di scrittura; solo `rglob` in lettura. I tool chiamanti
in `fs/files.py` fanno poi `(workspace_dir / rel).resolve()` + check `relative_to`.

Numeri: STT e basename vengono canonizzati così
`progetto 03` / `progetto_03` / `progetto-03` / `progetto zero tre` /
`progetto zero3` collassano sulla stessa chiave (`progetto_3`).
"""

from __future__ import annotations

import re
from pathlib import Path

from rapidfuzz import fuzz, process

from lavora_e_guida.config import is_index_skipped_rel

# Stopword di comando/STT italiano: rumore da togliere prima del match sullo stem.
# Ordine lungo→corto nelle frasi multi-token gestito a parte (estensioni pronunciate).
# Include verbi di lettura e di append_note (“aggiungi/aggiorna … spesa”).
_STOPWORDS: frozenset[str] = frozenset(
    {
        "leggimi",
        "leggi",
        "apri",
        "cerca",
        "trova",
        "mostrami",
        "mostra",
        # Comandi tipici append_note: nome tool “sporco” senza il pezzo da scrivere.
        "aggiungi",
        "aggiungimi",
        "aggiorna",
        "aggiornami",
        "appendi",
        "inserisci",
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

# Parole-numero italiane → intero (STT “zero tre” / “venti tre” ↔ cifre sul FS).
# Copre 0–20, decine e cento: abbastanza per nomi tipo progetto_03 / verbale_12.
_IT_NUMBER_WORDS: dict[str, int] = {
    "zero": 0,
    "uno": 1,
    "una": 1,
    "due": 2,
    "tre": 3,
    "quattro": 4,
    "cinque": 5,
    "sei": 6,
    "sette": 7,
    "otto": 8,
    "nove": 9,
    "dieci": 10,
    "undici": 11,
    "dodici": 12,
    "tredici": 13,
    "quattordici": 14,
    "quindici": 15,
    "sedici": 16,
    "diciassette": 17,
    "diciotto": 18,
    "diciannove": 19,
    "venti": 20,
    "trenta": 30,
    "quaranta": 40,
    "cinquanta": 50,
    "sessanta": 60,
    "settanta": 70,
    "ottanta": 80,
    "novanta": 90,
    "cento": 100,
}

# Decine “pure” (20, 30, …): unite all’unità successiva (“venti tre” → 23).
_IT_TENS: frozenset[int] = frozenset({20, 30, 40, 50, 60, 70, 80, 90})

# Spezza token misti lettera/cifra: `zero3` → zero + 3; `progetto03` → progetto + 03.
_ALNUM_CHUNK = re.compile(r"[a-zàèéìòù]+|\d+", re.IGNORECASE)


def _normalize_stem_key(raw: str) -> str:
    """Canonizza separatorivarianti: lower, spazi/trattini → `_`.

    Coerente con `normalize_fs_name` (spazi→`_`) più lower e trattini,
    così STT e basename disco condividono la stessa chiave base.
    """
    # Strip + lower: confronti case-insensitive senza dipendere dal FS.
    s = (raw or "").strip().lower()
    # Trattini e spazi → underscore (Windows/shell-friendly, come create_text_file).
    s = s.replace("-", "_").replace(" ", "_")
    # Collassa `__` ripetuti da doppia sostituzione o rumore STT.
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("_")


def _split_alnum_chunks(token: str) -> list[str]:
    """Spezza un token in pezzi solo-lettere o solo-cifre (lower).

    Esempi: `zero3` → [`zero`,`3`]; `03` → [`03`]; `progetto` → [`progetto`].
    """
    if not token:
        return []
    # findall: ignora eventuali simboli residui tra pezzi.
    return [m.group(0).lower() for m in _ALNUM_CHUNK.finditer(token)]


def _token_to_digit_str(token: str) -> str:
    """Se il token è cifra o parola-numero IT → stringa numerica; altrimenti invariato.

    `03` resta `03` (la compressione leading-zero avviene nel merge run).
    `tre` → `3`; `venti` → `20`; `spesa` → `spesa`.
    """
    # Già solo cifre: non tocchiamo il padding qui.
    if token.isdigit():
        return token
    # Parola-numero nota → cifre decimali senza padding.
    if token in _IT_NUMBER_WORDS:
        return str(_IT_NUMBER_WORDS[token])
    return token


def _merge_numeric_run(digit_tokens: list[str]) -> str:
    """Fonde una run di token già numerici in un unico intero canonico.

    Regole (in ordine):
    - decina (20..90) + unità 1..9 → somma (`20`,`3` → `23`);
    - `100` + n < 100 → somma (`100`,`3` → `103`);
    - altrimenti concatenazione cifre (`0`,`3` → `03` → int → `3`).
    Così `progetto_03` e `progetto zero tre` condividono `progetto_3`.
    """
    if not digit_tokens:
        return ""

    # Accumuliamo pezzi stringa; a volte sostituiamo l’ultimo (decine+unità).
    pieces: list[str] = [digit_tokens[0]]
    for nxt in digit_tokens[1:]:
        prev_i = int(pieces[-1])
        cur_i = int(nxt)
        # “venti tre” / “cento tre”: composizione italiana (decina+unità o cento+resto).
        if (prev_i in _IT_TENS and 1 <= cur_i <= 9) or (
            prev_i == 100 and 0 < cur_i < 100
        ):
            pieces[-1] = str(prev_i + cur_i)
        else:
            # Cifre isolate o padded: le concateniamo poi normalizziamo con int().
            pieces.append(nxt)

    # Una sola composizione già chiusa (es. 23) oppure "0"+"3" → 3.
    combined = "".join(pieces)
    return str(int(combined))


def _number_canonical_key(raw: str) -> str:
    """Chiave stem con numeri unificati (parole↔cifre, padding, separatorivarianti).

    Pipeline: separatorivarianti → chunk alfanumerici → parole-numero→cifre →
    merge run numeriche → re-join con `_`.
    """
    # Prima i separatorivarianti: `progetto-03` e `progetto 03` → stesso split.
    base = _normalize_stem_key(raw)
    if not base:
        return ""

    # Token su `_`, poi spezza misti (`zero3`) e mappa parole-numero.
    flat: list[str] = []
    for tok in base.split("_"):
        for chunk in _split_alnum_chunks(tok):
            flat.append(_token_to_digit_str(chunk))

    if not flat:
        return ""

    # Cammina la lista: run di soli digit → un token; resto letterale invariato.
    out: list[str] = []
    i = 0
    while i < len(flat):
        if flat[i].isdigit():
            # Raccogli la run numerica massima, poi fondila in un int canonico.
            j = i
            run: list[str] = []
            while j < len(flat) and flat[j].isdigit():
                run.append(flat[j])
                j += 1
            out.append(_merge_numeric_run(run))
            i = j
        else:
            out.append(flat[i])
            i += 1

    # Re-normalizza: eventuali vuoti / `__` da pezzi degeneri.
    return _normalize_stem_key("_".join(out))


def _scan_stem_map(workspace_dir: Path) -> dict[str, list[Path]]:
    """Scansiona i file sotto workspace → stem_canonico → path relativi.

    Solo `is_file()`; directory ignorate. Collisioni stesso stem → lista
    (es. `notes/spesa.txt` e `inbox/spesa.pdf`).
    `email_attachments/` è esclusa: stesso contratto dello skip RAG.
    Chiave = `_number_canonical_key(stem)` così `progetto_03` ≡ `progetto_3`.
    """
    root = workspace_dir.resolve()
    stem_map: dict[str, list[Path]] = {}

    # rglob: ricorsivo sotto notes/, inbox/, eventuali sotto-cartelle.
    # Stesso skip RAG: email_attachments/ non è candidato per read_file/append_note.
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
        # Fatture Gmail: find_file/read_file non devono collassare su di esse.
        if is_index_skipped_rel(rel):
            continue
        # Chiave numerica-aware: parola/cifra/padding non devono cambiare il match.
        key = _number_canonical_key(abs_path.stem)
        if not key:
            continue
        stem_map.setdefault(key, []).append(rel)

    return stem_map


def _clean_stt_input(stt_input: str) -> str:
    """Pulisce la traccia STT/tool fino a uno stem confrontabile con la mappa.

    Passi: lower → togli “punto/dot + ext” → togli stopword → numeri canonici.
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

    # Numeri: `zero tre` / `03` / `zero3` → stessa chiave della mappa FS.
    return _number_canonical_key("_".join(kept))


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

    Waterfall: scan → pulizia STT (+ numeri) → exact stem → fuzzy WRatio ≥ threshold.
    `allowed_suffixes` filtra dopo lo scan (es. `{".pdf"}`); `None` = tutti i file.
    """
    # Workspace assente o non directory: niente da risolvere.
    if not workspace_dir.is_dir():
        return None

    # 1) Scan: stem numerico-canonico → lista di path relativi.
    stem_map = _scan_stem_map(workspace_dir)
    if not stem_map:
        return None

    # 2) Pulizia STT: stopword + pronunce ext + unificazione numeri.
    cleaned = _clean_stt_input(stt_input)
    if not cleaned:
        return None

    # 3a) Exact: chiave mappa == cleaned (già number-aware).
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
