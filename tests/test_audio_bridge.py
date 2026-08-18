"""Test audio Mock + client HTTP bridge (senza host reale)."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import httpx
import pytest

from lavora_e_guida.agent import LoopSpec, run_chat_loop
from lavora_e_guida.audio.factory import create_audio_pair, create_stt, create_tts
from lavora_e_guida.audio.http_bridge import HttpBridgeSTT, HttpBridgeTTS
from lavora_e_guida.audio.mock import MockSTT, MockTTS
from lavora_e_guida.config import Settings
from lavora_e_guida.llm.usage import TokenUsage


def test_mock_listen_returns_line() -> None:
    """MockSTT legge una riga da infile e toglie solo il newline."""
    infile = StringIO("ciao mondo\n")
    outfile = StringIO()
    stt = MockSTT(infile=infile, outfile=outfile, prompt="> ")
    assert stt.listen() == "ciao mondo"
    # Il prompt deve comparire sul canale di controllo prima dell'attesa.
    assert outfile.getvalue() == "> "


def test_mock_listen_eof_returns_empty() -> None:
    """EOF (stream vuoto) → stringa vuota, così il loop può uscire soft."""
    stt = MockSTT(infile=StringIO(""), outfile=StringIO())
    assert stt.listen() == ""


def test_mock_speak_writes_prefixed_line() -> None:
    """MockTTS stampa con prefisso stabile per assert e debug a occhio."""
    out = StringIO()
    tts = MockTTS(outfile=out, prefix="[TTS] ")
    tts.speak("prova")
    assert out.getvalue() == "[TTS] prova\n"


def test_agent_loop_mock_end_to_end(tmp_path: Path) -> None:
    """Audio Mock + loop vocale (LLM fake): regressione del canale I/O."""

    class _FakeLLM:
        """Una sola reply parlata: niente tool FS, solo canale STT/TTS."""

        last_usage = TokenUsage()

        def chat(self, *args: object, **kwargs: object) -> str:
            return '{"tool": "none", "reply": "Ciao dal loop vocale"}'

        def close(self) -> None:
            return None

    infile = StringIO("ciao\nesci\n")
    outfile = StringIO()
    stt = MockSTT(infile=infile, outfile=outfile, prompt="")
    tts = MockTTS(outfile=outfile, prefix="[TTS] ")
    # DB temporaneo: non toccare runtime/telemetry.db del repo.
    code = run_chat_loop(
        stt,
        tts,
        _FakeLLM(),
        report_latency=False,
        telemetry_db=tmp_path / "telemetry.db",
    )
    assert code == 0
    spoken = outfile.getvalue()
    # Introduzione lab + reply LLM + saluto di chiusura.
    assert "Lab Ollama FS" in spoken
    assert "Ciao dal loop vocale" in spoken
    assert "Arrivederci." in spoken


def test_agent_loop_empty_input_exits(tmp_path: Path) -> None:
    """Enter a vuoto / transcript blank → uscita senza crash."""

    class _UnusedLLM:
        def chat(self, *args: object, **kwargs: object) -> str:
            raise AssertionError("LLM non deve essere chiamato su input vuoto")

        def close(self) -> None:
            return None

    infile = StringIO("   \n")
    outfile = StringIO()
    stt = MockSTT(infile=infile, outfile=outfile, prompt="")
    tts = MockTTS(outfile=outfile)
    assert (
        run_chat_loop(
            stt,
            tts,
            _UnusedLLM(),
            report_latency=False,
            telemetry_db=tmp_path / "telemetry.db",
        )
        == 0
    )
    assert "Nessun input" in outfile.getvalue()


def test_agent_loop_uses_custom_spec(tmp_path: Path) -> None:
    """spec diverso dal master: intro, prompt, dispatch e stampa custom."""

    printed: list[tuple[str, str]] = []
    seen_system: list[str] = []

    def dispatch(tool: str, args: dict[str, object]) -> str:
        # Lo spec, non il master FS, deve eseguire questo tool.
        assert tool == "list_emails"
        assert args.get("query") == "inbox"
        return "OK: 1 email in inbox. 1. Da Mario, oggetto Fattura."

    spec = LoopSpec(
        system_prompt="Sei l'agente Gmail di test.",
        dispatch=dispatch,
        intro_text="Agente Gmail in sola lettura. Di' esci per terminare.",
        schema_hint='{"tool":"none","reply":"string"}',
        print_tool_result=lambda tool, result: printed.append((tool, result)),
    )

    class _FakeLLM:
        """Primo round: tool dello spec; secondo: reply parlata."""

        last_usage = TokenUsage()
        calls = 0

        def chat(self, messages: list[dict[str, str]], *args: object, **kwargs: object) -> str:
            self.calls += 1
            if self.calls == 1:
                seen_system.append(messages[0]["content"])
                return '{"tool": "list_emails", "args": {"query": "inbox"}}'
            return '{"tool": "none", "reply": "Hai una email da Mario."}'

        def close(self) -> None:
            return None

    infile = StringIO("ultime email\nesci\n")
    outfile = StringIO()
    llm = _FakeLLM()
    code = run_chat_loop(
        MockSTT(infile=infile, outfile=outfile, prompt=""),
        MockTTS(outfile=outfile, prefix="[TTS] "),
        llm,
        report_latency=False,
        telemetry_db=tmp_path / "telemetry.db",
        spec=spec,
    )
    assert code == 0
    spoken = outfile.getvalue()
    # Intro dello spec, non quella del master FS.
    assert "Agente Gmail in sola lettura" in spoken
    assert "Lab Ollama FS" not in spoken
    assert "Hai una email da Mario." in spoken
    assert seen_system == ["Sei l'agente Gmail di test."]
    assert printed == [
        ("list_emails", "OK: 1 email in inbox. 1. Da Mario, oggetto Fattura."),
    ]
    assert llm.calls == 2


def test_tool_loop_key_prefers_query_then_name() -> None:
    """Anti-loop: query se presente (list/find), altrimenti name (read)."""
    from lavora_e_guida.agent import _tool_loop_key

    assert _tool_loop_key("list_emails", {"query": "is:unread"}) == (
        "list_emails|is:unread"
    )
    # Query vuota → fallback su name (read_email / read_file).
    assert _tool_loop_key("read_email", {"name": "la prima"}) == "read_email|la prima"
    assert _tool_loop_key("find_file", {"query": "  Cetrioli  "}) == "find_file|cetrioli"
    # Se arrivano entrambe, vince query (contratto dello spec riusabile).
    assert _tool_loop_key(
        "list_emails",
        {"query": "inbox", "name": "1"},
    ) == "list_emails|inbox"


def _make_bridge_handler(
    *,
    transcript: str = "trascrizione host",
    speak_sink: list[str] | None = None,
) -> httpx.MockTransport:
    """Transport httpx che simula POST /listen e POST /speak del host."""

    sink = speak_sink if speak_sink is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/listen"):
            return httpx.Response(200, json={"transcript": transcript})
        if request.method == "POST" and path.endswith("/speak"):
            # content è già bytes: niente stream da consumare due volte.
            payload = json.loads(request.content.decode("utf-8"))
            sink.append(payload["text"])
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


def test_http_bridge_listen_parses_transcript() -> None:
    """HttpBridgeSTT estrae `transcript` dal JSON del host."""
    transport = _make_bridge_handler(transcript="parlato dall'host")
    client = httpx.Client(transport=transport)
    stt = HttpBridgeSTT("http://192.168.1.1:8765", client=client)
    try:
        assert stt.listen() == "parlato dall'host"
    finally:
        client.close()


def test_http_bridge_speak_posts_text_json() -> None:
    """HttpBridgeTTS invia `{"text": ...}` a POST /speak."""
    spoken: list[str] = []
    transport = _make_bridge_handler(speak_sink=spoken)
    client = httpx.Client(transport=transport)
    tts = HttpBridgeTTS("http://192.168.1.1:8765", client=client)
    try:
        tts.speak("ciao dal wsl")
    finally:
        client.close()
    assert spoken == ["ciao dal wsl"]


def test_http_bridge_listen_http_error_propagates() -> None:
    """Errori HTTP non devono essere silenziati (il loop li gestirà in Phase 4)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "busy"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    stt = HttpBridgeSTT("http://example.test:8765", client=client)
    try:
        with pytest.raises(httpx.HTTPStatusError):
            stt.listen()
    finally:
        client.close()


def test_factory_respects_audio_driver_mock() -> None:
    """AUDIO_DRIVER=mock → coppia MockSTT/MockTTS."""
    settings = Settings(audio_driver="mock")
    stt, tts = create_audio_pair(settings)
    assert isinstance(stt, MockSTT)
    assert isinstance(tts, MockTTS)


def test_factory_respects_audio_driver_http() -> None:
    """AUDIO_DRIVER=http → coppia HttpBridge* sull'URL risolto."""
    settings = Settings(
        audio_driver="http",
        windows_host="10.0.0.5",
        windows_audio_port=8765,
    )
    stt = create_stt(settings)
    tts = create_tts(settings)
    try:
        assert isinstance(stt, HttpBridgeSTT)
        assert isinstance(tts, HttpBridgeTTS)
        assert settings.audio_bridge_url == "http://10.0.0.5:8765"
    finally:
        # Evitiamo socket aperti residui nei worker pytest.
        stt.close()
        tts.close()
