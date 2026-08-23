"""Invio Gmail: bozza in sessione Python e REST send solo dopo HITL.

`draft_email` non tocca la rete: memorizza destinatario/oggetto/corpo.
`send_email` chiama `users/me/messages/send` solo se la sessione ha una
bozza e la conferma vocale è già avvenuta (interceptor sì/no nel loop).
Mai un browser: token o scope insufficienti → GmailAuthError già parlante.

`gmail.modify` (marca-letto/archivio) è fuori scope: altro piano.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from email.mime.text import MIMEText

import httpx
from google.oauth2.credentials import Credentials

from lavora_e_guida.config import Settings
from lavora_e_guida.gmail.oauth import (
    GMAIL_SCOPES,
    MSG_GMAIL_NOT_LINKED,
    MSG_PROFILE_UNREACHABLE,
    GmailAuthError,
    get_gmail_credentials,
)

# Stesso host della lettura: POST send, niente google-api-python-client.
GMAIL_SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

# Timeout unico: WSL può essere lento, ma l'invio non deve appendere in auto.
_HTTP_TIMEOUT = 30.0

# Messaggi TTS: prefisso OK/ERRORE, niente RFC822 né id Gmail nel parlato.
MSG_EMPTY_TO = "ERRORE: indica a chi inviare l'email"
MSG_EMPTY_SUBJECT = "ERRORE: indica l'oggetto dell'email"
MSG_EMPTY_BODY = "ERRORE: indica il testo dell'email"
MSG_BAD_TO = "ERRORE: indirizzo destinatario non valido"
MSG_NO_DRAFT = "ERRORE: nessuna bozza da inviare, prepara prima l'email"
MSG_NEED_CONFIRM = "ERRORE: conferma prima l'invio dicendo sì oppure no"


@dataclass
class EmailDraft:
    """Bozza in-process: i campi parlabili, niente MIME finché non si invia."""

    to: str = ""
    subject: str = ""
    body: str = ""


@dataclass
class DraftSession:
    """Stato tra draft_email e la conferma vocale (HITL) / send_email.

    `awaiting_confirm` è True dopo una bozza OK: il loop intercetta sì/no
    senza passare da Gemini. `confirmed` è True solo dopo un sì esplicito;
    send_email rifiuta se manca.
    """

    draft: EmailDraft | None = None
    awaiting_confirm: bool = False
    confirmed: bool = False

    def set_draft(self, to: str, subject: str, body: str) -> None:
        """Sostituisce la bozza e riapre l'attesa HITL (un enunciato = un draft)."""
        self.draft = EmailDraft(to=to, subject=subject, body=body)
        self.awaiting_confirm = True
        self.confirmed = False

    def mark_confirmed(self) -> None:
        """Il sì vocale: da qui send_email può colpire la REST."""
        self.confirmed = True
        self.awaiting_confirm = False

    def clear(self) -> None:
        """Annulla o post-send: niente secondo invio accidentale."""
        self.draft = None
        self.awaiting_confirm = False
        self.confirmed = False


# Sessione di default del processo: il loop vocale non passa lo spec a ogni tool.
_DRAFT_SESSION = DraftSession()


def get_draft_session() -> DraftSession:
    """Sessione globale del processo (i test la resettano tra un caso e l'altro)."""
    return _DRAFT_SESSION


def reset_draft_session() -> None:
    """Svuota la bozza globale: utile nei test e dopo un invio/annullo."""
    _DRAFT_SESSION.clear()


def spoken_draft_confirm(*, to: str, subject: str, body: str) -> str:
    """Frase TTS di conferma HITL: destinatario, oggetto, corpo, poi sì/no.

    Un solo testo per `draft_email` (con prefisso `OK:`) e per il retry se
    l'utente non dice sì/no: così la seconda richiesta ha ancora il corpo.
    Niente etichette `Oggetto:` né markdown: edge-tts legge questa stringa
    (regola `SPOKEN_REPLY_RULE`). Side-effect: nessuno.
    """
    # «oggetto {subject}» è una pausa in frase, non l'etichetta da elenco.
    # Il corpo va dopo «Il testo è:» così l'utente sente cosa sta per partire.
    return (
        f"ho preparato un'email a {to}, oggetto {subject}. "
        f"Il testo è: {body}. "
        "Di' sì per inviare o no per annullare."
    )


def _as_text(raw: object) -> str:
    """Normalizza un argomento Gemini a stringa strippata; None/bool → vuoto."""
    # bool è int: True non deve diventare "True" come destinatario.
    if raw is None or isinstance(raw, bool):
        return ""
    return str(raw).strip()


def _valid_to(address: str) -> bool:
    """Check minimale vocale: serve una @, niente RFC completo in macchina."""
    # Spazi nel mezzo (STT) li abbiamo già collassati in _as_text.
    if "@" not in address or address.startswith("@") or address.endswith("@"):
        return False
    local, _, domain = address.partition("@")
    return bool(local) and "." in domain


def _spoken_error(exc: BaseException) -> str:
    """Normalizza eccezioni a stringa ERRORE: (GmailAuthError è già prefissata)."""
    text = str(exc).strip()
    if text.startswith("ERRORE:"):
        return text
    return f"ERRORE: {text}"


def _load_credentials(
    *,
    credentials: Credentials | None,
    settings: Settings | None,
) -> Credentials:
    """Credenziali iniettate (test) oppure token su disco con GMAIL_SCOPES (readonly+send)."""
    if credentials is not None:
        return credentials
    # Default runtime = GMAIL_SCOPES: token solo-readonly → MSG_INSUFFICIENT_SCOPES.
    return get_gmail_credentials(settings=settings, scopes=GMAIL_SCOPES)


def _auth_headers(creds: Credentials) -> dict[str, str]:
    """Bearer dall'access token già rinfrescato; senza token → non collegata."""
    token = creds.token
    if not token:
        raise GmailAuthError(MSG_GMAIL_NOT_LINKED)
    return {"Authorization": f"Bearer {token}"}


def _rfc822_raw(*, to: str, subject: str, body: str, from_addr: str) -> str:
    """MIME testo UTF-8 → raw urlsafe-base64 (padding rimosso, contratto Gmail)."""
    # MIMEText imposta Content-Type text/plain; From è la mailbox autenticata.
    message = MIMEText(body, "plain", "utf-8")
    message["To"] = to
    message["Subject"] = subject
    if from_addr:
        message["From"] = from_addr
    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    return encoded.rstrip("=")


def draft_email(
    to: object = "",
    subject: object = "",
    body: object = "",
    *,
    session: DraftSession | None = None,
) -> str:
    """Memorizza la bozza; ritorna OK: parlante. Nessuna REST.

    Side-effect: `session.set_draft`. Il loop dopo questo tool fa TTS e
    attende sì/no: Gemini non deve chiamare send nello stesso enunciato.
    """
    dest = _as_text(to)
    subj = _as_text(subject)
    text = _as_text(body)
    if not dest:
        return MSG_EMPTY_TO
    if not _valid_to(dest):
        return MSG_BAD_TO
    if not subj:
        return MSG_EMPTY_SUBJECT
    if not text:
        return MSG_EMPTY_BODY
    store = session if session is not None else get_draft_session()
    store.set_draft(dest, subj, text)
    # OK: il loop HITL toglie il prefisso e parla la stessa frase del retry.
    return "OK: " + spoken_draft_confirm(to=dest, subject=subj, body=text)


def send_email(
    *,
    session: DraftSession | None = None,
    client: httpx.Client | None = None,
    settings: Settings | None = None,
    credentials: Credentials | None = None,
    from_addr: str | None = None,
) -> str:
    """Invia la bozza in sessione se `confirmed`; altrimenti ERRORE parlante.

    Side-effect: POST Gmail; `session.clear()` dopo un 2xx. Senza conferma
    HITL non parte la rete (anche se Gemini inventa send_email).
    """
    store = session if session is not None else get_draft_session()
    draft = store.draft
    if draft is None:
        return MSG_NO_DRAFT
    if not store.confirmed:
        # Il modello non può saltare il sì vocale: Python è il gate.
        return MSG_NEED_CONFIRM

    owns_client = client is None
    http = client
    try:
        try:
            creds = _load_credentials(credentials=credentials, settings=settings)
        except GmailAuthError as exc:
            return _spoken_error(exc)
        if http is None:
            http = httpx.Client(timeout=_HTTP_TIMEOUT)
        sender = (from_addr or "").strip()
        if not sender and settings is not None:
            sender = (settings.gmail_user or "").strip()
        raw = _rfc822_raw(
            to=draft.to,
            subject=draft.subject,
            body=draft.body,
            from_addr=sender,
        )
        try:
            response = http.post(
                GMAIL_SEND_URL,
                headers=_auth_headers(creds),
                json={"raw": raw},
            )
        except httpx.HTTPError:
            # Rete giù: stesso parlato della lettura, niente traceback httpx.
            store.awaiting_confirm = True
            store.confirmed = False
            return _spoken_error(GmailAuthError(MSG_PROFILE_UNREACHABLE))
        if response.status_code in (401, 403):
            # Invio fallito: la bozza resta, HITL può ritentare un sì.
            store.awaiting_confirm = True
            store.confirmed = False
            return MSG_GMAIL_NOT_LINKED
        if response.status_code >= 400:
            store.awaiting_confirm = True
            store.confirmed = False
            return f"ERRORE: Gmail HTTP {response.status_code}"
        # 2xx: togliamo la bozza così un secondo «invia» non duplica.
        dest = draft.to
        subj = draft.subject
        store.clear()
        return f"OK: email inviata a {dest}, oggetto {subj}."
    except GmailAuthError as exc:
        return _spoken_error(exc)
    finally:
        if owns_client and http is not None:
            http.close()
