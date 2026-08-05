"""Test Phase 1: Mock echo + client HTTP bridge (senza host reale)."""

from __future__ import annotations

import json
from io import StringIO

import httpx
import pytest

from lavora_e_guida.audio.factory import create_audio_pair, create_stt, create_tts
from lavora_e_guida.audio.http_bridge import HttpBridgeSTT, HttpBridgeTTS
from lavora_e_guida.audio.mock import MockSTT, MockTTS
from lavora_e_guida.config import Settings
from lavora_e_guida.main import run_echo_loop


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


def test_echo_loop_mock_end_to_end() -> None:
    """Criterio Phase 1: listen → echo → speak senza audio reale."""
    infile = StringIO("prova echo\nesci\n")
    outfile = StringIO()
    stt = MockSTT(infile=infile, outfile=outfile, prompt="")
    tts = MockTTS(outfile=outfile, prefix="[TTS] ")
    code = run_echo_loop(stt, tts)
    assert code == 0
    spoken = outfile.getvalue()
    # Introduzione + echo della frase + saluto di chiusura.
    assert "Pronto." in spoken
    assert "Hai detto: prova echo" in spoken
    assert "Arrivederci." in spoken


def test_echo_loop_empty_input_exits() -> None:
    """Enter a vuoto / transcript blank → uscita senza crash."""
    infile = StringIO("   \n")
    outfile = StringIO()
    stt = MockSTT(infile=infile, outfile=outfile, prompt="")
    tts = MockTTS(outfile=outfile)
    assert run_echo_loop(stt, tts) == 0
    assert "Nessun input" in outfile.getvalue()


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
