"""Test resolver deterministico RapidFuzz + smoke tool FS/PDF (tmp_path)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lavora_e_guida.tools.file_resolver import (
    _clean_stt_input,
    _number_canonical_key,
    resolve_file_path,
)
from lavora_e_guida.tools.fs import append_note, read_file


def _write_minimal_pdf(path: Path, line: str = "Verbale lab Ollama") -> None:
    """Scrive un PDF 1.4 minimale con una riga di testo estraibile da pypdf.

    Niente reportlab: byte fissi, abbastanza validi per `PdfReader.extract_text`.
    """
    # Stream content: Helvetica + una stringa literal (ASCII, niente escape).
    content = f"BT /F1 12 Tf 50 100 Td ({line}) Tj ET\n"
    # Length dello stream: pypdf e parser PDF richiedono /Length coerente.
    stream = content.encode("ascii")
    # Oggetti PDF numerati: Catalog → Pages → Page → Contents → Font.
    body = (
        b"%PDF-1.4\n"
        b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n"
        b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n"
        b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] "
        b"/Contents 4 0 R /Resources<< /Font<< /F1 5 0 R >> >> >>endobj\n"
        b"4 0 obj<< /Length "
        + str(len(stream)).encode("ascii")
        + b" >>stream\n"
        + stream
        + b"endstream\nendobj\n"
        b"5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj\n"
    )
    # xref minimale: offset fissi non critici per pypdf (usa oggetti inline).
    # Alcuni parser accettano trailer senza xref completo; pypdf sì su stream corto.
    eof = (
        b"xref\n0 6\n0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000058 00000 n \n"
        b"0000000115 00000 n \n"
        b"0000000266 00000 n \n"
        b"0000000349 00000 n \n"
        b"trailer<< /Size 6 /Root 1 0 R >>\n"
        b"startxref\n429\n%%EOF\n"
    )
    # Parent: crea directory se manca (inbox/ in fixture).
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body + eof)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Workspace lab finto: notes/spesa.txt + inbox/spesa.pdf (stesso stem)."""
    # notes/: file testo di riferimento per exact / fuzzy / read_file.
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "spesa.txt").write_text("latte e pane\n", encoding="utf-8")
    # inbox/: omonimo PDF per filtro allowed_suffixes e smoke hint PDF.
    _write_minimal_pdf(tmp_path / "inbox" / "spesa.pdf")
    return tmp_path


def test_resolve_exact_stt_leggi_spesa(workspace: Path) -> None:
    """STT pulito 'leggi spesa' → match exact sullo stem → notes/spesa.txt."""
    # Comando parlato tipico: stopword 'leggi' + stem 'spesa'.
    rel = resolve_file_path("leggi spesa", workspace)
    # Contratto: Path relativo (posix-friendly as_posix nei tool).
    assert rel == Path("notes/spesa.txt")


def test_resolve_fuzzy_typo_spessa(workspace: Path) -> None:
    """Typo STT 'spessa' con WRatio ≥ 70 → stesso file notes/spesa.txt."""
    # Typo vocale comune: doppia s; score atteso ~90 su stem 'spesa'.
    rel = resolve_file_path("spessa", workspace)
    assert rel == Path("notes/spesa.txt")


def test_resolve_fuzzy_under_threshold_returns_none(workspace: Path) -> None:
    """Query senza affinità con nessuno stem → None (sotto soglia default 70)."""
    # Stringa inventata: WRatio vs 'spesa' resta molto basso (~0–20).
    rel = resolve_file_path("xyzabcqqq", workspace)
    assert rel is None


def test_resolve_fuzzy_respects_custom_threshold(workspace: Path) -> None:
    """Con threshold alto (99) anche un buon typo 'spessa' viene rifiutato."""
    # Stesso input del test fuzzy positivo, ma soglia impossibile → None.
    rel = resolve_file_path("spessa", workspace, threshold=99)
    assert rel is None


def test_clean_stt_strips_stopwords_and_punto_txt() -> None:
    """Pulizia: stopword + 'punto txt' → stem canonico 'spesa'."""
    # Frase STT rumorosa tipica del lab hands-free.
    cleaned = _clean_stt_input("leggimi il file spesa punto txt")
    assert cleaned == "spesa"


def test_resolve_stopwords_and_ext_pronunciation(workspace: Path) -> None:
    """End-to-end: frase con stopword e 'punto txt' risolve notes/spesa.txt."""
    rel = resolve_file_path("leggimi il file spesa punto txt", workspace)
    assert rel == Path("notes/spesa.txt")


def test_allowed_suffixes_pdf_skips_txt_homonym(workspace: Path) -> None:
    """Con allowed_suffixes={.pdf} non ritorna spesa.txt omonimo."""
    # Stesso stem su disco; filtro post-scan deve scegliere solo il PDF.
    rel = resolve_file_path(
        "spesa",
        workspace,
        allowed_suffixes=frozenset({".pdf"}),
    )
    assert rel == Path("inbox/spesa.pdf")


def test_allowed_suffixes_text_skips_pdf_homonym(workspace: Path) -> None:
    """Con suffix testo, 'spesa' non collassa sul PDF in inbox/."""
    rel = resolve_file_path(
        "spesa",
        workspace,
        allowed_suffixes=frozenset({".txt", ".md", ".json"}),
    )
    assert rel == Path("notes/spesa.txt")


def test_tool_smoke_read_file_prefers_text(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """read_file senza hint PDF preferisce notes/spesa.txt sull'omonimo PDF."""
    # Isolamento: i tool usano WORKSPACE_ROOT importato a module level.
    monkeypatch.setattr(
        "lavora_e_guida.tools.fs.WORKSPACE_ROOT",
        workspace,
    )
    # Nome come lo emetterebbe STT/LLM senza cartella né estensione.
    out = read_file("leggi spesa")
    assert out.startswith("OK: contenuto di notes/spesa.txt")
    assert "latte e pane" in out


@pytest.mark.parametrize(
    "raw_name",
    [
        # Basename con suffix: tipico output LLM dopo STT.
        "spesa.pdf",
        # Pronuncia STT: «punto pdf» deve attivare lo stesso hint.
        "leggi spesa punto pdf",
    ],
)
def test_tool_smoke_read_file_pdf_hint(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_name: str,
) -> None:
    """read_file con hint PDF (.pdf o «punto pdf») → inbox/spesa.pdf, non .txt."""
    monkeypatch.setattr(
        "lavora_e_guida.tools.fs.WORKSPACE_ROOT",
        workspace,
    )
    # Hint esplicito: omonimo testo ignorato a favore del PDF.
    out = read_file(raw_name)
    assert out.startswith("OK: contenuto di inbox/spesa.pdf")
    assert "estratto" in out
    assert "Verbale lab Ollama" in out


def test_tool_smoke_append_note_dirty_name(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """append_note con nome sporco appende sul match fuzzy esistente (non crea)."""
    monkeypatch.setattr(
        "lavora_e_guida.tools.fs.WORKSPACE_ROOT",
        workspace,
    )
    # 'spessa' → fuzzy su notes/spesa.txt; action=aggiornata nel messaggio.
    out = append_note("spessa", "uova")
    assert "OK: nota aggiornata notes/spesa.txt" in out
    # Verifica side-effect: append con newline se il file già terminava con \n.
    body = (workspace / "notes" / "spesa.txt").read_text(encoding="utf-8")
    assert body == "latte e pane\nuova"


@pytest.fixture
def workspace_progetto(tmp_path: Path) -> Path:
    """Workspace con notes/progetto_03.txt per varianti numero/STT."""
    notes = tmp_path / "notes"
    notes.mkdir()
    # Sul disco: underscore + zero-padding tipico Windows/lab.
    (notes / "progetto_03.txt").write_text("bozza\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("progetto_03", "progetto_3"),
        ("progetto-03", "progetto_3"),
        ("progetto 03", "progetto_3"),
        ("progetto zero tre", "progetto_3"),
        ("progetto zero3", "progetto_3"),
        ("progetto 0 3", "progetto_3"),
        ("progetto tre", "progetto_3"),
        ("progetto_3", "progetto_3"),
        ("verbale venti tre", "verbale_23"),
        ("verbale_23", "verbale_23"),
    ],
)
def test_number_canonical_key_variants(raw: str, expected: str) -> None:
    """Parole-numero, padding e separatorivarianti collassano sulla stessa chiave."""
    assert _number_canonical_key(raw) == expected


@pytest.mark.parametrize(
    "stt",
    [
        "progetto 03",
        "progetto_03",
        "progetto-03",
        "progetto zero tre",
        "progetto zero3",
        "leggi progetto zero tre",
        "apri il file progetto 03",
        "progetto tre",
    ],
)
def test_resolve_progetto_number_variants(workspace_progetto: Path, stt: str) -> None:
    """STT con cifre/parole/separatori → stesso notes/progetto_03.txt."""
    rel = resolve_file_path(stt, workspace_progetto)
    assert rel == Path("notes/progetto_03.txt")


def test_clean_stt_number_words_to_digits() -> None:
    """Dopo stopword, 'zero tre' diventa chiave numerica canonica."""
    assert _clean_stt_input("leggi progetto zero tre") == "progetto_3"


def test_resolve_does_not_crash_on_extra_dirs(tmp_path: Path) -> None:
    """Sotto-cartelle arbitrarie non rompono lo scan stem (sanity check)."""
    notes = tmp_path / "notes"
    notes.mkdir()
    (notes / "marker.txt").write_text("ok\n", encoding="utf-8")
    # Cartella extra con file omonimo: entrambi entrano nella mappa, il resolver sceglie.
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "marker.txt").write_text("altro\n", encoding="utf-8")
    rel = resolve_file_path("marker", tmp_path)
    # Uno dei due viene scelto (entrambi validi): basta che non sia None.
    assert rel is not None
    assert rel.name == "marker.txt"
