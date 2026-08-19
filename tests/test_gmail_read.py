"""Tool Gmail readonly: MockTransport httpx, zero hit a Google."""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from google.oauth2.credentials import Credentials

from lavora_e_guida.agent import run_chat_loop
from lavora_e_guida.audio.mock import MockSTT, MockTTS
from lavora_e_guida.config import Settings
from lavora_e_guida.gmail import read as read_mod
from lavora_e_guida.gmail.agent import GMAIL_LOOP_SPEC, dispatch_gmail_tool
from lavora_e_guida.gmail.oauth import GMAIL_SCOPES, MSG_GMAIL_NOT_LINKED
from lavora_e_guida.gmail.read import (
    COUNT_CAP,
    COUNT_PAGE_SIZE,
    GMAIL_MESSAGES_URL,
    MAX_BODY_CHARS,
    MSG_EMPTY_LIST,
    MSG_LIST_FIRST,
    MailboxItem,
    MailboxSession,
    clamp_list_limit,
    extract_message_text,
    format_count_result,
    get_mailbox_session,
    list_emails,
    normalize_gmail_query,
    read_email,
    reset_mailbox_session,
    strip_html_to_text,
    truncate_tts_body,
)
from lavora_e_guida.llm.usage import TokenUsage

_USER = "tester@gmail.com"
_CLIENT_ID = "desktop-id.apps.googleusercontent.com"
_CLIENT_SECRET = "desktop-secret"
_ACCESS = "test-access-token"


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    """Settings isolate: token sotto tmp_path, niente .env del developer."""
    token = tmp_path / "gmail_token.json"
    values: dict[str, Any] = {
        "gmail_client_id": _CLIENT_ID,
        "gmail_client_secret": _CLIENT_SECRET,
        "gmail_user": _USER,
        "gmail_token_file": token,
        "index_root": tmp_path / "index",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _creds() -> Credentials:
    """Access token finto già valido: i test non devono rinfrescare Google."""
    return Credentials(
        token=_ACCESS,
        refresh_token="refresh-token",
        token_uri="https://oauth2.googleapis.com/token",
        client_id=_CLIENT_ID,
        client_secret=_CLIENT_SECRET,
        scopes=list(GMAIL_SCOPES),
        expiry=datetime.now(UTC) + timedelta(hours=1),
    )


def _b64url(text: str) -> str:
    """Stesso encoding dei body Gmail (urlsafe, padding spesso assente)."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _headers(sender: str, subject: str) -> list[dict[str, str]]:
    return [
        {"name": "From", "value": sender},
        {"name": "Subject", "value": subject},
    ]


def _meta(msg_id: str, sender: str, subject: str) -> dict[str, Any]:
    return {"id": msg_id, "payload": {"headers": _headers(sender, subject)}}


def _full(
    msg_id: str,
    sender: str,
    subject: str,
    *,
    plain: str | None = None,
    html: str | None = None,
    snippet: str | None = None,
) -> dict[str, Any]:
    """Resource format=full: multipart/alternative se ci sono entrambi i body."""
    parts: list[dict[str, Any]] = []
    if plain is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": _b64url(plain)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": _b64url(html)}})
    payload: dict[str, Any] = {"headers": _headers(sender, subject)}
    if len(parts) == 1:
        payload["mimeType"] = parts[0]["mimeType"]
        payload["body"] = parts[0]["body"]
    elif parts:
        payload["mimeType"] = "multipart/alternative"
        payload["parts"] = parts
    message: dict[str, Any] = {"id": msg_id, "payload": payload}
    if snippet is not None:
        message["snippet"] = snippet
    return message


def _query_of(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.url.query.decode("ascii") if isinstance(request.url.query, bytes) else str(request.url.query))


@pytest.fixture(autouse=True)
def _isolated_mailbox_session() -> Iterator[None]:
    """Ogni test parte da sessione vuota: il loop vocale usa lo stato globale."""
    reset_mailbox_session()
    yield
    reset_mailbox_session()


def _patch_gmail_http(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> list[str]:
    """Sostituisce httpx.Client in gmail.read con MockTransport + token finto.

    Dispatch e loop vocale non iniettano il client: senza questo patch
    list_emails aprirebbe una socket verso Google. Ritorna gli URL visti.
    """
    captured: list[str] = []
    real_client = httpx.Client

    def wrapped(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        return handler(request)

    def factory(*args: Any, **kwargs: Any) -> httpx.Client:
        # Transport mock vince su qualsiasi default: zero DNS/TLS verso Google.
        kwargs["transport"] = httpx.MockTransport(wrapped)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(read_mod.httpx, "Client", factory)
    # Keyword-only: dispatch chiama get_gmail_credentials() senza settings.
    monkeypatch.setattr(read_mod, "get_gmail_credentials", lambda **_kwargs: _creds())
    return captured


def _mailbox_client(messages: dict[str, dict[str, Any]]) -> httpx.Client:
    """GET lista + GET dettaglio; il q= della list è ispezionabile via captured."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # Lista: /gmail/v1/users/me/messages senza id extra.
        if path.rstrip("/").endswith("/users/me/messages"):
            ids = [{"id": mid} for mid in messages]
            return httpx.Response(200, json={"messages": ids})
        msg_id = path.rsplit("/", 1)[-1]
        resource = messages.get(msg_id)
        if resource is None:
            return httpx.Response(404, json={"error": {"code": 404}})
        # Metadata vs full: per i test restituiamo sempre il resource completo.
        return httpx.Response(200, json=resource)

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_normalize_gmail_query_inbox_and_unread() -> None:
    """Sinonimi italiani → operatori; query Gmail già valide restano intatte."""
    assert normalize_gmail_query("") == "in:inbox"
    assert normalize_gmail_query("inbox") == "in:inbox"
    assert normalize_gmail_query("ultime email") == "in:inbox"
    assert normalize_gmail_query("non lette") == "is:unread"
    assert normalize_gmail_query("is:unread") == "is:unread"
    assert normalize_gmail_query("from:mario is:unread") == "from:mario is:unread"
    assert normalize_gmail_query("da mario") == "from:mario"
    assert normalize_gmail_query("non lette da mario") == "is:unread from:mario"
    assert normalize_gmail_query("fattura") == "fattura"
    assert normalize_gmail_query("oggetto fattura") == "subject:fattura"


def test_normalize_gmail_query_hours_to_after_epoch_and_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ore → after:epoch (orologio processo); giorni italiani → newer_than:Nd."""
    # Epoch del piano: Python deve usare questo now, non un timestamp inventato.
    frozen = 1_734_567_890
    monkeypatch.setattr(read_mod.time, "time", lambda: float(frozen))
    five_hours_ago = frozen - 5 * 3600
    after_five = f"after:{five_hours_ago}"

    assert normalize_gmail_query("ultime 5 ore") == after_five
    assert normalize_gmail_query("nelle ultime 5 ore") == after_five
    assert normalize_gmail_query("from:mario ultime 5 ore") == f"from:mario {after_five}"
    # newer_than:Nh non è un operatore Gmail: va riscritto come after:epoch.
    assert normalize_gmail_query("newer_than:5h") == after_five
    assert normalize_gmail_query("from:mario newer_than:5h") == f"from:mario {after_five}"

    assert normalize_gmail_query("ultimi 3 giorni") == "newer_than:3d"
    assert normalize_gmail_query("negli ultimi 3 giorni") == "newer_than:3d"
    assert normalize_gmail_query("from:mario ultimi 3 giorni") == "from:mario newer_than:3d"
    # Giorni già validi e after: a grano giorno passano invariati.
    assert normalize_gmail_query("from:mario newer_than:3d") == "from:mario newer_than:3d"
    assert normalize_gmail_query("after:2024/08/01") == "after:2024/08/01"


def test_clamp_list_limit_default_and_cap() -> None:
    assert clamp_list_limit(None) == 5
    assert clamp_list_limit("3") == 3
    assert clamp_list_limit(100) == 20
    assert clamp_list_limit(0) == 1


def test_list_emails_inbox_uses_in_inbox_query(tmp_path: Path) -> None:
    """Elenco inbox: q=in:inbox, esito numerato, sessione 1..N senza id parlati."""
    captured: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(str(request.url))
        qs = _query_of(request)
        path = request.url.path
        if path.rstrip("/").endswith("/users/me/messages"):
            assert qs.get("q") == ["in:inbox"]
            assert qs.get("maxResults") == ["5"]
            return httpx.Response(200, json={"messages": [{"id": "aaa"}, {"id": "bbb"}]})
        if path.endswith("/aaa"):
            return httpx.Response(200, json=_meta("aaa", "Mario Rossi <mario@x.test>", "Fattura"))
        if path.endswith("/bbb"):
            return httpx.Response(200, json=_meta("bbb", "Anna <anna@x.test>", "Riunione"))
        return httpx.Response(404, json={})

    session = MailboxSession()
    result = list_emails(
        "inbox",
        session=session,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        settings=_settings(tmp_path),
        credentials=_creds(),
    )
    assert result.startswith("OK: 2 email in inbox.")
    assert "1. Da Mario Rossi, oggetto Fattura." in result
    assert "2. Da Anna, oggetto Riunione." in result
    assert "aaa" not in result
    assert session.listed
    assert [item.gmail_id for item in session.items] == ["aaa", "bbb"]
    assert any("/users/me/messages" in url for url in captured)


def test_list_emails_unread_query(tmp_path: Path) -> None:
    """'non lette' diventa is:unread sulla list API."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.rstrip("/").endswith("/users/me/messages"):
            assert _query_of(request).get("q") == ["is:unread"]
            return httpx.Response(200, json={"messages": [{"id": "u1"}]})
        return httpx.Response(
            200,
            json=_meta("u1", "Mario <mario@x.test>", "Promemoria"),
        )

    result = list_emails(
        "non lette",
        session=MailboxSession(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        settings=_settings(tmp_path),
        credentials=_creds(),
    )
    assert result.startswith("OK: 1 email non lette.")
    assert "Da Mario, oggetto Promemoria." in result


def test_extract_prefers_plain_over_html_and_strips() -> None:
    """Multipart: text/plain vince; HTML-only viene stripato; niente tag in TTS."""
    both = _full(
        "m1",
        "Mario <m@x.test>",
        "Ciao",
        plain="Testo vero",
        html="<p>HTML da ignorare</p>",
    )
    assert extract_message_text(both) == "Testo vero"

    html_only = _full(
        "m2",
        "Mario <m@x.test>",
        "Ciao",
        html="<html><body><p>Ciao <b>Mario</b></p><script>x()</script></body></html>",
    )
    text = extract_message_text(html_only)
    assert "Ciao Mario" in text
    assert "<" not in text
    assert "x()" not in text

    assert "Ciao" in strip_html_to_text("<p>Ciao</p>")


def test_truncate_tts_body_cap() -> None:
    long = "a" * (MAX_BODY_CHARS + 50)
    cut, truncated = truncate_tts_body(long)
    assert truncated is True
    assert len(cut) == MAX_BODY_CHARS


def test_read_email_resolve_index_and_sender(tmp_path: Path) -> None:
    """name=1 e name=mittente risolvono sulla ultima lista; il corpo non cita l'id."""
    full = _full(
        "aaa",
        "Mario Rossi <mario@x.test>",
        "Fattura",
        plain="Pagare entro venerdì.",
    )
    client = _mailbox_client({"aaa": full})
    session = MailboxSession()
    session.replace(
        [MailboxItem("aaa", "Mario Rossi", "Fattura", "Mario Rossi <mario@x.test>")],
        query="inbox",
        gmail_q="in:inbox",
    )
    by_index = read_email(
        "la prima",
        session=session,
        client=client,
        settings=_settings(tmp_path),
        credentials=_creds(),
    )
    assert by_index.startswith("OK: email da Mario Rossi, oggetto Fattura.")
    assert "Pagare entro venerdì." in by_index
    assert "aaa" not in by_index

    # Piano: resolve anche sulla cifra parlata `1`, non solo sull'ordinale.
    by_digit = read_email(
        "1",
        session=session,
        client=client,
        settings=_settings(tmp_path),
        credentials=_creds(),
    )
    assert "Pagare entro venerdì." in by_digit

    by_sender = read_email(
        "mario",
        session=session,
        client=client,
        settings=_settings(tmp_path),
        credentials=_creds(),
    )
    assert "Pagare entro venerdì." in by_sender


def test_read_email_requires_list_and_empty_list() -> None:
    """Senza elenco → prima elenca; elenco vuoto → nessuna email in elenco."""
    blank = MailboxSession()
    assert read_email("1", session=blank).startswith("ERRORE: prima elenca")
    assert MSG_LIST_FIRST in read_email("1", session=blank)

    empty = MailboxSession()
    empty.replace([], query="inbox", gmail_q="in:inbox")
    assert read_email("1", session=empty) == MSG_EMPTY_LIST


def test_read_email_html_only_and_truncation(tmp_path: Path) -> None:
    """HTML-only strip + tetto caratteri annotato come troncato, senza MIME."""
    long_html = "<p>" + ("parola " * 400) + "</p>"
    full = _full("z1", "Anna <a@x.test>", "Lungo", html=long_html)
    session = MailboxSession()
    session.replace(
        [MailboxItem("z1", "Anna", "Lungo", "Anna <a@x.test>")],
        query="inbox",
        gmail_q="in:inbox",
    )
    result = read_email(
        "1",
        session=session,
        client=_mailbox_client({"z1": full}),
        settings=_settings(tmp_path),
        credentials=_creds(),
        max_chars=80,
    )
    assert "(troncato)" in result
    assert "<p>" not in result
    assert "z1" not in result
    body = result.split("\n", 1)[1]
    assert len(body) <= 80


def test_missing_token_speaks_desktop_auth(tmp_path: Path) -> None:
    """File token assente: messaggio autenticazione a tavolino, niente client HTTP."""
    reset_mailbox_session()
    result = list_emails(
        "inbox",
        session=MailboxSession(),
        settings=_settings(tmp_path),
    )
    assert result == MSG_GMAIL_NOT_LINKED
    assert "tavolino" in result


def test_list_emails_url_constant() -> None:
    """Contratto REST: stesso host del profile, collection messages."""
    assert GMAIL_MESSAGES_URL == "https://gmail.googleapis.com/gmail/v1/users/me/messages"


def test_extract_nested_multipart_skips_attachments() -> None:
    """multipart/mixed: plain interno vince; PDF senza data non entra nel TTS."""
    nested = {
        "id": "n1",
        "payload": {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [
                        {
                            "mimeType": "text/plain",
                            "body": {"data": _b64url("Corpo plain")},
                        },
                        {
                            "mimeType": "text/html",
                            "body": {"data": _b64url("<p>HTML da ignorare</p>")},
                        },
                    ],
                },
                {
                    "mimeType": "application/pdf",
                    "filename": "fattura.pdf",
                    "body": {"attachmentId": "att1"},
                },
            ],
        },
    }
    assert extract_message_text(nested) == "Corpo plain"


def test_list_emails_unread_from_mario_query(tmp_path: Path) -> None:
    """'non lette da mario' → q=is:unread from:mario sulla list API."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.rstrip("/").endswith("/users/me/messages"):
            assert _query_of(request).get("q") == ["is:unread from:mario"]
            return httpx.Response(200, json={"messages": [{"id": "u1"}]})
        return httpx.Response(200, json=_meta("u1", "Mario <mario@x.test>", "Fattura"))

    result = list_emails(
        "non lette da mario",
        session=MailboxSession(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        settings=_settings(tmp_path),
        credentials=_creds(),
    )
    assert result.startswith("OK: 1 email non lette.")
    assert "Da Mario, oggetto Fattura." in result


def test_format_count_result_phrases() -> None:
    """Conteggio parlante: da mittente, zero, tetto; niente righe numerate."""
    assert format_count_result(42, "from:mario", capped=False) == "OK: 42 email da mario."
    assert format_count_result(0, "from:mario", capped=False) == "OK: nessuna email da mario."
    assert (
        format_count_result(COUNT_CAP, "from:mario", capped=True)
        == f"OK: almeno {COUNT_CAP} email da mario."
    )


def test_list_emails_count_paginates_ids_without_touching_session(tmp_path: Path) -> None:
    """count=true: due pagine di id, maxResults=500, nessun GET metadata, sessione intatta."""
    list_calls: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # Il ramo count non deve mai chiedere From/Subject per messaggio.
        assert path.rstrip("/").endswith("/users/me/messages"), path
        qs = _query_of(request)
        list_calls.append(qs)
        assert qs.get("q") == ["from:mario"]
        assert qs.get("maxResults") == [str(COUNT_PAGE_SIZE)]
        assert qs.get("maxResults") != ["5"]
        token = (qs.get("pageToken") or [None])[0]
        if token is None:
            return httpx.Response(
                200,
                json={
                    "messages": [{"id": f"p1-{i}"} for i in range(4)],
                    "nextPageToken": "page2",
                },
            )
        assert token == "page2"
        return httpx.Response(
            200,
            json={"messages": [{"id": f"p2-{i}"} for i in range(3)]},
        )

    session = MailboxSession()
    prior = MailboxItem("aaa", "Anna", "Riunione", "Anna <anna@x.test>")
    session.replace([prior], query="inbox", gmail_q="in:inbox")
    result = list_emails(
        "from:mario",
        count=True,
        session=session,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        settings=_settings(tmp_path),
        credentials=_creds(),
    )
    assert result == "OK: 7 email da mario."
    assert "1. Da" not in result
    assert len(list_calls) == 2
    assert list_calls[0].get("pageToken") is None
    assert list_calls[1].get("pageToken") == ["page2"]
    # “leggi la prima” dopo un conteggio deve ancora vedere l'elenco precedente.
    assert session.listed
    assert [item.gmail_id for item in session.items] == ["aaa"]
    assert session.last_query == "inbox"
    assert session.last_gmail_q == "in:inbox"


def test_list_emails_count_absent_still_defaults_to_five(tmp_path: Path) -> None:
    """Senza count: elenco vocale resta maxResults=5 e aggiorna la sessione."""
    list_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal list_calls
        path = request.url.path
        qs = _query_of(request)
        if path.rstrip("/").endswith("/users/me/messages"):
            list_calls += 1
            assert qs.get("maxResults") == ["5"]
            return httpx.Response(200, json={"messages": [{"id": "aaa"}]})
        return httpx.Response(200, json=_meta("aaa", "Mario <mario@x.test>", "Ciao"))

    session = MailboxSession()
    result = list_emails(
        "inbox",
        session=session,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        settings=_settings(tmp_path),
        credentials=_creds(),
    )
    assert result.startswith("OK: 1 email in inbox.")
    assert "1. Da Mario, oggetto Ciao." in result
    assert list_calls == 1
    assert [item.gmail_id for item in session.items] == ["aaa"]


def test_list_emails_count_cap_says_at_least(tmp_path: Path) -> None:
    """Oltre il tetto: stop alla pagina del cap, reply 'almeno N', sessione intatta."""
    list_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal list_calls
        path = request.url.path
        assert path.rstrip("/").endswith("/users/me/messages"), path
        qs = _query_of(request)
        assert qs.get("maxResults") == [str(COUNT_PAGE_SIZE)]
        list_calls += 1
        # Ogni pagina è piena e ha un token: il ramo deve fermarsi a COUNT_CAP.
        start = (list_calls - 1) * COUNT_PAGE_SIZE
        ids = [{"id": f"m{start + i}"} for i in range(COUNT_PAGE_SIZE)]
        return httpx.Response(
            200,
            json={"messages": ids, "nextPageToken": f"page{list_calls + 1}"},
        )

    session = MailboxSession()
    result = list_emails(
        "from:mario",
        count=True,
        session=session,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        settings=_settings(tmp_path),
        credentials=_creds(),
    )
    expected_pages = COUNT_CAP // COUNT_PAGE_SIZE
    assert list_calls == expected_pages
    assert result == f"OK: almeno {COUNT_CAP} email da mario."
    assert not session.listed
    assert session.items == []


def test_list_emails_count_zero_does_not_list(tmp_path: Path) -> None:
    """Zero match: nessuna email da X; listed resta False (non è un elenco)."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert path.rstrip("/").endswith("/users/me/messages"), path
        assert _query_of(request).get("maxResults") == [str(COUNT_PAGE_SIZE)]
        return httpx.Response(200, json={})

    session = MailboxSession()
    result = list_emails(
        "da mario",
        count=True,
        session=session,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        settings=_settings(tmp_path),
        credentials=_creds(),
    )
    assert result == "OK: nessuna email da mario."
    assert not session.listed
    assert session.items == []


def test_list_emails_count_sends_after_epoch_and_newer_than(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """count=true: q= verso Gmail ha after:epoch / newer_than:3d; sessione intatta."""
    frozen = 1_734_567_890
    monkeypatch.setattr(read_mod.time, "time", lambda: float(frozen))
    seen_q: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # Solo lista id: il ramo count non deve GET metadata per messaggio.
        assert path.rstrip("/").endswith("/users/me/messages"), path
        qs = _query_of(request)
        assert qs.get("maxResults") == [str(COUNT_PAGE_SIZE)]
        assert qs.get("maxResults") != ["5"]
        seen_q.append((qs.get("q") or [""])[0])
        return httpx.Response(200, json={"messages": [{"id": "m1"}, {"id": "m2"}]})

    session = MailboxSession()
    prior = MailboxItem("aaa", "Anna", "Riunione", "Anna <anna@x.test>")
    session.replace([prior], query="inbox", gmail_q="in:inbox")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    settings = _settings(tmp_path)
    creds = _creds()

    days_result = list_emails(
        "from:mario ultimi 3 giorni",
        count=True,
        session=session,
        client=client,
        settings=settings,
        credentials=creds,
    )
    assert days_result == "OK: 2 email da mario."
    assert seen_q[-1] == "from:mario newer_than:3d"

    hours_result = list_emails(
        "from:mario ultime 5 ore",
        count=True,
        session=session,
        client=client,
        settings=settings,
        credentials=creds,
    )
    assert hours_result == "OK: 2 email da mario."
    assert seen_q[-1] == f"from:mario after:{frozen - 5 * 3600}"
    # Un conteggio temporale non deve sovrascrivere l'elenco vocale precedente.
    assert session.listed
    assert [item.gmail_id for item in session.items] == ["aaa"]
    assert session.last_gmail_q == "in:inbox"


def test_list_emails_http_401_speaks_desktop_auth(tmp_path: Path) -> None:
    """HTTP 401 sul mock: stesso messaggio a tavolino, niente retry OAuth."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"code": 401}})

    result = list_emails(
        "inbox",
        session=MailboxSession(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        settings=_settings(tmp_path),
        credentials=_creds(),
    )
    assert result == MSG_GMAIL_NOT_LINKED
    assert "tavolino" in result


def test_dispatch_unknown_tool_is_spoken_error() -> None:
    """Whitelist Gmail: i tool FS del master non devono partire né toccare HTTP."""
    result = dispatch_gmail_tool("create_text_file", {"name": "spesa", "content": "latte"})
    assert result.startswith("ERRORE:")
    assert "list_emails" in result
    assert "read_email" in result


def _assert_mocked_gmail_only(captured: list[str]) -> None:
    """Le GET devono essere solo Gmail REST mockate: niente token/OAuth host."""
    assert captured, "il MockTransport deve aver visto almeno una GET"
    for url in captured:
        assert "gmail.googleapis.com" in url
        assert "oauth2.googleapis.com" not in url
        assert "accounts.google.com" not in url


def _inbox_two_messages_handler(request: httpx.Request) -> httpx.Response:
    """Due email inbox (lista + full): Bearer finto, q=in:inbox, id mai nel TTS."""
    # Token iniettato da _patch_gmail_http: se manca, il dispatch sta usando altro path.
    assert request.headers.get("Authorization") == f"Bearer {_ACCESS}"
    path = request.url.path
    if path.rstrip("/").endswith("/users/me/messages"):
        assert _query_of(request).get("q") == ["in:inbox"]
        return httpx.Response(200, json={"messages": [{"id": "aaa"}, {"id": "bbb"}]})
    if path.endswith("/aaa"):
        return httpx.Response(
            200,
            json=_full(
                "aaa",
                "Mario Rossi <mario@x.test>",
                "Fattura",
                plain="Pagare entro venerdì.",
            ),
        )
    if path.endswith("/bbb"):
        return httpx.Response(
            200,
            json=_full("bbb", "Anna <anna@x.test>", "Riunione", plain="Domani alle dieci."),
        )
    return httpx.Response(404, json={})


class _ScriptedLLM:
    """LLM fake: coda di JSON, zero rete. Come test_audio_bridge, ma per Gmail."""

    def __init__(self, replies: list[str]) -> None:
        self.last_usage = TokenUsage()
        self._replies = list(replies)
        self.calls = 0

    def chat(self, messages: list[dict[str, str]], *args: object, **kwargs: object) -> str:
        self.calls += 1
        # Primo round: lo spec Gmail, non il master FS, deve stare in testa.
        if self.calls == 1:
            system = messages[0]["content"]
            assert system == GMAIL_LOOP_SPEC.system_prompt
            assert "list_emails" in system
            assert "create_text_file" not in system
        if not self._replies:
            raise AssertionError(f"chiamata LLM extra #{self.calls} su {messages[-1]!r}")
        return self._replies.pop(0)

    def close(self) -> None:
        return None


def test_gmail_loop_list_emails_then_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Loop vocale + spec Gmail: list_emails poi none. MockTransport, zero Google."""
    captured = _patch_gmail_http(monkeypatch, _inbox_two_messages_handler)
    llm = _ScriptedLLM(
        [
            '{"tool": "list_emails", "args": {"query": "inbox"}}',
            (
                '{"tool": "none", "reply": '
                '"Hai due email: da Mario, fattura, e da Anna, riunione."}'
            ),
        ]
    )
    outfile = StringIO()
    code = run_chat_loop(
        MockSTT(infile=StringIO("ultime email\nesci\n"), outfile=outfile, prompt=""),
        MockTTS(outfile=outfile, prefix="[TTS] "),
        llm,
        report_latency=False,
        telemetry_db=tmp_path / "telemetry.db",
        spec=GMAIL_LOOP_SPEC,
    )
    assert code == 0
    spoken = outfile.getvalue()
    # Intro specialista + sintesi none; il master FS non deve comparire.
    assert "Agente Gmail in sola lettura" in spoken
    assert "Lab Ollama FS" not in spoken
    assert "Hai due email: da Mario, fattura, e da Anna, riunione." in spoken
    assert "aaa" not in spoken
    assert llm.calls == 2
    # Dispatch reale: sessione 1..N popolata, stdout [GMAIL] come [FS]/[RAG].
    session = get_mailbox_session()
    assert [item.gmail_id for item in session.items] == ["aaa", "bbb"]
    stdout = capsys.readouterr().out
    assert "[GMAIL] OK: 2 email in inbox." in stdout
    _assert_mocked_gmail_only(captured)


def test_gmail_loop_list_then_read_email(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Due turni vocali: elenco poi 'leggi la prima'. Id Gmail solo in sessione."""
    captured = _patch_gmail_http(monkeypatch, _inbox_two_messages_handler)
    llm = _ScriptedLLM(
        [
            '{"tool": "list_emails", "args": {"query": "inbox"}}',
            '{"tool": "none", "reply": "Hai due email in inbox."}',
            '{"tool": "read_email", "args": {"name": "la prima"}}',
            '{"tool": "none", "reply": "Mario chiede di pagare entro venerdì."}',
        ]
    )
    outfile = StringIO()
    code = run_chat_loop(
        MockSTT(
            infile=StringIO("ultime email\nleggi la prima\nesci\n"),
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
    assert "Hai due email in inbox." in spoken
    assert "Mario chiede di pagare entro venerdì." in spoken
    assert "Pagare entro venerdì." not in spoken
    assert "aaa" not in spoken
    assert llm.calls == 4
    stdout = capsys.readouterr().out
    assert "[GMAIL] OK: 2 email in inbox." in stdout
    assert "[GMAIL] OK: email da Mario Rossi, oggetto Fattura." in stdout
    assert "Pagare entro venerdì." in stdout
    _assert_mocked_gmail_only(captured)
