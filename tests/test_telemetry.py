"""Test telemetria STT: parser token, store SQLite, un turno del loop vocale."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any

from lavora_e_guida.agent import run_chat_loop
from lavora_e_guida.audio.mock import MockSTT, MockTTS
from lavora_e_guida.llm.errors import LLMError
from lavora_e_guida.llm.turn import FunctionCall, LlmTurn
from lavora_e_guida.llm.usage import (
    TokenUsage,
    parse_gemini_usage,
    parse_ollama_usage,
)
from lavora_e_guida.telemetry import TelemetryDB, db_path

# ---------------------------------------------------------------------------
# Parser Ollama / Gemini
# ---------------------------------------------------------------------------


def test_parse_ollama_usage_real_payload() -> None:
    """Campi `prompt_eval_count` / `eval_count` da un JSON /api/chat reale."""
    payload = {
        "model": "qwen2.5:3b",
        "created_at": "2026-08-13T12:00:00.000000000Z",
        "message": {"role": "assistant", "content": '{"tool":"none","reply":"ok"}'},
        "done": True,
        "prompt_eval_count": 256,
        "eval_count": 64,
    }
    usage = parse_ollama_usage(payload)
    assert usage.prompt_tokens == 256
    assert usage.completion_tokens == 64


def test_parse_ollama_usage_missing_or_invalid_fields() -> None:
    """Payload non-dict, campi assenti, bool o negativi → 0 (mai eccezione)."""
    assert parse_ollama_usage(None) == TokenUsage()
    assert parse_ollama_usage("non-oggetto") == TokenUsage()
    assert parse_ollama_usage({}) == TokenUsage(0, 0)
    # bool è sottoclasse di int: non deve diventare 1 token fittizio.
    assert parse_ollama_usage({"prompt_eval_count": True, "eval_count": False}) == TokenUsage()
    assert parse_ollama_usage({"prompt_eval_count": -3, "eval_count": "12"}) == TokenUsage()


def test_parse_gemini_usage_real_payload() -> None:
    """Campi `usageMetadata.promptTokenCount` / `candidatesTokenCount`."""
    payload = {
        "candidates": [
            {
                "content": {"parts": [{"text": '{"tool":"none","reply":"ok"}'}], "role": "model"},
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 180,
            "candidatesTokenCount": 22,
            "totalTokenCount": 202,
        },
    }
    usage = parse_gemini_usage(payload)
    assert usage.prompt_tokens == 180
    assert usage.completion_tokens == 22


def test_parse_gemini_usage_missing_metadata() -> None:
    """usageMetadata assente, non-oggetto o campi mancanti → TokenUsage(0, 0)."""
    assert parse_gemini_usage(None) == TokenUsage()
    assert parse_gemini_usage({}) == TokenUsage()
    assert parse_gemini_usage({"usageMetadata": "nope"}) == TokenUsage()
    assert parse_gemini_usage({"usageMetadata": {}}) == TokenUsage(0, 0)


def test_token_usage_add_sums_rounds() -> None:
    """Somma di due round: usata dal loop per aggregare un turno STT."""
    first = TokenUsage(prompt_tokens=100, completion_tokens=20)
    second = TokenUsage(prompt_tokens=80, completion_tokens=15)
    assert first + second == TokenUsage(prompt_tokens=180, completion_tokens=35)
    # sum() parte da 0: __radd__ deve accettare l'intero.
    assert sum([first, second]) == TokenUsage(prompt_tokens=180, completion_tokens=35)


# ---------------------------------------------------------------------------
# Store SQLite
# ---------------------------------------------------------------------------


def test_insert_and_read_stt_request(tmp_path: Path) -> None:
    """Una insert produce una riga con token e testo TTS letti identici."""
    store_path = db_path(tmp_path)
    with TelemetryDB(store_path) as db:
        ok = db.insert_stt_request(
            started_at="2026-08-13T13:00:00+00:00",
            ended_at="2026-08-13T13:00:02+00:00",
            token_input=120,
            token_output=40,
            tts_response="Ho aggiunto latte alla spesa",
        )
        assert ok is True
        rows = db.list_stt_requests()

    assert len(rows) == 1
    rec = rows[0]
    assert rec.id == 1
    assert rec.started_at == "2026-08-13T13:00:00+00:00"
    assert rec.ended_at == "2026-08-13T13:00:02+00:00"
    assert rec.token_input == 120
    assert rec.token_output == 40
    assert rec.tts_response == "Ho aggiunto latte alla spesa"
    assert store_path.is_file()


def test_two_inserts_sum_style_rows(tmp_path: Path) -> None:
    """Due turni → due righe; token restano per-turno, non aggregati dallo store."""
    with TelemetryDB(tmp_path / "telemetry.db") as db:
        db.insert_stt_request(
            started_at="2026-08-13T13:01:00+00:00",
            ended_at="2026-08-13T13:01:01+00:00",
            token_input=10,
            token_output=5,
            tts_response="primo",
        )
        db.insert_stt_request(
            started_at="2026-08-13T13:02:00+00:00",
            ended_at="2026-08-13T13:02:01+00:00",
            token_input=20,
            token_output=8,
            tts_response="secondo",
        )
        rows = db.list_stt_requests()

    assert [r.tts_response for r in rows] == ["primo", "secondo"]
    assert [r.token_input for r in rows] == [10, 20]
    assert [r.id for r in rows] == [1, 2]


def test_insert_failure_does_not_raise(tmp_path: Path) -> None:
    """Path non scrivibile → False e log, niente eccezione verso il caller."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    # File al posto della directory padre: SQLite non può creare telemetry.db.
    parent_as_file = blocked / "not_a_dir"
    parent_as_file.write_text("occupato", encoding="utf-8")
    db = TelemetryDB(parent_as_file / "telemetry.db")
    ok = db.insert_stt_request(
        started_at="2026-08-13T13:03:00+00:00",
        ended_at="2026-08-13T13:03:01+00:00",
        tts_response="non deve finire in db",
    )
    assert ok is False


# ---------------------------------------------------------------------------
# Loop vocale: mock LLM + MockSTT/MockTTS
# ---------------------------------------------------------------------------


class _ScriptedLLM:
    """Mock SupportsChat: coda di (LlmTurn, TokenUsage) per round consecutivi."""

    def __init__(self, script: list[tuple[LlmTurn, TokenUsage]]) -> None:
        # Copia: pop(0) non deve mutare lo script del caller.
        self._script = list(script)
        self.last_usage = TokenUsage()
        self.calls = 0

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> LlmTurn:
        self.calls += 1
        if not self._script:
            raise AssertionError("chat chiamata oltre lo script del mock")
        turn, usage = self._script.pop(0)
        self.last_usage = usage
        return turn

    def close(self) -> None:
        return None


def test_chat_loop_records_one_row_with_summed_tokens(tmp_path: Path) -> None:
    """Un turno a due round → una riga: token sommati e testo TTS della reply."""
    # Round 1: tool inventato (niente I/O FS); round 2: reply parlata.
    llm = _ScriptedLLM(
        [
            (
                LlmTurn(
                    function_calls=(FunctionCall(name="inventato", args={}),),
                ),
                TokenUsage(prompt_tokens=100, completion_tokens=20),
            ),
            (
                LlmTurn(text="Ho aggiunto latte alla spesa"),
                TokenUsage(prompt_tokens=80, completion_tokens=15),
            ),
        ]
    )
    # Due listen: turno utile + esci (esci non deve produrre una seconda riga).
    infile = StringIO("Aggiungi latte alla spesa\nesci\n")
    outfile = StringIO()
    stt = MockSTT(infile=infile, outfile=outfile, prompt="")
    tts = MockTTS(outfile=outfile, prefix="[TTS] ")
    db_file = tmp_path / "telemetry.db"

    code = run_chat_loop(
        stt,
        tts,
        llm,
        report_latency=False,
        telemetry_db=db_file,
    )

    assert code == 0
    assert llm.calls == 2
    spoken = outfile.getvalue()
    # Intro + reply LLM + arrivederci: solo la reply finisce in SQLite.
    assert "Ho aggiunto latte alla spesa" in spoken
    assert "Arrivederci." in spoken

    with TelemetryDB(db_file) as db:
        rows = db.list_stt_requests()

    assert len(rows) == 1
    rec = rows[0]
    assert rec.token_input == 180
    assert rec.token_output == 35
    assert rec.tts_response == "Ho aggiunto latte alla spesa"
    # ISO-8601 UTC: started_at <= ended_at (stesso secondo è lecito).
    assert "T" in rec.started_at
    assert rec.started_at <= rec.ended_at


def test_chat_loop_skips_intro_empty_and_exit(tmp_path: Path) -> None:
    """Intro, riga vuota e `esci` non inseriscono righe in stt_requests."""
    llm = _ScriptedLLM([])
    db_file = tmp_path / "telemetry.db"
    for text in ("esci\n", "   \n"):
        outfile = StringIO()
        code = run_chat_loop(
            MockSTT(infile=StringIO(text), outfile=outfile, prompt=""),
            MockTTS(outfile=outfile),
            llm,
            report_latency=False,
            telemetry_db=db_file,
        )
        assert code == 0
    assert llm.calls == 0
    # Nessun insert → il file può anche non esistere (connect lazy).
    if db_file.is_file():
        with TelemetryDB(db_file) as db:
            assert db.list_stt_requests() == []


def test_chat_loop_records_llm_error_row(tmp_path: Path) -> None:
    """chat() che alza LLMError: riga con 0 token e testo TTS dell'errore."""

    class _FailingLLM:
        last_usage = TokenUsage()

        def chat(self, *args: object, **kwargs: object) -> str:
            raise LLMError("daemon spento")

        def close(self) -> None:
            return None

    infile = StringIO("ciao\nesci\n")
    outfile = StringIO()
    db_file = tmp_path / "telemetry.db"
    code = run_chat_loop(
        MockSTT(infile=infile, outfile=outfile, prompt=""),
        MockTTS(outfile=outfile, prefix="[TTS] "),
        _FailingLLM(),
        report_latency=False,
        telemetry_db=db_file,
    )
    assert code == 0
    with TelemetryDB(db_file) as db:
        rows = db.list_stt_requests()
    assert len(rows) == 1
    assert rows[0].token_input == 0
    assert rows[0].token_output == 0
    assert rows[0].tts_response.startswith("Errore LLM:")
    assert "daemon spento" in rows[0].tts_response
