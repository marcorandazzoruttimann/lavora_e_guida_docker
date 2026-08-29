"""Regola TTS condivisa e pulizia markdown prima di edge-tts."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

from lavora_e_guida.agent import MASTER_LOOP_SPEC, run_chat_loop
from lavora_e_guida.audio.mock import MockSTT, MockTTS
from lavora_e_guida.gmail.agent import GMAIL_LOOP_SPEC
from lavora_e_guida.llm.spoken import SPOKEN_REPLY_RULE, prepare_spoken_text
from lavora_e_guida.llm.turn import LlmTurn
from lavora_e_guida.llm.usage import TokenUsage
from lavora_e_guida.web.agent import WEB_LOOP_SPEC


def test_spoken_reply_rule_is_in_every_loop_spec() -> None:
    """FS, Gmail e web condividono lo stesso vincolo TTS: niente drift tra specialisti."""
    assert SPOKEN_REPLY_RULE in MASTER_LOOP_SPEC.system_prompt
    assert SPOKEN_REPLY_RULE in GMAIL_LOOP_SPEC.system_prompt
    assert SPOKEN_REPLY_RULE in WEB_LOOP_SPEC.system_prompt


def test_prepare_spoken_text_strips_markdown_email_list() -> None:
    """Replica di una reply Gemini da elenco inbox: markup → frasi parlabili."""
    raw = (
        "1. **Da:** Mail Delivery Subsystem - **Oggetto:** "
        "Delivery Status Notification (Delay)\n"
        "2. **Da:** noicompriamoauto.it - **Oggetto:** "
        "Ottieni la tua offerta reale aggiornata\n"
        "3. **Da:** Crypto.com - **Oggetto:** EUR purchase complete"
    )
    spoken = prepare_spoken_text(raw)
    assert "**" not in spoken
    assert "Da:" not in spoken
    assert "Oggetto:" not in spoken
    assert "(" not in spoken
    assert ")" not in spoken
    assert " - " not in spoken
    assert "da Mail Delivery Subsystem" in spoken
    assert "oggetto Delivery Status Notification Delay" in spoken
    assert "da Crypto.com" in spoken
    assert "oggetto EUR purchase complete" in spoken


def test_prepare_spoken_text_keeps_plain_italian() -> None:
    """Frase già parlabile: identica, niente riscrittura inutile."""
    plain = "La prima è da Mario, oggetto Fattura."
    assert prepare_spoken_text(plain) == plain


def test_loop_speaks_cleaned_markdown(tmp_path: Path) -> None:
    """Il TTS riceve il testo pulito; la storia tiene il crudo del modello."""

    class _MarkdownLLM:
        """Simula Gemini che elenca con grassetto: il loop deve spezzarlo."""

        last_usage = TokenUsage()

        def chat(self, *args: object, **kwargs: object) -> LlmTurn:
            return LlmTurn(text="1. **Da:** Mario - **Oggetto:** Fattura")

        def close(self) -> None:
            return None

    outfile = StringIO()
    code = run_chat_loop(
        MockSTT(infile=StringIO("ultime email\nesci\n"), outfile=outfile, prompt=""),
        MockTTS(outfile=outfile, prefix="[TTS] "),
        _MarkdownLLM(),
        report_latency=False,
        telemetry_db=tmp_path / "telemetry.db",
    )
    assert code == 0
    spoken = outfile.getvalue()
    assert "**" not in spoken
    assert "Da:" not in spoken
    assert "da Mario" in spoken
    assert "oggetto Fattura" in spoken
