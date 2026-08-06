"""Test Phase 2: intent stub, state machine, client Ollama mockato."""

from __future__ import annotations

import json
from io import StringIO

import httpx
import pytest

from lavora_e_guida.audio.mock import MockSTT, MockTTS
from lavora_e_guida.llm.local_ollama import LocalOllama, OllamaError
from lavora_e_guida.main import run_agent_loop
from lavora_e_guida.orchestrator.states import LoopState
from lavora_e_guida.routing.intent import (
    IntentClassifier,
    IntentLabel,
    classify_heuristic,
    stub_response_for,
)


def test_loop_states_are_ordered_strings() -> None:
    """Gli stati espongono valori stabili per logging / TTS status futuri."""
    assert LoopState.LISTENING.value == "listening"
    assert [s.value for s in LoopState] == [
        "listening",
        "thinking",
        "executing",
        "speaking",
    ]


def test_heuristic_system_file() -> None:
    result = classify_heuristic("elenca i file nella cartella home")
    assert result.label == IntentLabel.SYSTEM_FILE
    assert result.source == "heuristic"


def test_heuristic_cursor() -> None:
    result = classify_heuristic("chiedi a Cursor di spiegare il bug")
    assert result.label == IntentLabel.CURSOR


def test_heuristic_web_email() -> None:
    result = classify_heuristic("controlla le email non lette su gmail")
    assert result.label == IntentLabel.WEB_EMAIL


def test_heuristic_general_fallback() -> None:
    result = classify_heuristic("che tempo fa domani")
    assert result.label == IntentLabel.GENERAL


def test_classifier_uses_ollama_json() -> None:
    """IntentClassifier parsifica JSON Ollama e mappa l'etichetta."""

    class FakeLLM:
        model = "fake"

        def generate(self, prompt: str, **kwargs: object) -> str:
            return json.dumps({"intent": "CURSOR", "confidence": 0.91})

    result = IntentClassifier(FakeLLM()).classify("analizza il codice")
    assert result.label == IntentLabel.CURSOR
    assert result.source == "ollama"
    assert result.confidence == pytest.approx(0.91)


def test_classifier_falls_back_on_bad_json() -> None:
    class FakeLLM:
        model = "fake"

        def generate(self, prompt: str, **kwargs: object) -> str:
            return "non è json"

    # "elenca i file" → heuristic SYSTEM_FILE se JSON fallisce.
    result = IntentClassifier(FakeLLM()).classify("elenca i file")
    assert result.label == IntentLabel.SYSTEM_FILE
    assert result.source == "heuristic"


def test_stub_response_mentions_intent() -> None:
    text = stub_response_for(IntentLabel.GENERAL, "ciao")
    assert "generale" in text.casefold() or "GENERAL" in text or "stub" in text.casefold()
    assert "ciao" in text


def test_agent_loop_mock_end_to_end() -> None:
    """Criterio Phase 2: listen → classify → stub speak senza audio reale."""
    infile = StringIO("che ore sono\nesci\n")
    outfile = StringIO()
    stt = MockSTT(infile=infile, outfile=outfile, prompt="")
    tts = MockTTS(outfile=outfile, prefix="[TTS] ")
    # Heuristic-only: nessun daemon richiesto nei CI.
    code = run_agent_loop(stt, tts, IntentClassifier(llm=None))
    assert code == 0
    spoken = outfile.getvalue()
    assert "Pronto." in spoken
    assert "Intent GENERAL" in spoken
    assert "Arrivederci." in spoken


def test_agent_loop_empty_input_exits() -> None:
    infile = StringIO("   \n")
    outfile = StringIO()
    stt = MockSTT(infile=infile, outfile=outfile, prompt="")
    tts = MockTTS(outfile=outfile)
    assert run_agent_loop(stt, tts, IntentClassifier(llm=None)) == 0
    assert "Nessun input" in outfile.getvalue()


def _ollama_transport(*, response_text: str = '{"intent":"GENERAL","confidence":0.8}') -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/api/tags"):
            return httpx.Response(
                200,
                json={"models": [{"name": "qwen2.5:3b"}]},
            )
        if path.endswith("/api/generate"):
            return httpx.Response(200, json={"response": response_text})
        if path.endswith("/api/chat"):
            return httpx.Response(
                200,
                json={"message": {"role": "assistant", "content": response_text}},
            )
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


def test_local_ollama_generate_and_extract_json() -> None:
    client = httpx.Client(
        transport=_ollama_transport(),
        base_url="http://ollama.test",
    )
    llm = LocalOllama(base_url="http://ollama.test", model="qwen2.5:3b", client=client)
    try:
        assert llm.ping() is True
        assert "qwen2.5:3b" in llm.list_models()
        raw = llm.generate("ciao", format_json=True)
        parsed = LocalOllama.extract_json_object(raw)
        assert parsed["intent"] == "GENERAL"
    finally:
        # Client iniettato: close del wrapper non lo chiude; chiudiamo noi.
        client.close()


def test_local_ollama_generate_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="http://ollama.test",
    )
    llm = LocalOllama(base_url="http://ollama.test", client=client)
    try:
        with pytest.raises(OllamaError):
            llm.generate("x")
    finally:
        client.close()
