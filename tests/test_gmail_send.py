"""Bozza Gmail, HITL sì/no e REST send. MockTransport, zero Google."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from io import StringIO
from pathlib import Path
from typing import Any

import httpx
import pytest
from google.oauth2.credentials import Credentials

from lavora_e_guida.agent import run_chat_loop
from lavora_e_guida.audio.mock import MockSTT, MockTTS
from lavora_e_guida.config import Settings
from lavora_e_guida.gmail import send as send_mod
from lavora_e_guida.gmail.agent import GMAIL_LOOP_SPEC, dispatch_gmail_tool
from lavora_e_guida.gmail.oauth import (
    GMAIL_SCOPES,
    MSG_GMAIL_NOT_LINKED,
    MSG_INSUFFICIENT_SCOPES,
    SCOPE_READONLY,
    GmailAuthError,
    get_gmail_credentials,
)
from lavora_e_guida.gmail.send import (
    GMAIL_SEND_URL,
    MSG_NEED_CONFIRM,
    MSG_NO_DRAFT,
    draft_email,
    get_draft_session,
    reset_draft_session,
    send_email,
)
from lavora_e_guida.llm.turn import FunctionCall, LlmTurn
from lavora_e_guida.llm.usage import TokenUsage

_USER = "tester@gmail.com"
_ACCESS = "send-access-token"


@pytest.fixture(autouse=True)
def _isolated_draft_session() -> Iterator[None]:
    """Ogni test parte senza bozza: HITL e send usano lo stato globale."""
    reset_draft_session()
    yield
    reset_draft_session()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        gmail_user=_USER,
        index_root=tmp_path / "index",
        gmail_token_file=tmp_path / "gmail_token.json",
    )


def _creds() -> Credentials:
    return Credentials(
        token=_ACCESS,
        refresh_token="refresh",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="id",
        client_secret="secret",
        scopes=list(GMAIL_SCOPES),
    )


def _patch_gmail_send_http(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[str]:
    """Sostituisce httpx.Client in gmail.send: zero DNS verso Google."""
    captured: list[str] = []
    real_client = httpx.Client

    def wrapped(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return handler(request)

    def factory(*args: Any, **kwargs: Any) -> httpx.Client:
        kwargs["transport"] = httpx.MockTransport(wrapped)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(send_mod.httpx, "Client", factory)
    monkeypatch.setattr(send_mod, "get_gmail_credentials", lambda **_k: _creds())
    return captured


def test_draft_email_stores_session_without_http() -> None:
    """Bozza OK: sessione piena, awaiting_confirm, nessuna REST."""
    result = draft_email("mario@x.test", "Fattura", "Pagare venerdì.")
    assert result.startswith("OK:")
    assert "mario@x.test" in result
    assert "Fattura" in result
    session = get_draft_session()
    assert session.draft is not None
    assert session.draft.to == "mario@x.test"
    assert session.awaiting_confirm is True
    assert session.confirmed is False


def test_draft_email_rejects_bad_to() -> None:
    """Destinatario senza @: errore parlante, sessione vuota."""
    result = draft_email("mario", "Oggetto", "Corpo")
    assert result.startswith("ERRORE:")
    assert get_draft_session().draft is None


def test_send_email_without_draft_is_spoken_error() -> None:
    """send senza bozza: niente HTTP."""
    assert send_email() == MSG_NO_DRAFT


def test_send_email_without_confirm_does_not_hit_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bozza sì, HITL no: send_email rifiuta e non apre httpx."""
    draft_email("mario@x.test", "Fattura", "Pagare.")

    def _boom(*_a: object, **_k: object) -> httpx.Client:
        raise AssertionError("httpx.Client non deve partire senza HITL")

    monkeypatch.setattr(send_mod.httpx, "Client", _boom)
    assert send_email() == MSG_NEED_CONFIRM
    assert get_draft_session().draft is not None


def test_send_email_happy_path_posts_raw(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dopo mark_confirmed: POST send, body raw, clear sessione."""
    draft_email("mario@x.test", "Fattura", "Pagare venerdì.")
    get_draft_session().mark_confirmed()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == GMAIL_SEND_URL
        assert request.headers.get("Authorization") == f"Bearer {_ACCESS}"
        payload = json.loads(request.content.decode("utf-8"))
        assert "raw" in payload
        assert payload["raw"]
        return httpx.Response(200, json={"id": "sent-1", "labelIds": ["SENT"]})

    captured = _patch_gmail_send_http(monkeypatch, handler)
    result = send_email(settings=_settings(tmp_path))
    assert result.startswith("OK:")
    assert "mario@x.test" in result
    assert "Fattura" in result
    assert get_draft_session().draft is None
    assert captured
    assert "gmail.googleapis.com" in captured[0]


def test_dispatch_draft_and_send_unknown() -> None:
    """Whitelist: send senza bozza è ERRORE; draft via dispatch popola la sessione."""
    assert dispatch_gmail_tool("send_email", {}) == MSG_NO_DRAFT
    out = dispatch_gmail_tool(
        "draft_email",
        {"to": "anna@x.test", "subject": "Ciao", "body": "Testo"},
    )
    assert out.startswith("OK:")
    assert get_draft_session().draft is not None


class _ScriptedLLM:
    """Coda di LlmTurn per il loop HITL (zero rete Gemini)."""

    def __init__(self, replies: list[LlmTurn]) -> None:
        self.last_usage = TokenUsage()
        self._replies = list(replies)
        self.calls = 0

    def chat(self, *_a: object, **_k: object) -> LlmTurn:
        self.calls += 1
        if not self._replies:
            raise AssertionError(f"chiamata LLM extra #{self.calls}")
        return self._replies.pop(0)

    def close(self) -> None:
        return None


def test_hitl_yes_sends_after_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turno 1: draft_email. Turno 2: «sì» senza Gemini → POST send."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "sent-hitl"})

    _patch_gmail_send_http(monkeypatch, handler)
    llm = _ScriptedLLM(
        [
            LlmTurn(
                function_calls=(
                    FunctionCall(
                        name="draft_email",
                        args={
                            "to": "mario@x.test",
                            "subject": "Fattura",
                            "body": "Pagare.",
                        },
                    ),
                ),
            ),
        ]
    )
    outfile = StringIO()
    code = run_chat_loop(
        MockSTT(
            infile=StringIO("scrivi a mario\nsì\nesci\n"),
            outfile=outfile,
            prompt="",
        ),
        MockTTS(outfile=outfile, prefix="[TTS] "),
        llm,
        report_latency=False,
        telemetry_db=tmp_path / "telemetry.db",
        spec=GMAIL_LOOP_SPEC,
    )
    assert code == 0
    spoken = outfile.getvalue()
    assert "mario@x.test" in spoken
    assert "sì per inviare" in spoken
    assert "email inviata a mario@x.test" in spoken
    # Il sì non deve chiedere un secondo round Gemini.
    assert llm.calls == 1
    assert get_draft_session().draft is None


def test_hitl_no_cancels_without_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """«no» dopo la bozza: niente Client httpx, sessione vuota."""

    def _boom(*_a: object, **_k: object) -> httpx.Client:
        raise AssertionError("annulla non deve chiamare Gmail")

    monkeypatch.setattr(send_mod.httpx, "Client", _boom)
    llm = _ScriptedLLM(
        [
            LlmTurn(
                function_calls=(
                    FunctionCall(
                        name="draft_email",
                        args={
                            "to": "mario@x.test",
                            "subject": "Fattura",
                            "body": "Pagare.",
                        },
                    ),
                ),
            ),
        ]
    )
    outfile = StringIO()
    code = run_chat_loop(
        MockSTT(
            infile=StringIO("scrivi a mario\nno\nesci\n"),
            outfile=outfile,
            prompt="",
        ),
        MockTTS(outfile=outfile, prefix="[TTS] "),
        llm,
        report_latency=False,
        telemetry_db=tmp_path / "telemetry.db",
        spec=GMAIL_LOOP_SPEC,
    )
    assert code == 0
    assert "Invio annullato." in outfile.getvalue()
    assert llm.calls == 1
    assert get_draft_session().draft is None


def test_readonly_token_blocks_send_credentials(tmp_path: Path) -> None:
    """Token solo readonly: get_gmail_credentials con GMAIL_SCOPES fallisce."""
    from lavora_e_guida.gmail.oauth import save_credentials

    creds = Credentials(
        token="ro",
        refresh_token="r",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="id",
        client_secret="secret",
        scopes=[SCOPE_READONLY],
    )
    save_credentials(creds, tmp_path / "gmail_token.json")
    with pytest.raises(GmailAuthError, match="insufficienti"):
        get_gmail_credentials(settings=_settings(tmp_path))
    assert MSG_INSUFFICIENT_SCOPES.startswith("ERRORE:")
    assert MSG_GMAIL_NOT_LINKED.startswith("ERRORE:")
