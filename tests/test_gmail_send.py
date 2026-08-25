"""Bozza Gmail, HITL sì/no e REST send. MockTransport, zero Google."""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Iterator
from email import message_from_string
from email.utils import getaddresses
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
from lavora_e_guida.gmail.agent import (
    GMAIL_LOOP_SPEC,
    GMAIL_TOOL_DECLARATIONS,
    dispatch_gmail_tool,
    gmail_hitl_after_tool,
    gmail_hitl_on_utterance,
)
from lavora_e_guida.gmail.oauth import (
    GMAIL_SCOPES,
    MSG_GMAIL_NOT_LINKED,
    MSG_INSUFFICIENT_SCOPES,
    SCOPE_READONLY,
    GmailAuthError,
    get_gmail_credentials,
)
from lavora_e_guida.gmail.read import (
    MSG_EMPTY_NAME,
    MSG_LIST_FIRST,
    MSG_NOT_IN_LIST,
    MailboxItem,
    MailboxSession,
    get_mailbox_session,
    reset_mailbox_session,
)
from lavora_e_guida.gmail.send import (
    GMAIL_SEND_URL,
    MSG_EMPTY_BODY,
    MSG_NEED_CONFIRM,
    MSG_NO_DRAFT,
    MSG_NO_LISTED_ADDRESS,
    MSG_NO_REPLY_ALL_RECIPIENTS,
    draft_email,
    get_draft_session,
    reply_all_email,
    reply_email,
    reset_draft_session,
    send_email,
)
from lavora_e_guida.llm.turn import FunctionCall, LlmTurn
from lavora_e_guida.llm.usage import TokenUsage

_USER = "tester@gmail.com"
_ACCESS = "send-access-token"
_ROSSI_THREAD = "thread-rossi"
_ROSSI_MESSAGE_ID = "<msg-rossi@x.test>"


def _decode_rfc822_raw(raw: str) -> str:
    """Decodifica il raw urlsafe-base64 senza padding che Gmail si aspetta."""
    padded = raw + "=" * ((4 - len(raw) % 4) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8")


@pytest.fixture(autouse=True)
def _isolated_draft_session() -> Iterator[None]:
    """Ogni test parte senza bozza né lista: HITL, send e fuzzy usano lo stato globale."""
    reset_draft_session()
    reset_mailbox_session()
    yield
    reset_draft_session()
    reset_mailbox_session()


def _rossi_mailbox(
    *,
    from_address: str = "mario.rossi@x.test",
    reply_to_address: str = "",
    extra: list[MailboxItem] | None = None,
    thread_id: str = _ROSSI_THREAD,
    rfc_message_id: str = _ROSSI_MESSAGE_ID,
    rfc_references: str = "",
    subject: str = "Fattura",
    to_addresses: tuple[str, ...] = (),
    cc_addresses: tuple[str, ...] = (),
) -> MailboxSession:
    """Lista vocale con Mario Rossi: From/Reply-To e thread in sessione."""
    box = MailboxSession()
    rossi = MailboxItem(
        gmail_id="aaa",
        sender="Mario Rossi",
        subject=subject,
        from_header="Mario Rossi <mario.rossi@x.test>",
        from_address=from_address,
        reply_to_address=reply_to_address,
        thread_id=thread_id,
        rfc_message_id=rfc_message_id,
        rfc_references=rfc_references,
        to_addresses=to_addresses,
        cc_addresses=cc_addresses,
    )
    items = [rossi]
    if extra:
        items.extend(extra)
    box.replace(items, query="inbox", gmail_q="in:inbox")
    return box


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
    # Il parlato HITL deve includere il corpo, non solo destinatario e oggetto.
    assert "Pagare venerdì." in result
    session = get_draft_session()
    assert session.draft is not None
    assert session.draft.to == "mario@x.test"
    assert session.awaiting_confirm is True
    assert session.confirmed is False
    # Compose nuovo: set_draft azzera il threading anche dopo una reply.
    assert session.draft.thread_id == ""
    assert session.draft.in_reply_to == ""
    assert session.draft.references == ""
    assert session.draft.cc == ""


def test_draft_email_rejects_spoken_to_without_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cognome senza lista in sessione: errore parlante, niente REST, bozza vuota."""

    def _boom(*_a: object, **_k: object) -> httpx.Client:
        raise AssertionError("senza lista non deve partire httpx")

    monkeypatch.setattr(send_mod.httpx, "Client", _boom)
    result = draft_email("Rossi", "Oggetto", "Corpo", mailbox=MailboxSession())
    assert result == MSG_LIST_FIRST
    assert get_draft_session().draft is None


def test_draft_email_resolves_surname_from_list() -> None:
    """«Rossi» dopo una lista con Mario Rossi: bozza sull'@, HITL lo contiene."""
    box = _rossi_mailbox()
    result = draft_email("Rossi", "Preventivo", "Arrivo mercoledì.", mailbox=box)
    assert result.startswith("OK:")
    assert "mario.rossi@x.test" in result
    assert "Preventivo" in result
    assert "Arrivo mercoledì." in result
    session = get_draft_session()
    assert session.draft is not None
    assert session.draft.to == "mario.rossi@x.test"
    assert session.awaiting_confirm is True


def test_draft_email_resolves_full_name_from_list() -> None:
    """«Mario Rossi» (nome+cognome) risolve lo stesso indirizzo del solo cognome."""
    box = _rossi_mailbox()
    result = draft_email("Mario Rossi", "Preventivo", "Arrivo mercoledì.", mailbox=box)
    assert result.startswith("OK:")
    assert "mario.rossi@x.test" in result
    session = get_draft_session()
    assert session.draft is not None
    assert session.draft.to == "mario.rossi@x.test"


def test_draft_email_valid_to_skips_fuzzy() -> None:
    """`to` già valido con @: niente RapidFuzz, anche se in lista c'è un altro mittente."""
    box = _rossi_mailbox()
    result = draft_email("anna@x.test", "Ciao", "Testo.", mailbox=box)
    assert result.startswith("OK:")
    assert "anna@x.test" in result
    assert "mario.rossi@x.test" not in result
    session = get_draft_session()
    assert session.draft is not None
    assert session.draft.to == "anna@x.test"


def test_draft_email_prefers_reply_to_over_from() -> None:
    """Destinatario = Reply-To se parsabile, altrimenti From."""
    box = _rossi_mailbox(reply_to_address="segreteria@x.test")
    result = draft_email("Rossi", "Preventivo", "Arrivo mercoledì.", mailbox=box)
    assert result.startswith("OK:")
    assert "segreteria@x.test" in result
    assert "mario.rossi@x.test" not in result
    session = get_draft_session()
    assert session.draft is not None
    assert session.draft.to == "segreteria@x.test"


def test_draft_email_row_without_address() -> None:
    """Riga in lista senza From/Reply-To parsabile: errore parlante, niente bozza."""
    box = _rossi_mailbox(from_address="", reply_to_address="")
    result = draft_email("Rossi", "Preventivo", "Arrivo mercoledì.", mailbox=box)
    assert result == MSG_NO_LISTED_ADDRESS
    assert get_draft_session().draft is None


def test_draft_email_unknown_name_in_list() -> None:
    """Cognome assente dalla lista: stesso RapidFuzz di read_email, niente bozza."""
    box = _rossi_mailbox()
    result = draft_email("Verdi", "Preventivo", "Arrivo mercoledì.", mailbox=box)
    assert result == MSG_NOT_IN_LIST
    assert get_draft_session().draft is None


def test_reply_email_without_list_is_spoken_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Senza lista: errore parlante, niente REST, bozza vuota."""

    def _boom(*_a: object, **_k: object) -> httpx.Client:
        raise AssertionError("senza lista non deve partire httpx")

    monkeypatch.setattr(send_mod.httpx, "Client", _boom)
    result = reply_email("Rossi", "ok", mailbox=MailboxSession())
    assert result == MSG_LIST_FIRST
    assert get_draft_session().draft is None


def test_reply_email_resolves_surname_and_threads() -> None:
    """«Rossi» → From, oggetto Re:, threadId e Message-ID in bozza; HITL ha l'@."""
    box = _rossi_mailbox()
    result = reply_email("Rossi", "ok, parto.", mailbox=box)
    assert result.startswith("OK:")
    assert "mario.rossi@x.test" in result
    assert "Re: Fattura" in result
    assert "ok, parto." in result
    session = get_draft_session()
    assert session.draft is not None
    assert session.draft.to == "mario.rossi@x.test"
    assert session.draft.subject == "Re: Fattura"
    assert session.draft.body == "ok, parto."
    assert session.draft.thread_id == _ROSSI_THREAD
    assert session.draft.in_reply_to == _ROSSI_MESSAGE_ID
    assert session.draft.references == _ROSSI_MESSAGE_ID
    assert session.awaiting_confirm is True


def test_reply_email_keeps_existing_re_prefix() -> None:
    """Oggetto già Re: non viene duplicato."""
    box = _rossi_mailbox(subject="Re: Fattura")
    result = reply_email("1", "ok", mailbox=box)
    assert result.startswith("OK:")
    session = get_draft_session()
    assert session.draft is not None
    assert session.draft.subject == "Re: Fattura"


def test_reply_email_prefers_reply_to_and_concatenates_references() -> None:
    """Destinatario = Reply-To; References = catena precedente + Message-ID."""
    box = _rossi_mailbox(
        reply_to_address="segreteria@x.test",
        rfc_references="<prev@x.test>",
    )
    result = reply_email("Mario Rossi", "ok", mailbox=box)
    assert result.startswith("OK:")
    assert "segreteria@x.test" in result
    assert "mario.rossi@x.test" not in result
    session = get_draft_session()
    assert session.draft is not None
    assert session.draft.to == "segreteria@x.test"
    assert session.draft.references == f"<prev@x.test> {_ROSSI_MESSAGE_ID}"


def test_reply_email_row_without_address() -> None:
    """Riga in lista senza From/Reply-To: errore parlante, niente bozza."""
    box = _rossi_mailbox(from_address="", reply_to_address="")
    result = reply_email("Rossi", "ok", mailbox=box)
    assert result == MSG_NO_LISTED_ADDRESS
    assert get_draft_session().draft is None


def test_reply_email_empty_name_or_body() -> None:
    """name/body vuoti: errori parlanti prima del resolve."""
    box = _rossi_mailbox()
    assert reply_email("", "ok", mailbox=box) == MSG_EMPTY_NAME
    assert reply_email("Rossi", "", mailbox=box) == MSG_EMPTY_BODY
    assert get_draft_session().draft is None


def test_reply_email_unknown_name_in_list() -> None:
    """Cognome assente: stesso RapidFuzz di read_email, niente bozza."""
    box = _rossi_mailbox()
    result = reply_email("Verdi", "ok", mailbox=box)
    assert result == MSG_NOT_IN_LIST
    assert get_draft_session().draft is None


def test_reply_email_ambiguous_surname_uses_rapidfuzz() -> None:
    """Due Rossi: RapidFuzz (soglia 70) ne sceglie uno; HITL dice quell'@."""
    anna = MailboxItem(
        gmail_id="bbb",
        sender="Anna Rossi",
        subject="Riunione",
        from_header="Anna Rossi <anna.rossi@x.test>",
        from_address="anna.rossi@x.test",
        thread_id="thread-anna",
        rfc_message_id="<msg-anna@x.test>",
    )
    box = _rossi_mailbox(extra=[anna])
    result = reply_email("Rossi", "ok", mailbox=box)
    assert result.startswith("OK:")
    session = get_draft_session()
    assert session.draft is not None
    chosen = session.draft.to
    assert chosen in {"mario.rossi@x.test", "anna.rossi@x.test"}
    assert chosen in result
    other = (
        "anna.rossi@x.test"
        if chosen == "mario.rossi@x.test"
        else "mario.rossi@x.test"
    )
    assert other not in result


def test_draft_after_reply_zeros_threading() -> None:
    """Un compose nuovo dopo una reply non deve ereditare threadId."""
    box = _rossi_mailbox()
    reply_email("Rossi", "ok", mailbox=box)
    assert get_draft_session().draft is not None
    assert get_draft_session().draft.thread_id == _ROSSI_THREAD
    draft_email("anna@x.test", "Ciao", "Testo.")
    session = get_draft_session()
    assert session.draft is not None
    assert session.draft.to == "anna@x.test"
    assert session.draft.thread_id == ""
    assert session.draft.in_reply_to == ""
    assert session.draft.references == ""


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
        assert "threadId" not in payload
        return httpx.Response(200, json={"id": "sent-1", "labelIds": ["SENT"]})

    captured = _patch_gmail_send_http(monkeypatch, handler)
    result = send_email(settings=_settings(tmp_path))
    assert result.startswith("OK:")
    assert "mario@x.test" in result
    assert "Fattura" in result
    assert get_draft_session().draft is None
    assert captured
    assert "gmail.googleapis.com" in captured[0]


def test_send_reply_posts_thread_id_and_in_reply_to(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reply confermata: POST con threadId e raw che contiene In-Reply-To."""
    box = _rossi_mailbox()
    reply_email("Rossi", "ok", mailbox=box)
    get_draft_session().mark_confirmed()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["threadId"] == _ROSSI_THREAD
        decoded = _decode_rfc822_raw(payload["raw"])
        assert "In-Reply-To:" in decoded
        assert _ROSSI_MESSAGE_ID in decoded
        assert "References:" in decoded
        return httpx.Response(200, json={"id": "sent-reply"})

    _patch_gmail_send_http(monkeypatch, handler)
    result = send_email(settings=_settings(tmp_path))
    assert result.startswith("OK:")
    assert "mario.rossi@x.test" in result
    assert "Re: Fattura" in result
    assert get_draft_session().draft is None


def test_send_reply_all_posts_to_cc_without_gmail_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """reply_all confermata: MIME To e Cc, threadId, In-Reply-To, GMAIL_USER assente."""
    box = _rossi_mailbox(
        to_addresses=(_USER, "anna@x.test"),
        cc_addresses=("segreteria@x.test",),
    )
    reply_all_email("Rossi", "ok", mailbox=box, settings=_settings(tmp_path))
    get_draft_session().mark_confirmed()

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["threadId"] == _ROSSI_THREAD
        decoded = _decode_rfc822_raw(payload["raw"])
        msg = message_from_string(decoded)
        to_addrs = [addr for _name, addr in getaddresses([msg["To"] or ""])]
        cc_addrs = [addr for _name, addr in getaddresses([msg["Cc"] or ""])]
        assert "mario.rossi@x.test" in to_addrs
        assert "anna@x.test" in to_addrs
        assert "segreteria@x.test" in cc_addrs
        assert _USER not in to_addrs
        assert _USER not in cc_addrs
        assert "In-Reply-To:" in decoded
        assert _ROSSI_MESSAGE_ID in decoded
        return httpx.Response(200, json={"id": "sent-reply-all"})

    _patch_gmail_send_http(monkeypatch, handler)
    result = send_email(settings=_settings(tmp_path))
    assert result.startswith("OK:")
    assert get_draft_session().draft is None


@pytest.mark.parametrize(
    ("status", "phrase"),
    [
        (400, "Ha rifiutato l'invio, controlla i destinatari e riprova"),
        (422, "Ha rifiutato l'invio, controlla i destinatari e riprova"),
        (429, "Gmail è occupata, riprova tra poco"),
        (500, "Gmail non raggiungibile, riprova più tardi"),
        (503, "Gmail non raggiungibile, riprova più tardi"),
        (409, "Invio non riuscito, riprova più tardi"),
    ],
)
def test_send_email_http_error_speaks_code_and_italian_phrase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    phrase: str,
) -> None:
    """POST >= 400 (non 401/403): Gmail HTTP {code} più frase; bozza resta."""
    draft_email("mario@x.test", "Fattura", "Pagare.")
    get_draft_session().mark_confirmed()
    # JSON error Gmail: deve restare fuori dal TTS, si parla solo codice+frase.
    gmail_json = {"error": {"code": status, "message": "Invalid to header"}}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=gmail_json)

    _patch_gmail_send_http(monkeypatch, handler)
    result = send_email(settings=_settings(tmp_path))
    assert result == f"ERRORE: Gmail HTTP {status}. {phrase}"
    assert "Invalid to header" not in result
    session = get_draft_session()
    assert session.draft is not None
    assert session.draft.to == "mario@x.test"
    assert session.awaiting_confirm is True
    assert session.confirmed is False


@pytest.mark.parametrize("status", [401, 403])
def test_send_email_http_auth_error_stays_not_linked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    """401/403 sull'invio: Gmail non collegata, niente Gmail HTTP {code}."""
    draft_email("mario@x.test", "Fattura", "Pagare.")
    get_draft_session().mark_confirmed()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": status}})

    _patch_gmail_send_http(monkeypatch, handler)
    result = send_email(settings=_settings(tmp_path))
    assert result == MSG_GMAIL_NOT_LINKED
    assert f"Gmail HTTP {status}" not in result
    assert get_draft_session().draft is not None


@pytest.mark.parametrize(
    ("status", "phrase"),
    [
        (400, "Ha rifiutato l'invio, controlla i destinatari e riprova"),
        (500, "Gmail non raggiungibile, riprova più tardi"),
    ],
)
def test_hitl_yes_http_error_speaks_code_and_keeps_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    phrase: str,
) -> None:
    """Sì HITL su POST 400/500: parla Gmail HTTP {code} e la frase; bozza resta."""
    draft_email("mario@x.test", "Fattura", "Pagare.")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={"error": {"code": status, "message": "Invalid to header"}},
        )

    _patch_gmail_send_http(monkeypatch, handler)
    spoken = gmail_hitl_on_utterance("sì")
    assert spoken == f"ERRORE: Gmail HTTP {status}. {phrase}"
    assert "Invalid to header" not in (spoken or "")
    session = get_draft_session()
    assert session.draft is not None
    assert session.draft.to == "mario@x.test"
    assert session.awaiting_confirm is True
    assert session.confirmed is False


def test_dispatch_draft_and_send_unknown() -> None:
    """Whitelist: send senza bozza è ERRORE; draft via dispatch popola la sessione."""
    assert dispatch_gmail_tool("send_email", {}) == MSG_NO_DRAFT
    out = dispatch_gmail_tool(
        "draft_email",
        {"to": "anna@x.test", "subject": "Ciao", "body": "Testo"},
    )
    assert out.startswith("OK:")
    assert get_draft_session().draft is not None


def test_dispatch_draft_resolves_surname_from_global_list() -> None:
    """Percorso runtime: Gemini passa to=Rossi, dispatch usa la mailbox globale."""
    listed = _rossi_mailbox()
    get_mailbox_session().replace(
        listed.items,
        query=listed.last_query,
        gmail_q=listed.last_gmail_q,
    )
    out = dispatch_gmail_tool(
        "draft_email",
        {"to": "Rossi", "subject": "Ciao", "body": "Testo"},
    )
    assert out.startswith("OK:")
    assert "mario.rossi@x.test" in out
    session = get_draft_session()
    assert session.draft is not None
    assert session.draft.to == "mario.rossi@x.test"


def test_dispatch_reply_email_from_global_list() -> None:
    """Gemini passa name=Rossi e body: dispatch risolve e apre HITL."""
    listed = _rossi_mailbox()
    get_mailbox_session().replace(
        listed.items,
        query=listed.last_query,
        gmail_q=listed.last_gmail_q,
    )
    out = dispatch_gmail_tool("reply_email", {"name": "Rossi", "body": "ok"})
    assert out.startswith("OK:")
    assert "mario.rossi@x.test" in out
    session = get_draft_session()
    assert session.draft is not None
    assert session.draft.to == "mario.rossi@x.test"
    assert session.draft.subject == "Re: Fattura"
    assert session.draft.thread_id == _ROSSI_THREAD


def test_dispatch_reply_email_integer_name() -> None:
    """Indice Gemini integer (1) diventa la prima riga, come read_email."""
    listed = _rossi_mailbox()
    get_mailbox_session().replace(
        listed.items,
        query=listed.last_query,
        gmail_q=listed.last_gmail_q,
    )
    out = dispatch_gmail_tool("reply_email", {"name": 1, "body": "ok"})
    assert out.startswith("OK:")
    session = get_draft_session()
    assert session.draft is not None
    assert session.draft.to == "mario.rossi@x.test"


def test_dispatch_reply_email_empty_name() -> None:
    """name assente: stesso errore parlante di read_email."""
    assert dispatch_gmail_tool("reply_email", {"body": "ok"}) == MSG_EMPTY_NAME
    assert get_draft_session().draft is None


def test_gmail_prompt_and_catalog_instruct_spoken_recipients() -> None:
    """Gemini non inventa l'@ : prompt, declaration e esempi Rossi, niente JSON-in-testo."""
    prompt = GMAIL_LOOP_SPEC.system_prompt
    # Contratto vocale: Python riempie From/Reply-To; Gemini passa Rossi, non un dominio.
    assert "lo riempie Python" in prompt
    assert "From o Reply-To" in prompt
    assert "Vietato inventare @ o domini" in prompt
    assert "non spellingare l'indirizzo" in prompt
    # Esempi del piano: cognome in query, in to, in name; indice sulla reply.
    assert "ultime di Rossi" in prompt
    assert "from:rossi" in prompt
    assert "draft_email to Rossi" in prompt
    assert "reply_email name Rossi" in prompt
    assert "reply_email name 1" in prompt
    assert "rispondi a tutti" in prompt
    assert "reply_all_email name Rossi" in prompt
    # Esempi del piano: Rossi → reply; «rispondi a tutti» → reply_all (name 1 o Rossi).
    assert "rispondi a Rossi che ok" in prompt
    assert "reply_email name Rossi body ok" in prompt
    assert "reply_all_email name 1" in prompt
    assert "reply_all_email name Rossi body ok" in prompt
    # Niente People API nel prompt: gli @ escono dalla riga in sessione.
    assert "people" not in prompt.casefold()
    # Gli schemi stanno nelle functionDeclarations, non come {"tool":...} nel system.
    assert '{"tool": "draft_email"' not in prompt
    assert '{"tool": "reply_email"' not in prompt
    assert '{"tool": "reply_all_email"' not in prompt

    by_name = {item.name: item for item in GMAIL_TOOL_DECLARATIONS}
    # Niente People API né tool extra: catalogo send + reply_email + reply_all_email.
    assert set(by_name) == {
        "list_emails",
        "read_email",
        "save_attachments",
        "draft_email",
        "reply_email",
        "reply_all_email",
        "send_email",
    }
    draft = by_name["draft_email"]
    reply = by_name["reply_email"]
    assert "Non inventare @ o domini" in draft.description
    assert "lo riempie Python" in draft.description
    assert "Non spellingare l'indirizzo" in draft.description
    to_desc = draft.parameters["properties"]["to"]["description"]
    assert "Rossi" in to_desc
    assert "Mario Rossi" in to_desc
    assert "Non inventare un @" in to_desc
    # Niente placeholder <nome> / {testo} nelle declaration (contratto Gemini).
    assert "<" not in to_desc
    assert "{" not in to_desc

    # Reply: solo name+body; to/subject/threadId li deriva Python.
    assert "name" in reply.parameters["properties"]
    assert "body" in reply.parameters["properties"]
    assert "to" not in reply.parameters["properties"]
    assert "subject" not in reply.parameters["properties"]
    assert "riempie Python" in reply.description
    assert "Non spellingare l'indirizzo" in reply.description
    name_desc = reply.parameters["properties"]["name"]["description"]
    assert "Rossi" in name_desc
    assert "Mario Rossi" in name_desc
    assert "<" not in name_desc
    assert "{" not in name_desc

    reply_all = by_name["reply_all_email"]
    assert "name" in reply_all.parameters["properties"]
    assert "body" in reply_all.parameters["properties"]
    assert "to" not in reply_all.parameters["properties"]
    assert "all" not in reply_all.parameters["properties"]
    assert "people" not in reply_all.description.casefold()
    all_name = reply_all.parameters["properties"]["name"]["description"]
    assert "Rossi" in all_name
    assert "<" not in all_name
    assert "{" not in all_name

    assert GMAIL_LOOP_SPEC.gemini_tools is not None
    decls = GMAIL_LOOP_SPEC.gemini_tools[0]["functionDeclarations"]
    decl_names = [item["name"] for item in decls]
    assert decl_names == [
        "list_emails",
        "read_email",
        "save_attachments",
        "draft_email",
        "reply_email",
        "reply_all_email",
        "send_email",
    ]


def test_gmail_hitl_after_tool_includes_reply_all() -> None:
    """Stesso gate HITL di draft/reply: reply_all_email OK parla e salta Gemini."""
    spoken = (
        "ho preparato un'email a mario.rossi@x.test, oggetto Re: Fattura. "
        "Il testo è: ok. Di' sì per inviare o no per annullare."
    )
    assert gmail_hitl_after_tool("reply_all_email", "OK: " + spoken) == spoken
    assert gmail_hitl_after_tool("reply_email", "OK: " + spoken) == spoken
    assert gmail_hitl_after_tool("draft_email", "OK: " + spoken) == spoken
    assert gmail_hitl_after_tool("send_email", "OK: " + spoken) is None
    assert gmail_hitl_after_tool("reply_all_email", "ERRORE: nessuno") is None


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
    # Stesso corpo della bozza: l'utente sente il testo prima di confermare.
    assert "Pagare." in spoken
    assert "email inviata a mario@x.test" in spoken
    # Il sì non deve chiedere un secondo round Gemini.
    assert llm.calls == 1
    assert get_draft_session().draft is None


def test_hitl_yes_after_draft_to_rossi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turno 1: Gemini to=Rossi (non un @). Turno 2: sì → POST compose nuovo."""
    # Lista in sessione come dopo list_emails: il dispatch globale fa il fuzzy.
    listed = _rossi_mailbox()
    get_mailbox_session().replace(
        listed.items,
        query=listed.last_query,
        gmail_q=listed.last_gmail_q,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        # Compose nuovo: niente threadId né In-Reply-To (quelli sono di reply_email).
        assert "threadId" not in payload
        decoded = _decode_rfc822_raw(payload["raw"])
        assert "mario.rossi@x.test" in decoded
        assert "In-Reply-To:" not in decoded
        return httpx.Response(200, json={"id": "sent-draft-rossi"})

    _patch_gmail_send_http(monkeypatch, handler)
    llm = _ScriptedLLM(
        [
            LlmTurn(
                function_calls=(
                    FunctionCall(
                        name="draft_email",
                        args={
                            "to": "Rossi",
                            "subject": "Preventivo",
                            "body": "Arrivo mercoledì.",
                        },
                    ),
                ),
            ),
        ]
    )
    outfile = StringIO()
    code = run_chat_loop(
        MockSTT(
            infile=StringIO("scrivi a rossi\nsì\nesci\n"),
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
    # L'@ lo dice l'HITL Python, non Gemini: conferma e esito di invio.
    assert "mario.rossi@x.test" in spoken
    assert "sì per inviare" in spoken
    assert "Arrivo mercoledì." in spoken
    assert "email inviata a mario.rossi@x.test" in spoken
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


def test_hitl_yes_after_reply_posts_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turno 1: reply_email. Turno 2: «sì» senza Gemini → POST con threadId."""
    listed = _rossi_mailbox()
    get_mailbox_session().replace(
        listed.items,
        query=listed.last_query,
        gmail_q=listed.last_gmail_q,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["threadId"] == _ROSSI_THREAD
        decoded = _decode_rfc822_raw(payload["raw"])
        assert "In-Reply-To:" in decoded
        assert _ROSSI_MESSAGE_ID in decoded
        return httpx.Response(200, json={"id": "sent-reply-hitl"})

    _patch_gmail_send_http(monkeypatch, handler)
    llm = _ScriptedLLM(
        [
            LlmTurn(
                function_calls=(
                    FunctionCall(
                        name="reply_email",
                        args={"name": "Rossi", "body": "ok"},
                    ),
                ),
            ),
        ]
    )
    outfile = StringIO()
    code = run_chat_loop(
        MockSTT(
            infile=StringIO("rispondi a rossi\nsì\nesci\n"),
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
    assert "mario.rossi@x.test" in spoken
    assert "sì per inviare" in spoken
    assert "ok" in spoken
    assert "email inviata a mario.rossi@x.test" in spoken
    assert llm.calls == 1
    assert get_draft_session().draft is None



def test_reply_all_email_fills_to_cc_without_gmail_user(tmp_path: Path) -> None:
    """reply_all: To = mittente + To originali senza me; Cc = gli altri; HITL ordinale."""
    box = _rossi_mailbox(
        to_addresses=(_USER, "anna@x.test"),
        cc_addresses=("segreteria@x.test",),
    )
    result = reply_all_email("Rossi", "ok", mailbox=box, settings=_settings(tmp_path))
    assert result.startswith("OK:")
    assert "mario.rossi@x.test, a anna@x.test e a segreteria@x.test" in result
    assert "A:" not in result
    assert "Cc:" not in result
    assert _USER not in result
    session = get_draft_session()
    assert session.draft is not None
    assert session.draft.to == "mario.rossi@x.test, anna@x.test"
    assert session.draft.cc == "segreteria@x.test"
    assert session.draft.subject == "Re: Fattura"
    assert session.draft.thread_id == _ROSSI_THREAD
    assert session.draft.in_reply_to == _ROSSI_MESSAGE_ID
    assert session.awaiting_confirm is True


def test_reply_all_email_only_me_in_to_others_in_cc(tmp_path: Path) -> None:
    """Solo io in To e altri in Cc: To = mittente, Cc = gli altri."""
    box = _rossi_mailbox(
        to_addresses=(_USER,),
        cc_addresses=("anna@x.test", "segreteria@x.test"),
    )
    result = reply_all_email("Rossi", "ok", mailbox=box, settings=_settings(tmp_path))
    assert result.startswith("OK:")
    session = get_draft_session()
    assert session.draft is not None
    assert session.draft.to == "mario.rossi@x.test"
    assert session.draft.cc == "anna@x.test, segreteria@x.test"
    assert _USER.casefold() not in session.draft.to.casefold()
    assert _USER.casefold() not in session.draft.cc.casefold()
    assert "mario.rossi@x.test, a anna@x.test e a segreteria@x.test" in result


def test_reply_all_email_only_me_in_to_prefers_reply_to(tmp_path: Path) -> None:
    """Solo io in To: mittente = Reply-To se valido, Cc = gli altri, senza me."""
    box = _rossi_mailbox(
        reply_to_address="segreteria-reply@x.test",
        to_addresses=(_USER,),
        cc_addresses=("anna@x.test",),
    )
    result = reply_all_email("Rossi", "ok", mailbox=box, settings=_settings(tmp_path))
    assert result.startswith("OK:")
    session = get_draft_session()
    assert session.draft is not None
    assert session.draft.to == "segreteria-reply@x.test"
    assert session.draft.cc == "anna@x.test"
    assert "mario.rossi@x.test" not in session.draft.to
    assert _USER.casefold() not in session.draft.to.casefold()
    assert _USER.casefold() not in session.draft.cc.casefold()


def test_reply_all_email_excludes_gmail_user_casefold(tmp_path: Path) -> None:
    """GMAIL_USER in To/Cc con casing diverso: comunque escluso da entrambi."""
    box = _rossi_mailbox(
        to_addresses=("Tester@Gmail.com", "anna@x.test"),
        cc_addresses=("TESTER@gmail.com", "segreteria@x.test"),
    )
    result = reply_all_email("Rossi", "ok", mailbox=box, settings=_settings(tmp_path))
    assert result.startswith("OK:")
    session = get_draft_session()
    assert session.draft is not None
    assert session.draft.to == "mario.rossi@x.test, anna@x.test"
    assert session.draft.cc == "segreteria@x.test"
    assert "tester@gmail.com" not in session.draft.to.casefold()
    assert "tester@gmail.com" not in session.draft.cc.casefold()


def test_reply_all_email_empty_to_after_filter_is_spoken_error(tmp_path: Path) -> None:
    """Ero l'unico in To e From non parsabile: errore parlante, niente bozza."""
    box = _rossi_mailbox(
        from_address="",
        reply_to_address="",
        to_addresses=(_USER,),
        cc_addresses=("anna@x.test",),
    )
    result = reply_all_email("Rossi", "ok", mailbox=box, settings=_settings(tmp_path))
    assert result == MSG_NO_REPLY_ALL_RECIPIENTS
    assert get_draft_session().draft is None


def test_reply_all_email_degenerate_single_recipient(tmp_path: Path) -> None:
    """Un solo @ restante è comunque reply-all degenere: si prepara, HITL dice uno."""
    box = _rossi_mailbox()
    result = reply_all_email("Rossi", "ok", mailbox=box, settings=_settings(tmp_path))
    assert result.startswith("OK:")
    assert "mario.rossi@x.test" in result
    assert " e a " not in result
    session = get_draft_session()
    assert session.draft is not None
    assert session.draft.to == "mario.rossi@x.test"
    assert session.draft.cc == ""


def test_reply_email_does_not_copy_to_cc(tmp_path: Path) -> None:
    """reply_email resta solo mittente anche se la riga ha To/Cc."""
    box = _rossi_mailbox(
        to_addresses=(_USER, "anna@x.test"),
        cc_addresses=("segreteria@x.test",),
    )
    result = reply_email("Rossi", "ok", mailbox=box)
    assert result.startswith("OK:")
    assert "anna@x.test" not in result
    assert "segreteria@x.test" not in result
    session = get_draft_session()
    assert session.draft is not None
    assert session.draft.to == "mario.rossi@x.test"
    assert session.draft.cc == ""


def test_dispatch_reply_all_email_from_global_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gemini passa name=Rossi: dispatch usa mailbox globale e GMAIL_USER da settings."""
    monkeypatch.setattr(send_mod, "get_settings", lambda: _settings(tmp_path))
    listed = _rossi_mailbox(
        to_addresses=(_USER, "anna@x.test"),
        cc_addresses=("segreteria@x.test",),
    )
    get_mailbox_session().replace(
        listed.items,
        query=listed.last_query,
        gmail_q=listed.last_gmail_q,
    )
    out = dispatch_gmail_tool("reply_all_email", {"name": "Rossi", "body": "ok"})
    assert out.startswith("OK:")
    session = get_draft_session()
    assert session.draft is not None
    assert session.draft.to == "mario.rossi@x.test, anna@x.test"
    assert session.draft.cc == "segreteria@x.test"


def test_dispatch_reply_all_email_integer_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Indice integer 1: stessa riga di reply_email."""
    monkeypatch.setattr(send_mod, "get_settings", lambda: _settings(tmp_path))
    listed = _rossi_mailbox(to_addresses=("anna@x.test",), cc_addresses=())
    get_mailbox_session().replace(
        listed.items,
        query=listed.last_query,
        gmail_q=listed.last_gmail_q,
    )
    out = dispatch_gmail_tool("reply_all_email", {"name": 1, "body": "ok"})
    assert out.startswith("OK:")
    session = get_draft_session()
    assert session.draft is not None
    assert "mario.rossi@x.test" in session.draft.to


def test_dispatch_reply_all_email_empty_name() -> None:
    """name assente: stesso errore parlante di reply_email."""
    assert dispatch_gmail_tool("reply_all_email", {"body": "ok"}) == MSG_EMPTY_NAME
    assert get_draft_session().draft is None


def test_hitl_yes_after_reply_all_posts_to_cc_and_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Turno 1: reply_all_email. Turno 2: sì → MIME To e Cc, threadId, senza GMAIL_USER."""
    monkeypatch.setattr(send_mod, "get_settings", lambda: _settings(tmp_path))
    listed = _rossi_mailbox(
        to_addresses=(_USER, "anna@x.test"),
        cc_addresses=("segreteria@x.test",),
    )
    get_mailbox_session().replace(
        listed.items,
        query=listed.last_query,
        gmail_q=listed.last_gmail_q,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["threadId"] == _ROSSI_THREAD
        decoded = _decode_rfc822_raw(payload["raw"])
        msg = message_from_string(decoded)
        to_addrs = [addr for _name, addr in getaddresses([msg["To"] or ""])]
        cc_addrs = [addr for _name, addr in getaddresses([msg["Cc"] or ""])]
        assert "mario.rossi@x.test" in to_addrs
        assert "anna@x.test" in to_addrs
        assert "segreteria@x.test" in cc_addrs
        assert _USER not in to_addrs
        assert _USER not in cc_addrs
        assert "In-Reply-To:" in decoded
        assert _ROSSI_MESSAGE_ID in decoded
        return httpx.Response(200, json={"id": "sent-reply-all-hitl"})

    _patch_gmail_send_http(monkeypatch, handler)
    llm = _ScriptedLLM(
        [
            LlmTurn(
                function_calls=(
                    FunctionCall(
                        name="reply_all_email",
                        args={"name": "Rossi", "body": "ok"},
                    ),
                ),
            ),
        ]
    )
    outfile = StringIO()
    code = run_chat_loop(
        MockSTT(
            infile=StringIO("rispondi a tutti a rossi\nsì\nesci\n"),
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
    assert "mario.rossi@x.test" in spoken
    assert "anna@x.test" in spoken
    assert "segreteria@x.test" in spoken
    assert "sì per inviare" in spoken
    assert "ok" in spoken
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
