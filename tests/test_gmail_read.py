"""Tool Gmail readonly: MockTransport httpx, zero hit a Google."""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
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
from lavora_e_guida.gmail.agent import (
    GMAIL_LOOP_SPEC,
    GMAIL_TOOL_DECLARATIONS,
    dispatch_gmail_tool,
)
from lavora_e_guida.gmail.oauth import GMAIL_SCOPES, MSG_GMAIL_NOT_LINKED
from lavora_e_guida.gmail.read import (
    COUNT_CAP,
    COUNT_PAGE_SIZE,
    EMAIL_ATTACHMENTS_DIRNAME,
    GMAIL_MESSAGES_URL,
    MAX_ATTACHMENT_BYTES,
    MAX_BODY_CHARS,
    MSG_EMPTY_LIST,
    MSG_EMPTY_NAME,
    MSG_GONE,
    MSG_LIST_FIRST,
    MSG_NO_ATTACHMENTS,
    MailboxItem,
    MailboxSession,
    clamp_list_limit,
    extract_message_text,
    format_count_result,
    format_list_result,
    get_mailbox_session,
    item_from_message,
    iter_real_attachments,
    list_emails,
    normalize_gmail_query,
    read_email,
    reset_mailbox_session,
    sanitize_attachment_filename,
    save_attachments,
    speak_list_attachment_suffix,
    speak_read_attachment_suffix,
    strip_html_to_text,
    truncate_tts_body,
)
from lavora_e_guida.llm.turn import FunctionCall, LlmTurn
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
    extra_parts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resource format=full: multipart/alternative se ci sono entrambi i body.

    `extra_parts` (PDF, immagini) vanno in multipart/mixed sopra l'alternative:
    così i test di elenco/lettura possono iniettare allegati veri o logo inline.
    """
    parts: list[dict[str, Any]] = []
    if plain is not None:
        parts.append({"mimeType": "text/plain", "body": {"data": _b64url(plain)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": _b64url(html)}})
    extras = list(extra_parts or [])
    payload: dict[str, Any] = {"headers": _headers(sender, subject)}
    if extras:
        # mixed: corpo (nudo o alternative) + file; filename/size restano sui figli.
        inner: dict[str, Any]
        if len(parts) == 1:
            inner = {"mimeType": parts[0]["mimeType"], "body": parts[0]["body"]}
        elif parts:
            inner = {"mimeType": "multipart/alternative", "parts": parts}
        else:
            inner = {"mimeType": "text/plain", "body": {}}
        payload["mimeType"] = "multipart/mixed"
        payload["parts"] = [inner, *extras]
    elif len(parts) == 1:
        payload["mimeType"] = parts[0]["mimeType"]
        payload["body"] = parts[0]["body"]
    elif parts:
        payload["mimeType"] = "multipart/alternative"
        payload["parts"] = parts
    message: dict[str, Any] = {"id": msg_id, "payload": payload}
    if snippet is not None:
        message["snippet"] = snippet
    return message


def _mime_part(
    mime: str,
    filename: str,
    *,
    size: int = 10_000,
    attachment_id: str = "att1",
    disposition: str | None = None,
    content_id: str | None = None,
) -> dict[str, Any]:
    """MessagePart Gmail (metadata o full): filename/size/header, niente body.data."""
    headers: list[dict[str, str]] = []
    if disposition is not None:
        headers.append({"name": "Content-Disposition", "value": disposition})
    if content_id is not None:
        headers.append({"name": "Content-ID", "value": content_id})
    return {
        "mimeType": mime,
        "filename": filename,
        "headers": headers,
        "body": {"attachmentId": attachment_id, "size": size},
    }


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


def _b64url_bytes(data: bytes) -> str:
    """Stesso encoding dei binari Gmail (urlsafe, padding spesso assente)."""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _mailbox_client_with_blobs(
    messages: dict[str, dict[str, Any]],
    blobs: dict[tuple[str, str], bytes],
    captured: list[str] | None = None,
) -> httpx.Client:
    """GET messaggio + GET attachments/{id}; il binario è in `blobs[(msg, aid)]`."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if captured is not None:
            captured.append(path)
        # Allegato: .../messages/{id}/attachments/{aid} — prima del dettaglio messaggio.
        if "/attachments/" in path:
            pieces = [part for part in path.split("/") if part]
            att_index = pieces.index("attachments")
            msg_id = pieces[att_index - 1]
            att_id = pieces[att_index + 1]
            blob = blobs.get((msg_id, att_id))
            if blob is None:
                return httpx.Response(404, json={"error": {"code": 404}})
            return httpx.Response(
                200,
                json={"size": len(blob), "data": _b64url_bytes(blob)},
            )
        if path.rstrip("/").endswith("/users/me/messages"):
            ids = [{"id": mid} for mid in messages]
            return httpx.Response(200, json={"messages": ids})
        msg_id = path.rsplit("/", 1)[-1]
        resource = messages.get(msg_id)
        if resource is None:
            return httpx.Response(404, json={"error": {"code": 404}})
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


def test_item_from_message_keeps_thread_id_and_rfc_headers() -> None:
    """Sessione: threadId, From, To/Cc, Reply-To e Message-ID restano sull'item, mai nel TTS."""
    message = {
        "id": "aaa",
        "threadId": "thread-xyz",
        "payload": {
            "headers": [
                {"name": "From", "value": "Mario Rossi <mario.rossi@x.test>"},
                {"name": "Subject", "value": "Fattura"},
                {"name": "Reply-To", "value": "Mario Rossi <reply@x.test>"},
                {"name": "Message-ID", "value": "<msg-1@x.test>"},
                {"name": "References", "value": "<prev@x.test>"},
                # Due To: getaddresses li tiene entrambi; parseaddr avrebbe solo Mario.
                {
                    "name": "To",
                    "value": "Mario Rossi <mario.rossi@x.test>, Anna <anna@x.test>",
                },
                {"name": "Cc", "value": "Segreteria <segreteria@x.test>"},
            ]
        },
    }
    item = item_from_message(message)
    assert item is not None
    assert item.gmail_id == "aaa"
    assert item.sender == "Mario Rossi"
    assert item.subject == "Fattura"
    assert item.thread_id == "thread-xyz"
    assert item.from_address == "mario.rossi@x.test"
    assert item.reply_to_address == "reply@x.test"
    assert item.rfc_message_id == "<msg-1@x.test>"
    assert item.rfc_references == "<prev@x.test>"
    assert item.to_addresses == ("mario.rossi@x.test", "anna@x.test")
    assert item.cc_addresses == ("segreteria@x.test",)
    # Lista parlata: display name e oggetto, niente @ né id di thread.
    spoken = format_list_result([item], "in:inbox")
    assert "1. Da Mario Rossi, oggetto Fattura." in spoken
    assert "@" not in spoken
    assert "thread-xyz" not in spoken
    assert "msg-1" not in spoken
    assert "anna@x.test" not in spoken
    assert "segreteria@x.test" not in spoken


def test_item_from_message_without_reply_to_or_thread() -> None:
    """Header opzionali assenti: indirizzo dal From, gli altri campi restano vuoti."""
    item = item_from_message(
        {
            "id": "aaa",
            "payload": {"headers": _headers("Mario <mario@x.test>", "Ciao")},
        }
    )
    assert item is not None
    assert item.thread_id == ""
    assert item.from_address == "mario@x.test"
    assert item.reply_to_address == ""
    assert item.rfc_message_id == ""
    assert item.rfc_references == ""
    assert item.to_addresses == ()
    assert item.cc_addresses == ()
    spoken = format_list_result([item], "in:inbox")
    assert "Da Mario, oggetto Ciao." in spoken
    assert "@" not in spoken


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
    # Lista TTS: display name, mai l'@ del From (resta in sessione per draft/reply).
    assert "@" not in result
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
    # Il PDF è un allegato vero: l'estrazione testo lo ignora, l'euristica lo conta.
    reals = iter_real_attachments(nested["payload"])
    assert [item.filename for item in reals] == ["fattura.pdf"]


def test_iter_real_attachments_pdf_counted_inline_png_excluded() -> None:
    """PDF (anche senza disposition) è vero; image/png inline+cid è logo, non TTS."""
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64url("Ciao")}},
            _mime_part("application/pdf", "fattura.pdf", size=80_000),
            _mime_part(
                "image/png",
                "logo.png",
                size=2_000,
                disposition="inline; filename=\"logo.png\"",
                content_id="<logo@x.test>",
            ),
        ],
    }
    reals = iter_real_attachments(payload)
    assert [item.filename for item in reals] == ["fattura.pdf"]
    assert reals[0].mime_type == "application/pdf"
    assert speak_list_attachment_suffix(len(reals)) == ", 1 allegato"
    assert speak_read_attachment_suffix(reals) == ", 1 allegato fattura.pdf"


def test_iter_real_attachments_decorative_names_and_small_images() -> None:
    """image001, signature, untitled e PNG sotto 40 KiB senza attachment: ignorati."""
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            _mime_part("image/png", "image001.png", size=8_000),
            _mime_part("image/gif", "signature.gif", size=50_000),
            _mime_part("image/png", "untitled", size=12_000),
            _mime_part("image/jpeg", "icona.jpg", size=10_000),
            _mime_part(
                "image/jpeg",
                "vacanze.jpg",
                size=200_000,
                disposition="attachment; filename=\"vacanze.jpg\"",
            ),
        ],
    }
    reals = iter_real_attachments(payload)
    assert [item.filename for item in reals] == ["vacanze.jpg"]
    assert speak_list_attachment_suffix(2) == ", 2 allegati"
    assert speak_list_attachment_suffix(0) == ""
    assert speak_read_attachment_suffix([]) == ""


def test_iter_real_attachments_non_image_without_disposition() -> None:
    """doc/zip con filename restano veri anche se Content-Disposition manca."""
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            _mime_part("application/zip", "contratto.zip", size=500_000),
            _mime_part(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "verbale.docx",
                size=40_000,
            ),
        ],
    }
    names = [item.filename for item in iter_real_attachments(payload)]
    assert names == ["contratto.zip", "verbale.docx"]
    assert speak_read_attachment_suffix(iter_real_attachments(payload)) == (
        ", 2 allegati contratto.zip, verbale.docx"
    )


def test_list_emails_speaks_real_attachment_count(tmp_path: Path) -> None:
    """Elenco metadata: PDF → ', 1 allegato'; logo inline non aggiunge suffisso."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.rstrip("/").endswith("/users/me/messages"):
            return httpx.Response(200, json={"messages": [{"id": "aaa"}, {"id": "bbb"}]})
        if path.endswith("/aaa"):
            return httpx.Response(
                200,
                json=_full(
                    "aaa",
                    "Mario Rossi <mario@x.test>",
                    "Fattura",
                    plain="Pagare.",
                    extra_parts=[
                        _mime_part("application/pdf", "fattura.pdf", size=80_000),
                        _mime_part(
                            "image/png",
                            "logo.png",
                            size=1_500,
                            disposition="inline",
                            content_id="<logo@x.test>",
                        ),
                    ],
                ),
            )
        if path.endswith("/bbb"):
            return httpx.Response(
                200,
                json=_full(
                    "bbb",
                    "Anna <anna@x.test>",
                    "Riunione",
                    plain="Domani.",
                    extra_parts=[
                        _mime_part(
                            "image/png",
                            "image001.png",
                            size=2_000,
                            disposition="inline",
                            content_id="<img001>",
                        ),
                    ],
                ),
            )
        return httpx.Response(404, json={})

    session = MailboxSession()
    result = list_emails(
        "inbox",
        session=session,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        settings=_settings(tmp_path),
        credentials=_creds(),
    )
    assert "1. Da Mario Rossi, oggetto Fattura, 1 allegato." in result
    # Solo logo: niente suffisso, così Gemini non inventa un PDF.
    assert "2. Da Anna, oggetto Riunione." in result
    assert "2. Da Anna, oggetto Riunione, " not in result
    assert session.items[0].attachment_count == 1
    assert session.items[1].attachment_count == 0


def test_list_emails_requests_format_metadata_not_full(tmp_path: Path) -> None:
    """Piano: in lista format=metadata (filename/size/disposition), mai GET full."""
    seen_detail: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        qs = _query_of(request)
        if path.rstrip("/").endswith("/users/me/messages"):
            return httpx.Response(200, json={"messages": [{"id": "aaa"}]})
        if path.endswith("/aaa"):
            # Solo il dettaglio messaggio: qui deve comparire format=metadata.
            seen_detail.append(qs)
            # Resource come metadata Gmail: parts con filename/size, niente body.data.
            return httpx.Response(
                200,
                json={
                    "id": "aaa",
                    "threadId": "thread-aaa",
                    "payload": {
                        "headers": [
                            *_headers("Mario <mario@x.test>", "Fattura"),
                            {"name": "Reply-To", "value": "Mario <reply@x.test>"},
                            {"name": "Message-ID", "value": "<aaa@x.test>"},
                            {
                                "name": "To",
                                "value": "Mario <mario@x.test>, Anna <anna@x.test>",
                            },
                            {"name": "Cc", "value": "Segreteria <segreteria@x.test>"},
                        ],
                        "mimeType": "multipart/mixed",
                        "parts": [
                            {"mimeType": "text/plain", "body": {"size": 12}},
                            _mime_part("application/pdf", "fattura.pdf", size=80_000),
                            _mime_part(
                                "image/png",
                                "logo.png",
                                size=1_500,
                                disposition="inline",
                                content_id="<logo@x.test>",
                            ),
                        ],
                    },
                },
            )
        return httpx.Response(404, json={})

    session = MailboxSession()
    result = list_emails(
        "inbox",
        session=session,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        settings=_settings(tmp_path),
        credentials=_creds(),
    )
    assert seen_detail, "list_emails deve GET il dettaglio per ogni id"
    assert seen_detail[0].get("format") == ["metadata"]
    # Nessun format=full: il body non serve per il flag allegati in elenco.
    assert seen_detail[0].get("format") != ["full"]
    # From/Subject già c'erano; Message-ID, Reply-To, To e Cc restano in sessione.
    meta_headers = seen_detail[0].get("metadataHeaders") or []
    assert "From" in meta_headers
    assert "Subject" in meta_headers
    assert "Message-ID" in meta_headers
    assert "Reply-To" in meta_headers
    assert "References" in meta_headers
    assert "To" in meta_headers
    assert "Cc" in meta_headers
    assert "1. Da Mario, oggetto Fattura, 1 allegato." in result
    assert "logo.png" not in result
    # TTS lista invariato: l'@ resta in sessione, non nelle frasi.
    assert "@" not in result
    assert session.items[0].thread_id == "thread-aaa"
    assert session.items[0].from_address == "mario@x.test"
    assert session.items[0].reply_to_address == "reply@x.test"
    assert session.items[0].rfc_message_id == "<aaa@x.test>"
    assert session.items[0].to_addresses == ("mario@x.test", "anna@x.test")
    assert session.items[0].cc_addresses == ("segreteria@x.test",)


def test_read_email_speaks_attachment_filename(tmp_path: Path) -> None:
    """Lettura: header con nome file vero; il logo inline non compare nel TTS."""
    full = _full(
        "aaa",
        "Mario Rossi <mario@x.test>",
        "Fattura",
        plain="Pagare entro venerdì.",
        extra_parts=[
            _mime_part("application/pdf", "fattura.pdf", size=80_000),
            _mime_part(
                "image/png",
                "logo.png",
                size=1_200,
                disposition="inline",
                content_id="<logo@x.test>",
            ),
        ],
    )
    session = MailboxSession()
    session.replace(
        [MailboxItem("aaa", "Mario Rossi", "Fattura", "Mario Rossi <mario@x.test>")],
        query="inbox",
        gmail_q="in:inbox",
    )
    result = read_email(
        "1",
        session=session,
        client=_mailbox_client({"aaa": full}),
        settings=_settings(tmp_path),
        credentials=_creds(),
    )
    assert result.startswith(
        "OK: email da Mario Rossi, oggetto Fattura, 1 allegato fattura.pdf."
    )
    assert "logo.png" not in result
    assert "Pagare entro venerdì." in result


def test_read_email_logo_only_has_no_attachment_suffix(tmp_path: Path) -> None:
    """Solo firma/logo: stessa formula di un'email senza file, niente 'allegato'."""
    full = _full(
        "aaa",
        "Mario <mario@x.test>",
        "Ciao",
        plain="Solo testo.",
        extra_parts=[
            _mime_part(
                "image/png",
                "logo.png",
                size=800,
                disposition="inline",
                content_id="<logo@x.test>",
            ),
        ],
    )
    session = MailboxSession()
    session.replace(
        [MailboxItem("aaa", "Mario", "Ciao", "Mario <mario@x.test>")],
        query="inbox",
        gmail_q="in:inbox",
    )
    result = read_email(
        "1",
        session=session,
        client=_mailbox_client({"aaa": full}),
        settings=_settings(tmp_path),
        credentials=_creds(),
    )
    assert result.startswith("OK: email da Mario, oggetto Ciao.")
    assert "allegato" not in result.split("\n", 1)[0]


def test_sanitize_attachment_filename_strips_traversal_and_windows_chars() -> None:
    """Path traversal e caratteri NTFS non devono uscire dalla cartella del giorno."""
    assert sanitize_attachment_filename("../../Windows/system.ini") == "system.ini"
    assert sanitize_attachment_filename("foo/../../../etc/passwd") == "passwd"
    assert sanitize_attachment_filename("C:\\Windows\\x.pdf") == "x.pdf"
    assert sanitize_attachment_filename("..") == "allegato"
    assert sanitize_attachment_filename("a:b|c?.pdf") == "a_b_c_.pdf"
    assert sanitize_attachment_filename("CON.txt").casefold().startswith("_con")
    assert "/" not in sanitize_attachment_filename("../evil.pdf")
    assert "\\" not in sanitize_attachment_filename("..\\evil.pdf")


def test_save_attachments_writes_pdf_skips_inline_png(tmp_path: Path) -> None:
    """PDF vero → disco + TTS; logo inline non si GET e non si scrive."""
    pdf_bytes = b"%PDF-1.4 fake-invoice"
    full = _full(
        "aaa",
        "Mario Rossi <mario@x.test>",
        "Fattura",
        plain="Pagare.",
        extra_parts=[
            _mime_part("application/pdf", "fattura.pdf", size=len(pdf_bytes), attachment_id="pdf1"),
            _mime_part(
                "image/png",
                "logo.png",
                size=1_200,
                attachment_id="logo1",
                disposition="inline",
                content_id="<logo@x.test>",
            ),
        ],
    )
    captured: list[str] = []
    client = _mailbox_client_with_blobs(
        {"aaa": full},
        {("aaa", "pdf1"): pdf_bytes, ("aaa", "logo1"): b"\x89PNG"},
        captured,
    )
    session = MailboxSession()
    session.replace(
        [MailboxItem("aaa", "Mario Rossi", "Fattura", "Mario Rossi <mario@x.test>")],
        query="inbox",
        gmail_q="in:inbox",
    )
    day = date(2026, 8, 19)
    result = save_attachments(
        "1",
        session=session,
        client=client,
        settings=_settings(tmp_path),
        credentials=_creds(),
        workspace=tmp_path,
        today=day,
    )
    assert result == (
        f"OK: 1 allegato salvato in {EMAIL_ATTACHMENTS_DIRNAME}/2026-08-19: fattura.pdf."
    )
    dest = tmp_path / EMAIL_ATTACHMENTS_DIRNAME / "2026-08-19" / "fattura.pdf"
    assert dest.read_bytes() == pdf_bytes
    # Solo il PDF: il logo non deve comparire né sul disco né nel GET attachments.
    assert not (tmp_path / EMAIL_ATTACHMENTS_DIRNAME / "2026-08-19" / "logo.png").exists()
    assert not (tmp_path / "notes").exists()
    assert not (tmp_path / "inbox").exists()
    assert any(path.endswith("/attachments/pdf1") for path in captured)
    assert not any(path.endswith("/attachments/logo1") for path in captured)
    assert "aaa" not in result


def test_save_attachments_no_real_attachments_speaks_none(tmp_path: Path) -> None:
    """Solo firma/logo: OK nessun allegato, zero write e zero GET binario."""
    full = _full(
        "aaa",
        "Mario <mario@x.test>",
        "Ciao",
        plain="Solo testo.",
        extra_parts=[
            _mime_part(
                "image/png",
                "logo.png",
                size=800,
                attachment_id="logo1",
                disposition="inline",
                content_id="<logo@x.test>",
            ),
        ],
    )
    captured: list[str] = []
    session = MailboxSession()
    session.replace(
        [MailboxItem("aaa", "Mario", "Ciao", "Mario <mario@x.test>")],
        query="inbox",
        gmail_q="in:inbox",
    )
    result = save_attachments(
        "1",
        session=session,
        client=_mailbox_client_with_blobs(
            {"aaa": full},
            {("aaa", "logo1"): b"\x89PNG"},
            captured,
        ),
        settings=_settings(tmp_path),
        credentials=_creds(),
        workspace=tmp_path,
        today=date(2026, 8, 19),
    )
    assert result == MSG_NO_ATTACHMENTS
    day_dir = tmp_path / EMAIL_ATTACHMENTS_DIRNAME / "2026-08-19"
    assert not day_dir.exists() or not any(day_dir.iterdir())
    assert not any("/attachments/" in path for path in captured)


def test_save_attachments_plain_email_speaks_none(tmp_path: Path) -> None:
    """Email solo testo, zero extra_parts: stesso OK nessun allegato, zero write."""
    full = _full("aaa", "Mario <mario@x.test>", "Ciao", plain="Solo testo.")
    captured: list[str] = []
    session = MailboxSession()
    session.replace(
        [MailboxItem("aaa", "Mario", "Ciao", "Mario <mario@x.test>")],
        query="inbox",
        gmail_q="in:inbox",
    )
    result = save_attachments(
        "1",
        session=session,
        client=_mailbox_client_with_blobs({"aaa": full}, {}, captured),
        settings=_settings(tmp_path),
        credentials=_creds(),
        workspace=tmp_path,
        today=date(2026, 8, 19),
    )
    assert result == MSG_NO_ATTACHMENTS
    assert not (tmp_path / EMAIL_ATTACHMENTS_DIRNAME).exists()
    # Senza attachmentId veri non deve partire users.messages.attachments.get.
    assert not any("/attachments/" in path for path in captured)


def test_save_attachments_path_traversal_stays_in_day_folder(tmp_path: Path) -> None:
    """Filename `../..` → basename sanitizzato sotto email_attachments/oggi/."""
    blob = b"%PDF-1.4 evil"
    full = _full(
        "aaa",
        "Mario <mario@x.test>",
        "Fattura",
        plain="Pagare.",
        extra_parts=[
            _mime_part(
                "application/pdf",
                "../../Windows/fattura.pdf",
                size=len(blob),
                attachment_id="pdf1",
            ),
        ],
    )
    session = MailboxSession()
    session.replace(
        [MailboxItem("aaa", "Mario", "Fattura", "Mario <mario@x.test>")],
        query="inbox",
        gmail_q="in:inbox",
    )
    day = date(2026, 8, 19)
    result = save_attachments(
        "1",
        session=session,
        client=_mailbox_client_with_blobs({"aaa": full}, {("aaa", "pdf1"): blob}),
        settings=_settings(tmp_path),
        credentials=_creds(),
        workspace=tmp_path,
        today=day,
    )
    day_dir = (tmp_path / EMAIL_ATTACHMENTS_DIRNAME / "2026-08-19").resolve()
    dest = day_dir / "fattura.pdf"
    assert dest.is_file()
    assert dest.read_bytes() == blob
    assert dest.resolve().parent == day_dir
    assert ".." not in result
    assert "Windows" not in result
    assert result == (
        f"OK: 1 allegato salvato in {EMAIL_ATTACHMENTS_DIRNAME}/2026-08-19: fattura.pdf."
    )


def test_save_attachments_requires_list_like_read() -> None:
    """Senza elenco e lista vuota: stessi errori parlanti di read_email."""
    blank = MailboxSession()
    assert save_attachments("1", session=blank) == MSG_LIST_FIRST
    empty = MailboxSession()
    empty.replace([], query="inbox", gmail_q="in:inbox")
    assert save_attachments("1", session=empty) == MSG_EMPTY_LIST


def test_save_attachments_skips_oversize_without_blocking_others(tmp_path: Path) -> None:
    """Oltre 15 MiB: skip parlante, niente GET di quel file, gli altri si salvano."""
    small = b"%PDF-1.4 ok"
    full = _full(
        "aaa",
        "Mario <mario@x.test>",
        "Misto",
        plain="Due file.",
        extra_parts=[
            _mime_part(
                "application/pdf",
                "fattura.pdf",
                size=len(small),
                attachment_id="pdf1",
            ),
            _mime_part(
                "application/zip",
                "archivio.zip",
                size=MAX_ATTACHMENT_BYTES + 1,
                attachment_id="zip1",
            ),
        ],
    )
    captured: list[str] = []
    session = MailboxSession()
    session.replace(
        [MailboxItem("aaa", "Mario", "Misto", "Mario <mario@x.test>")],
        query="inbox",
        gmail_q="in:inbox",
    )
    result = save_attachments(
        "1",
        session=session,
        client=_mailbox_client_with_blobs(
            {"aaa": full},
            {("aaa", "pdf1"): small, ("aaa", "zip1"): b"PK" * 10},
            captured,
        ),
        settings=_settings(tmp_path),
        credentials=_creds(),
        workspace=tmp_path,
        today=date(2026, 8, 19),
    )
    assert "fattura.pdf" in result
    assert "troppo grande, saltato" in result
    assert "archivio.zip" not in result
    dest = tmp_path / EMAIL_ATTACHMENTS_DIRNAME / "2026-08-19" / "fattura.pdf"
    assert dest.read_bytes() == small
    assert not (tmp_path / EMAIL_ATTACHMENTS_DIRNAME / "2026-08-19" / "archivio.zip").exists()
    assert any(path.endswith("/attachments/pdf1") for path in captured)
    assert not any(path.endswith("/attachments/zip1") for path in captured)


def test_save_attachments_two_files_speaks_both_names(tmp_path: Path) -> None:
    """Due veri: formula plurale e entrambi i nomi, cartella del giorno locale."""
    pdf_bytes = b"%PDF-1.4 a"
    jpg_bytes = b"\xff\xd8\xff fake-jpeg"
    full = _full(
        "aaa",
        "Mario <mario@x.test>",
        "Fattura",
        plain="Allegati.",
        extra_parts=[
            _mime_part(
                "application/pdf",
                "fattura.pdf",
                size=len(pdf_bytes),
                attachment_id="pdf1",
            ),
            _mime_part(
                "image/jpeg",
                "foto.jpg",
                size=len(jpg_bytes),
                attachment_id="jpg1",
                disposition='attachment; filename="foto.jpg"',
            ),
        ],
    )
    session = MailboxSession()
    session.replace(
        [MailboxItem("aaa", "Mario", "Fattura", "Mario <mario@x.test>")],
        query="inbox",
        gmail_q="in:inbox",
    )
    result = save_attachments(
        "1",
        session=session,
        client=_mailbox_client_with_blobs(
            {"aaa": full},
            {("aaa", "pdf1"): pdf_bytes, ("aaa", "jpg1"): jpg_bytes},
        ),
        settings=_settings(tmp_path),
        credentials=_creds(),
        workspace=tmp_path,
        today=date(2026, 8, 19),
    )
    assert result == (
        f"OK: 2 allegati salvati in {EMAIL_ATTACHMENTS_DIRNAME}/2026-08-19: "
        "fattura.pdf, foto.jpg."
    )
    day_dir = tmp_path / EMAIL_ATTACHMENTS_DIRNAME / "2026-08-19"
    assert (day_dir / "fattura.pdf").read_bytes() == pdf_bytes
    assert (day_dir / "foto.jpg").read_bytes() == jpg_bytes


def test_save_attachments_resolves_sender_like_read(tmp_path: Path) -> None:
    """Stessa chiave name di read_email: 'Mario' sulla ultima lista, non l'id Gmail."""
    pdf_bytes = b"%PDF-1.4 da-mario"
    full = _full(
        "aaa",
        "Mario Rossi <mario@x.test>",
        "Fattura",
        plain="Pagare.",
        extra_parts=[
            _mime_part(
                "application/pdf",
                "fattura.pdf",
                size=len(pdf_bytes),
                attachment_id="pdf1",
            ),
        ],
    )
    session = MailboxSession()
    session.replace(
        [
            MailboxItem("aaa", "Mario Rossi", "Fattura", "Mario Rossi <mario@x.test>"),
            MailboxItem("bbb", "Anna", "Riunione", "Anna <anna@x.test>"),
        ],
        query="inbox",
        gmail_q="in:inbox",
    )
    result = save_attachments(
        "Mario",
        session=session,
        client=_mailbox_client_with_blobs({"aaa": full}, {("aaa", "pdf1"): pdf_bytes}),
        settings=_settings(tmp_path),
        credentials=_creds(),
        workspace=tmp_path,
        today=date(2026, 8, 19),
    )
    assert result == (
        f"OK: 1 allegato salvato in {EMAIL_ATTACHMENTS_DIRNAME}/2026-08-19: fattura.pdf."
    )
    dest = tmp_path / EMAIL_ATTACHMENTS_DIRNAME / "2026-08-19" / "fattura.pdf"
    assert dest.read_bytes() == pdf_bytes
    assert "aaa" not in result


def test_save_attachments_existing_name_gets_suffix(tmp_path: Path) -> None:
    """Omonimo già in email_attachments/oggi/ → fattura_2.pdf; il primo resta."""
    pdf_bytes = b"%PDF-1.4 nuovo"
    day_dir = tmp_path / EMAIL_ATTACHMENTS_DIRNAME / "2026-08-19"
    # Cartella del giorno già usata: mkdir del tool deve essere exist_ok.
    day_dir.mkdir(parents=True)
    (day_dir / "fattura.pdf").write_bytes(b"%PDF-1.4 vecchio")
    full = _full(
        "aaa",
        "Mario <mario@x.test>",
        "Fattura",
        plain="Pagare.",
        extra_parts=[
            _mime_part(
                "application/pdf",
                "fattura.pdf",
                size=len(pdf_bytes),
                attachment_id="pdf1",
            ),
        ],
    )
    session = MailboxSession()
    session.replace(
        [MailboxItem("aaa", "Mario", "Fattura", "Mario <mario@x.test>")],
        query="inbox",
        gmail_q="in:inbox",
    )
    result = save_attachments(
        "1",
        session=session,
        client=_mailbox_client_with_blobs({"aaa": full}, {("aaa", "pdf1"): pdf_bytes}),
        settings=_settings(tmp_path),
        credentials=_creds(),
        workspace=tmp_path,
        today=date(2026, 8, 19),
    )
    assert result == (
        f"OK: 1 allegato salvato in {EMAIL_ATTACHMENTS_DIRNAME}/2026-08-19: fattura_2.pdf."
    )
    # Il file preesistente non viene sovrascritto; il nuovo prende il suffisso _2.
    assert (day_dir / "fattura.pdf").read_bytes() == b"%PDF-1.4 vecchio"
    assert (day_dir / "fattura_2.pdf").read_bytes() == pdf_bytes


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


@pytest.mark.parametrize(
    ("status", "phrase"),
    [
        (400, "Ha rifiutato la richiesta, riprova"),
        (429, "Gmail è occupata, riprova tra poco"),
        (500, "Gmail non raggiungibile, riprova più tardi"),
        (503, "Gmail non raggiungibile, riprova più tardi"),
        (409, "Richiesta non riuscita, riprova più tardi"),
    ],
)
def test_list_emails_http_error_speaks_code_and_italian_phrase(
    tmp_path: Path,
    status: int,
    phrase: str,
) -> None:
    """GET mailbox >= 400 (non 401/403/404): Gmail HTTP {code} più frase."""
    # JSON error Gmail: deve restare fuori dal TTS, si parla solo codice+frase.
    gmail_json = {"error": {"code": status, "message": "Invalid q parameter"}}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=gmail_json)

    result = list_emails(
        "inbox",
        session=MailboxSession(),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        settings=_settings(tmp_path),
        credentials=_creds(),
    )
    assert result == f"ERRORE: Gmail HTTP {status}. {phrase}"
    assert "Invalid q parameter" not in result


def test_read_email_http_400_speaks_code_and_request_phrase(tmp_path: Path) -> None:
    """GET dettaglio 400: codice più «ha rifiutato la richiesta», non i destinatari."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": 400, "message": "Invalid userId"}},
        )

    session = MailboxSession()
    session.replace(
        [MailboxItem("aaa", "Mario Rossi", "Fattura", "Mario Rossi <mario@x.test>")],
        query="inbox",
        gmail_q="in:inbox",
    )
    result = read_email(
        "1",
        session=session,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        settings=_settings(tmp_path),
        credentials=_creds(),
    )
    assert result == (
        "ERRORE: Gmail HTTP 400. Ha rifiutato la richiesta, riprova"
    )
    assert "Invalid userId" not in result
    assert "destinatari" not in result


def test_read_email_http_404_stays_gone(tmp_path: Path) -> None:
    """GET dettaglio 404: email sparita, niente Gmail HTTP {code} nel TTS."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found"}})

    session = MailboxSession()
    session.replace(
        [MailboxItem("aaa", "Mario Rossi", "Fattura", "Mario Rossi <mario@x.test>")],
        query="inbox",
        gmail_q="in:inbox",
    )
    result = read_email(
        "1",
        session=session,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        settings=_settings(tmp_path),
        credentials=_creds(),
    )
    assert result == MSG_GONE
    assert "Gmail HTTP 404" not in result
    assert "Not Found" not in result


def test_gmail_prompt_and_catalog_include_save_attachments() -> None:
    """Prompt + declaration Gemini: save_attachments, stessa chiave name, nessun JSON-in-testo."""
    prompt = GMAIL_LOOP_SPEC.system_prompt
    by_name = {item.name: item for item in GMAIL_TOOL_DECLARATIONS}
    assert "save_attachments" in by_name
    assert "read_email" in by_name
    assert "draft_email" in by_name
    assert "reply_email" in by_name
    # Omogeneità name con read_email, niente placeholder <...>.
    save_props = by_name["save_attachments"].parameters["properties"]
    read_props = by_name["read_email"].parameters["properties"]
    assert "name" in save_props
    assert "name" in read_props
    assert "<" not in save_props["name"]["description"]
    assert "scarica gli allegati della prima" in prompt
    assert "mai in automatico" in prompt
    # Contratto nativo: gli schemi non stanno più come JSON {"tool":...} nel prompt.
    assert '{"tool": "save_attachments"' not in prompt
    assert GMAIL_LOOP_SPEC.gemini_tools is not None
    decls = GMAIL_LOOP_SPEC.gemini_tools[0]["functionDeclarations"]
    assert any(item["name"] == "save_attachments" for item in decls)


def test_dispatch_save_attachments_empty_name() -> None:
    """name assente o vuoto: stesso errore parlante di read_email, niente HTTP."""
    assert dispatch_gmail_tool("save_attachments", {}) == MSG_EMPTY_NAME
    assert dispatch_gmail_tool("save_attachments", {"name": ""}) == MSG_EMPTY_NAME
    assert dispatch_gmail_tool("save_attachments", {"name": "   "}) == MSG_EMPTY_NAME


def test_dispatch_unknown_tool_is_spoken_error() -> None:
    """Whitelist Gmail: i tool FS del master non devono partire né toccare HTTP."""
    result = dispatch_gmail_tool("create_text_file", {"name": "spesa", "content": "latte"})
    assert result.startswith("ERRORE:")
    assert "list_emails" in result
    assert "read_email" in result
    assert "save_attachments" in result
    assert "draft_email" in result
    assert "reply_email" in result
    assert "reply_all_email" in result
    assert "send_email" in result


def test_dispatch_save_attachments_writes_under_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON save_attachments name=1: resolve sessione + write, niente notes/inbox."""
    monkeypatch.setattr(read_mod, "WORKSPACE_ROOT", tmp_path)
    pdf_bytes = b"%PDF-1.4 dispatch"
    full = _full(
        "aaa",
        "Mario Rossi <mario@x.test>",
        "Fattura",
        plain="Pagare.",
        extra_parts=[
            _mime_part(
                "application/pdf",
                "fattura.pdf",
                size=len(pdf_bytes),
                attachment_id="pdf1",
            ),
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/attachments/" in path:
            return httpx.Response(
                200,
                json={"size": len(pdf_bytes), "data": _b64url_bytes(pdf_bytes)},
            )
        if path.endswith("/aaa"):
            return httpx.Response(200, json=full)
        return httpx.Response(404, json={})

    _patch_gmail_http(monkeypatch, handler)
    get_mailbox_session().replace(
        [MailboxItem("aaa", "Mario Rossi", "Fattura", "Mario Rossi <mario@x.test>")],
        query="inbox",
        gmail_q="in:inbox",
    )
    result = dispatch_gmail_tool("save_attachments", {"name": "1"})
    assert result.startswith(f"OK: 1 allegato salvato in {EMAIL_ATTACHMENTS_DIRNAME}/")
    assert "fattura.pdf" in result
    written = list((tmp_path / EMAIL_ATTACHMENTS_DIRNAME).rglob("fattura.pdf"))
    assert len(written) == 1
    assert written[0].read_bytes() == pdf_bytes
    assert not (tmp_path / "notes").exists()
    assert not (tmp_path / "inbox").exists()


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
    """LLM fake: coda di LlmTurn, zero rete. Come test_audio_bridge, ma per Gmail."""

    def __init__(self, replies: list[LlmTurn]) -> None:
        self.last_usage = TokenUsage()
        self._replies = list(replies)
        self.calls = 0

    def chat(
        self,
        messages: list[dict[str, Any]],
        *args: object,
        **kwargs: object,
    ) -> LlmTurn:
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
            LlmTurn(
                function_calls=(
                    FunctionCall(name="list_emails", args={"query": "inbox"}),
                ),
            ),
            LlmTurn(text="Hai due email: da Mario, fattura, e da Anna, riunione."),
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
    assert "Agente Gmail: posso elencare" in spoken
    assert "Assistente file sul Desktop" not in spoken
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
            LlmTurn(
                function_calls=(
                    FunctionCall(name="list_emails", args={"query": "inbox"}),
                ),
            ),
            LlmTurn(text="Hai due email in inbox."),
            LlmTurn(
                function_calls=(
                    FunctionCall(name="read_email", args={"name": "la prima"}),
                ),
            ),
            LlmTurn(text="Mario chiede di pagare entro venerdì."),
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


def test_gmail_loop_list_then_save_attachments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Due turni vocali: elenco poi 'scarica gli allegati della prima'. Mock, zero Google."""
    # Dispatch non inietta workspace: il write deve finire sotto tmp_path, non il Desktop.
    monkeypatch.setattr(read_mod, "WORKSPACE_ROOT", tmp_path)
    pdf_bytes = b"%PDF-1.4 loop-vocale"
    full = _full(
        "aaa",
        "Mario Rossi <mario@x.test>",
        "Fattura",
        plain="Pagare entro venerdì.",
        extra_parts=[
            _mime_part(
                "application/pdf",
                "fattura.pdf",
                size=len(pdf_bytes),
                attachment_id="pdf1",
            ),
            _mime_part(
                "image/png",
                "logo.png",
                size=1_200,
                attachment_id="logo1",
                disposition="inline",
                content_id="<logo@x.test>",
            ),
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        # Token iniettato da _patch_gmail_http: se manca, il loop sta usando altro path.
        assert request.headers.get("Authorization") == f"Bearer {_ACCESS}"
        path = request.url.path
        # Binario: solo il PDF vero; il logo inline non deve arrivare qui.
        if "/attachments/" in path:
            assert path.endswith("/attachments/pdf1"), path
            return httpx.Response(
                200,
                json={"size": len(pdf_bytes), "data": _b64url_bytes(pdf_bytes)},
            )
        if path.rstrip("/").endswith("/users/me/messages"):
            assert _query_of(request).get("q") == ["in:inbox"]
            return httpx.Response(200, json={"messages": [{"id": "aaa"}]})
        if path.endswith("/aaa"):
            return httpx.Response(200, json=full)
        return httpx.Response(404, json={})

    captured = _patch_gmail_http(monkeypatch, handler)
    llm = _ScriptedLLM(
        [
            LlmTurn(
                function_calls=(
                    FunctionCall(name="list_emails", args={"query": "inbox"}),
                ),
            ),
            LlmTurn(text="Hai una email da Mario, fattura."),
            LlmTurn(
                function_calls=(
                    FunctionCall(name="save_attachments", args={"name": "1"}),
                ),
            ),
            LlmTurn(
                text="Ho salvato fattura.pdf nella cartella degli allegati di oggi.",
            ),
        ]
    )
    outfile = StringIO()
    code = run_chat_loop(
        MockSTT(
            infile=StringIO("ultime email\nscarica gli allegati della prima\nesci\n"),
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
    # TTS: sintesi none; il path Desktop e l'id Gmail non devono uscire a voce.
    assert "Hai una email da Mario, fattura." in spoken
    assert "Ho salvato fattura.pdf nella cartella degli allegati di oggi." in spoken
    assert "aaa" not in spoken
    assert llm.calls == 4
    stdout = capsys.readouterr().out
    assert "[GMAIL] OK: 1 email in inbox." in stdout
    assert f"[GMAIL] OK: 1 allegato salvato in {EMAIL_ATTACHMENTS_DIRNAME}/" in stdout
    assert "fattura.pdf" in stdout
    assert "logo.png" not in stdout
    written = list((tmp_path / EMAIL_ATTACHMENTS_DIRNAME).rglob("fattura.pdf"))
    assert len(written) == 1
    assert written[0].read_bytes() == pdf_bytes
    assert not list((tmp_path / EMAIL_ATTACHMENTS_DIRNAME).rglob("logo.png"))
    assert not (tmp_path / "notes").exists()
    assert not (tmp_path / "inbox").exists()
    _assert_mocked_gmail_only(captured)
