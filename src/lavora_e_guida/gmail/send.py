"""Invio Gmail: bozza in sessione Python e REST send solo dopo HITL.

`draft_email` non tocca la rete: memorizza destinatario/oggetto/corpo.
Il `to` può essere un `@` esplicito oppure un pezzo parlato (cognome,
nome+cognome, indice): in quel caso Python risolve From/Reply-To sulla
ultima lista, senza People API e senza far spellingare l'indirizzo a Gemini.
`reply_email` è lo stesso gate HITL, ma sul thread: Gemini passa solo
`name` e `body`; Python riempie destinatario (Reply-To o From), oggetto
`Re:`, `threadId` e gli header RFC `In-Reply-To` / `References`.
`reply_all_email` è un tool distinto (niente flag `all` su reply): To+Cc
della riga, senza `GMAIL_USER`. `send_email` chiama `users/me/messages/send`
solo se la sessione ha una bozza e la conferma vocale è già avvenuta
(interceptor sì/no nel loop). Mai un browser: token o scope insufficienti
→ GmailAuthError già parlante.

`gmail.modify` (marca-letto/archivio) è fuori scope: altro piano.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from email.mime.text import MIMEText

import httpx
from google.oauth2.credentials import Credentials

from lavora_e_guida.config import Settings, get_settings
from lavora_e_guida.gmail.oauth import (
    GMAIL_SCOPES,
    MSG_GMAIL_NOT_LINKED,
    MSG_PROFILE_UNREACHABLE,
    GmailAuthError,
    _spoken_http_status,
    get_gmail_credentials,
)
from lavora_e_guida.gmail.read import (
    MSG_EMPTY_NAME,
    GmailReadError,
    MailboxItem,
    MailboxSession,
    get_mailbox_session,
    resolve_listed_item,
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
# Riga in lista sì, ma From/Reply-To assenti o non parsabili: non inventiamo un @.
MSG_NO_LISTED_ADDRESS = "ERRORE: nessun indirizzo nella email elencata"
# Reply-all: dopo aver tolto me, To è vuoto (es. ero l'unico e il From non parsabile).
MSG_NO_REPLY_ALL_RECIPIENTS = "ERRORE: nessun destinatario per rispondere a tutti"
MSG_NO_DRAFT = "ERRORE: nessuna bozza da inviare, prepara prima l'email"
MSG_NEED_CONFIRM = "ERRORE: conferma prima l'invio dicendo sì oppure no"


@dataclass
class EmailDraft:
    """Bozza in-process: i campi parlabili, niente MIME finché non si invia.

    I tre campi threading restano vuoti sul compose nuovo (`draft_email`).
    Su `reply_email` / `reply_all_email` Python li copia dalla riga in
    sessione: `threadId` Gmail e Message-ID RFC, mai parlati in lista/lettura.
    `cc` è pieno solo sul reply-all; compose e reply al mittente lo lasciano
    vuoto così il MIME non mette l'header Cc.
    """

    to: str = ""
    subject: str = ""
    body: str = ""
    # threadId Gmail (JSON top-level del POST send); vuoto = compose nuovo.
    thread_id: str = ""
    # Header In-Reply-To: Message-ID della mail a cui si risponde.
    in_reply_to: str = ""
    # Header References: catena precedente più quel Message-ID.
    references: str = ""
    # Header Cc: virgola-separati; vuoto = niente Cc sul MIME (compose/reply).
    cc: str = ""


@dataclass
class DraftSession:
    """Stato tra draft/reply e la conferma vocale (HITL) / send_email.

    `awaiting_confirm` è True dopo una bozza OK: il loop intercetta sì/no
    senza passare da Gemini. `confirmed` è True solo dopo un sì esplicito;
    send_email rifiuta se manca.
    """

    draft: EmailDraft | None = None
    awaiting_confirm: bool = False
    confirmed: bool = False

    def set_draft(
        self,
        to: str,
        subject: str,
        body: str,
        *,
        thread_id: str = "",
        in_reply_to: str = "",
        references: str = "",
        cc: str = "",
    ) -> None:
        """Sostituisce la bozza e riapre l'attesa HITL (un enunciato = un draft).

        Compose nuovo: i default azzerano threading e Cc, anche se prima c'era
        una reply-all in sessione. Reply al mittente: threadId/RFC, Cc vuoto.
        Reply-all: il chiamante passa anche `cc` (già senza GMAIL_USER).
        """
        self.draft = EmailDraft(
            to=to,
            subject=subject,
            body=body,
            thread_id=thread_id,
            in_reply_to=in_reply_to,
            references=references,
            cc=cc,
        )
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


def _split_header_addresses(raw: str) -> list[str]:
    """Spezza To/Cc di sessione (già bare-@, virgola-separati) in lista stabile.

    Non usa parseaddr: in bozza non c'è il display name, solo gli indirizzi
    che Python ha già filtrato. Parti vuote (virgola doppia) si scartano.
    """
    # Strip su ogni pezzo: lo join MIME mette ", " e HITL non deve leggere spazi.
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def _join_header_addresses(addresses: list[str]) -> str:
    """To/Cc MIME e sessione: stesso ordine, virgola-spazio, niente display name."""
    return ", ".join(addresses)


def _spoken_recipient_clause(addresses: list[str]) -> str:
    """Frasi ordinali dopo «un'email a»: un @, due con «e a», tre+ con virgole.

    Niente etichette `A:` / `Cc:`: To e Cc si fondono in un'unica lista parlata.
    Un solo destinatario (compose, reply, reply-all degenere) resta `addr`
    così i test HITL a un @ non cambiano. Side-effect: nessuno.
    """
    if not addresses:
        return ""
    if len(addresses) == 1:
        return addresses[0]
    # Dal secondo in poi il TTS sente «a» davanti all'@, come nell'esempio del piano.
    *rest, last = addresses
    headed = [rest[0]] + [f"a {item}" for item in rest[1:]]
    return f"{', '.join(headed)} e a {last}"


def spoken_draft_confirm(*, to: str, subject: str, body: str, cc: str = "") -> str:
    """Frase TTS di conferma HITL: destinatari, oggetto, corpo, poi sì/no.

    Un solo testo per `draft_email` / `reply_email` / `reply_all_email`
    (con prefisso `OK:`) e per il retry se l'utente non dice sì/no: così la
    seconda richiesta ha ancora il corpo. Con più To o un Cc, gli `@` vanno
    in frasi ordinali (regola `SPOKEN_REPLY_RULE`): niente markdown, niente
    etichette `A:` / `Cc:`. L'`@` si sente solo qui, non nella reply Gemini.
    Side-effect: nessuno.
    """
    # To poi Cc, stesso ordine del MIME: l'utente sente tutti prima del sì.
    recipients = _split_header_addresses(to) + _split_header_addresses(cc)
    spoken_to = _spoken_recipient_clause(recipients) or to
    # «oggetto {subject}» è una pausa in frase, non l'etichetta da elenco.
    # Il corpo va dopo «Il testo è:» così l'utente sente cosa sta per partire.
    return (
        f"ho preparato un'email a {spoken_to}, oggetto {subject}. "
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


def _recipient_from_listed_item(item: MailboxItem) -> str:
    """Indirizzo da mettere in bozza: Reply-To se usabile, altrimenti From.

    Gemini passa solo il pezzo parlato (Rossi, Mario Rossi, 1); l'@ lo
    riempie Python dalla riga già in sessione. Preferiamo Reply-To perché
    è lì che il mittente vuole le risposte; se manca o non passa `_valid_to`,
    scendiamo sul From. Vuoto = la riga non ha un destinatario parsabile.
    """
    # Due candidati, stesso check vocale dell'indirizzo esplicito: niente RFC.
    for candidate in (item.reply_to_address, item.from_address):
        dest = (candidate or "").strip()
        # Un header presente ma non-email (es. solo display name) non deve
        # mascherare l'altro: se Reply-To è spazzatura, il From può salvarci.
        if dest and _valid_to(dest):
            return dest
    return ""


def _resolve_draft_to(spoken: str, mailbox: MailboxSession) -> str:
    """Cognome, nome+cognome o indice → @ della riga in sessione.

    Stesso RapidFuzz di `read_email` (WRatio, soglia 70) su sender /
    from_header / oggetto / indice. Side-effect: nessuno. Alza
    `GmailReadError` se manca la lista o il match (messaggi già parlanti).
    Ritorna stringa vuota se la riga non ha From/Reply-To parsabile: il
    chiamante parla `MSG_NO_LISTED_ADDRESS`, non inventa un dominio.
    """
    # resolve_listed_item pulisce stopword e prova indice prima del fuzzy.
    item = resolve_listed_item(spoken, mailbox)
    return _recipient_from_listed_item(item)


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


def _reply_subject(original: str) -> str:
    """Oggetto di risposta: prefisso Re: se manca, senza duplicarlo.

    Gemini non manda `subject` su `reply_email`: lo deriva Python dalla
    riga in sessione. casefold così `RE:` / `Re:` / `re:` restano intatti.
    """
    stripped = original.strip()
    # Già una reply RFC: non accatastiamo un secondo Re:.
    if stripped.casefold().startswith("re:"):
        return stripped
    if stripped:
        return f"Re: {stripped}"
    # Oggetto vuoto sulla mail originale: restiamo parlabili, non inventiamo.
    return "Re:"


def _reply_references(*, existing: str, message_id: str) -> str:
    """Catena RFC References: precedente più il Message-ID a cui si risponde.

    Vuoto + id → solo id. Catena senza id (riga senza Message-ID) → la
    catena così com'è. Entrambi vuoti → stringa vuota, niente header.
    """
    existing = existing.strip()
    message_id = message_id.strip()
    if existing and message_id:
        return f"{existing} {message_id}"
    return existing or message_id


def _rfc822_raw(
    *,
    to: str,
    subject: str,
    body: str,
    from_addr: str,
    in_reply_to: str = "",
    references: str = "",
    cc: str = "",
) -> str:
    """MIME testo UTF-8 → raw urlsafe-base64 (padding rimosso, contratto Gmail).

    Compose nuovo: solo To/Subject/From. Reply al mittente: threading RFC.
    Reply-all: anche header Cc se `cc` non è vuoto. threadId resta nel body
    REST, non nel MIME.
    """
    # MIMEText imposta Content-Type text/plain; From è la mailbox autenticata.
    message = MIMEText(body, "plain", "utf-8")
    message["To"] = to
    # Cc solo se Python ha lasciato qualcuno dopo aver tolto me e i To.
    if cc.strip():
        message["Cc"] = cc.strip()
    message["Subject"] = subject
    if from_addr:
        message["From"] = from_addr
    # Angle brackets del Message-ID restano così: Gmail li ha già in sessione.
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if references:
        message["References"] = references
    encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    return encoded.rstrip("=")


def draft_email(
    to: object = "",
    subject: object = "",
    body: object = "",
    *,
    session: DraftSession | None = None,
    mailbox: MailboxSession | None = None,
) -> str:
    """Memorizza la bozza; ritorna OK: parlante. Nessuna REST.

    `to` già valido con `@` resta tale (dettato o copiato). Altrimenti è
    un pezzo parlato: Python lo risolve sulla ultima lista (From / Reply-To)
    e in bozza salva solo l'indirizzo, così l'HITL lo dice una volta sola.
    Side-effect: `session.set_draft`. Il loop dopo questo tool fa TTS e
    attende sì/no: Gemini non deve chiamare send nello stesso enunciato.
    """
    dest = _as_text(to)
    subj = _as_text(subject)
    text = _as_text(body)
    if not dest:
        return MSG_EMPTY_TO
    # Indirizzo esplicito: niente fuzzy, anche se in lista c'è un omonimo.
    if not _valid_to(dest):
        # Default = sessione globale del processo (stessa di list/read).
        box = mailbox if mailbox is not None else get_mailbox_session()
        try:
            # Cognome / nome+cognome / indice: stesso resolver della lettura.
            dest = _resolve_draft_to(dest, box)
        except GmailReadError as exc:
            # Lista assente, elenco vuoto, nessun match: già prefissati ERRORE:.
            return _spoken_error(exc)
        if not dest:
            # Riga trovata ma From/Reply-To non parsabili: non inventiamo un @.
            return MSG_NO_LISTED_ADDRESS
    if not subj:
        return MSG_EMPTY_SUBJECT
    if not text:
        return MSG_EMPTY_BODY
    store = session if session is not None else get_draft_session()
    # In sessione solo l'@ risolto: HITL riusa spoken_draft_confirm invariato.
    store.set_draft(dest, subj, text)
    # OK: il loop HITL toglie il prefisso e parla la stessa frase del retry.
    return "OK: " + spoken_draft_confirm(to=dest, subject=subj, body=text)



def _gmail_user_address(settings: Settings | None) -> str:
    """Mailbox autenticata da Settings (test) o singleton env (runtime).

    Serve al reply-all per togliere me da To/Cc (confronto casefold).
    Vuoto = non filtriamo: in lab GMAIL_USER c'è quasi sempre.
    """
    # Iniezione nei test; nel loop vocale dispatch non passa settings.
    cfg = settings if settings is not None else get_settings()
    return (cfg.gmail_user or "").strip()


def _is_self_address(address: str, me: str) -> bool:
    """True se `address` è GMAIL_USER, ignorando maiuscole/spazi."""
    # casefold: Tester@Gmail.com e tester@gmail.com sono la stessa mailbox.
    return bool(me) and address.strip().casefold() == me.strip().casefold()


def _unique_valid_addresses(
    candidates: list[str],
    *,
    me: str,
    exclude: set[str] | None = None,
) -> list[str]:
    """Dedup casefold, ordine stabile: niente me, niente già visti, solo @ validi.

    `exclude` è un set di indirizzi già casefoldati (es. chi è già in To
    non deve ricomparire in Cc). Side-effect: nessuno.
    """
    seen = set(exclude or ())
    out: list[str] = []
    for raw in candidates:
        addr = (raw or "").strip()
        if not addr or not _valid_to(addr):
            continue
        fold = addr.casefold()
        # Me e i duplicati escono: l'ordine della prima occorrenza resta.
        if _is_self_address(addr, me) or fold in seen:
            continue
        seen.add(fold)
        out.append(addr)
    return out


def _reply_all_recipients(
    item: MailboxItem, me: str
) -> tuple[list[str], list[str]]:
    """To e Cc del reply-all: mittente + To originali, poi Cc, senza me.

    To = (Reply-To se valido, senno From) + To della riga, unici, senza me.
    Cc = Cc della riga, senza me e senza chi è già in To. Ordine stabile.
    """
    # Stesso mittente del reply semplice: Reply-To vince sul From se parsabile.
    sender = _recipient_from_listed_item(item)
    to_candidates = ([sender] if sender else []) + list(item.to_addresses)
    to_list = _unique_valid_addresses(to_candidates, me=me)
    # Chi è già in To non va anche in Cc, nemmeno con casing diverso.
    to_folds = {addr.casefold() for addr in to_list}
    cc_list = _unique_valid_addresses(
        list(item.cc_addresses),
        me=me,
        exclude=to_folds,
    )
    return to_list, cc_list


def _store_threaded_draft(
    store: DraftSession,
    item: MailboxItem,
    *,
    to: str,
    body: str,
    cc: str = "",
) -> str:
    """Oggetto Re:, threadId, RFC e HITL. Side-effect: `store.set_draft`.

    Condiviso da reply_email (cc vuoto) e reply_all_email (To+Cc). Ritorna
    la stringa OK: con la frase parlata, Cc incluso se c'è.
    """
    subj = _reply_subject(item.subject)
    in_reply_to = (item.rfc_message_id or "").strip()
    references = _reply_references(
        existing=item.rfc_references or "",
        message_id=in_reply_to,
    )
    store.set_draft(
        to,
        subj,
        body,
        thread_id=(item.thread_id or "").strip(),
        in_reply_to=in_reply_to,
        references=references,
        cc=cc,
    )
    return "OK: " + spoken_draft_confirm(to=to, subject=subj, body=body, cc=cc)


def reply_email(
    name: object = "",
    body: object = "",
    *,
    session: DraftSession | None = None,
    mailbox: MailboxSession | None = None,
) -> str:
    """Bozza in-reply al solo mittente della riga elencata; niente REST.

    Gemini passa `name` parlato (cognome, nome+cognome, indice) e `body`.
    Non manda `to` né `subject`: Python li deriva da Reply-To/From e
    dall'oggetto originale (`Re:` se manca). Threading: threadId Gmail,
    In-Reply-To = Message-ID, References = catena esistente + quel id.
    Stesso HITL di `draft_email`. Side-effect: `session.set_draft`.
    Reply-all (To+Cc) è il tool `reply_all_email`, non un flag su questo.
    """
    spoken_name = _as_text(name)
    text = _as_text(body)
    if not spoken_name:
        return MSG_EMPTY_NAME
    if not text:
        return MSG_EMPTY_BODY
    # Default = stessa mailbox di list/read: senza elenco è errore parlante.
    box = mailbox if mailbox is not None else get_mailbox_session()
    try:
        # Stesso RapidFuzz di read_email (WRatio, soglia 70).
        item = resolve_listed_item(spoken_name, box)
    except GmailReadError as exc:
        return _spoken_error(exc)
    dest = _recipient_from_listed_item(item)
    if not dest:
        # Riga sì, @ no: non inventiamo un dominio per chiudere il thread.
        return MSG_NO_LISTED_ADDRESS
    store = session if session is not None else get_draft_session()
    # Cc resta vuoto: questo tool è solo mittente, reply-all è un altro nome.
    return _store_threaded_draft(store, item, to=dest, body=text)



def reply_all_email(
    name: object = "",
    body: object = "",
    *,
    session: DraftSession | None = None,
    mailbox: MailboxSession | None = None,
    settings: Settings | None = None,
) -> str:
    """Bozza in-reply a To+Cc della riga elencata, senza GMAIL_USER; niente REST.

    Stesso `name` e `body` di `reply_email` (cognome, nome+cognome, indice).
    Python riempie To = (Reply-To o From) + To originali senza me, Cc =
    Cc originali senza me e senza chi è già in To. Se To resta vuoto,
    errore parlante (niente bozza). Un solo @ restante è reply-all degenere:
    si prepara comunque, HITL dice un solo indirizzo.
    Side-effect: `session.set_draft` con threading come la reply al mittente.
    """
    spoken_name = _as_text(name)
    text_body = _as_text(body)
    if not spoken_name:
        return MSG_EMPTY_NAME
    if not text_body:
        return MSG_EMPTY_BODY
    # Stessa mailbox di list/read: senza elenco non c'è a chi rispondere.
    box = mailbox if mailbox is not None else get_mailbox_session()
    try:
        # Stesso RapidFuzz di reply_email / read_email (WRatio, soglia 70).
        item = resolve_listed_item(spoken_name, box)
    except GmailReadError as exc:
        return _spoken_error(exc)
    me = _gmail_user_address(settings)
    to_list, cc_list = _reply_all_recipients(item, me)
    if not to_list:
        # Ero l'unico in To e il From non è parsabile (o sono io): niente invio.
        return MSG_NO_REPLY_ALL_RECIPIENTS
    store = session if session is not None else get_draft_session()
    to_header = _join_header_addresses(to_list)
    cc_header = _join_header_addresses(cc_list)
    # HITL elenca To e Cc in frasi ordinali; l'@ si sente solo qui.
    return _store_threaded_draft(
        store,
        item,
        to=to_header,
        body=text_body,
        cc=cc_header,
    )


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
            in_reply_to=draft.in_reply_to,
            references=draft.references,
            cc=draft.cc,
        )
        # Compose nuovo: solo raw. Reply: Gmail raggruppa col threadId.
        payload: dict[str, str] = {"raw": raw}
        if draft.thread_id:
            payload["threadId"] = draft.thread_id
        try:
            response = http.post(
                GMAIL_SEND_URL,
                headers=_auth_headers(creds),
                json=payload,
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
            # Bozza resta: l'utente può ritentare il sì dopo aver corretto.
            store.awaiting_confirm = True
            store.confirmed = False
            return _spoken_http_status(response.status_code, "send")
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
