"""Tool Gmail in sola lettura: elenco, corpo TTS e salvataggio allegati.

Gemini chiama `list_emails` / `read_email` / `save_attachments` via
function calling; questo modulo esegue la REST Gmail via httpx e parla
all'utente. Gli id messaggio non sono parlabili: restano nella
MailboxSession in-process (mappa 1..N). Mai un browser: token assente o
HTTP 401 → GmailAuthError già parlante.
`save_attachments` scrive sotto WORKSPACE_ROOT/email_attachments/YYYY-MM-DD/,
non in inbox/ (PDF importati a mano dallo specialista FS).
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email.header import decode_header, make_header
from email.utils import parseaddr
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx
from google.oauth2.credentials import Credentials
from rapidfuzz import fuzz, process

# Dirname unico: write Gmail e skip RAG/resolver condividono EMAIL_ATTACHMENTS_DIRNAME.
from lavora_e_guida.config import EMAIL_ATTACHMENTS_DIRNAME, WORKSPACE_ROOT, Settings
from lavora_e_guida.gmail.oauth import (
    MSG_GMAIL_NOT_LINKED,
    MSG_PROFILE_UNREACHABLE,
    GmailAuthError,
    get_gmail_credentials,
)

# Stesso host del profile OAuth: lista id + dettaglio messaggio, niente client Google.
GMAIL_MESSAGES_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages"

# Default vocale: poche email da leggere ad alta voce; cap per non saturare TTS.
DEFAULT_LIST_LIMIT = 5
MAX_LIST_LIMIT = 20

# Ramo count: page size massimo Gmail e tetto anti-append su caselle enormi.
# Oltre COUNT_CAP non si segue più nextPageToken: la reply dice "almeno N".
COUNT_PAGE_SIZE = 500
COUNT_CAP = 2000

# Corpo per Gemini: più largo del tetto 3B; il modello riassume in reply.
MAX_BODY_CHARS = 1500

# Tetto a file: oltre non si scarica, gli altri allegati della stessa mail sì.
MAX_ATTACHMENT_BYTES = 15 * 1024 * 1024

# Zero file veri (solo logo/firma) o tutti saltati senza write: stessa formula.
MSG_NO_ATTACHMENTS = "OK: nessun allegato."

# Caratteri vietati sui nomi file Windows (NTFS + Desktop montato in WSL).
_WIN_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Device reserved Windows: CON.txt sul Desktop rompe Explorer, non il TTS.
_WIN_RESERVED_STEMS: frozenset[str] = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)

# Fallback se il filename Gmail è vuoto o diventa vuoto dopo la sanitizzazione.
_FALLBACK_ATTACHMENT_NAME = "allegato"

# Tetto sul basename: lascia spazio a `_2` e al path `email_attachments/YYYY-MM-DD/`.
_MAX_ATTACHMENT_FILENAME_LEN = 200

# Stesso ordine di grandezza del resolver FS: sotto soglia → niente match azzardato.
_FUZZY_THRESHOLD = 70

# Timeout unico sulle GET mailbox (WSL può essere lento, ma non deve appendere).
_HTTP_TIMEOUT = 30.0

# Messaggi TTS: prefisso ERRORE, niente stacktrace né id Gmail.
MSG_LIST_FIRST = "ERRORE: prima elenca le email"
MSG_EMPTY_LIST = "ERRORE: nessuna email in elenco"
MSG_NOT_IN_LIST = "ERRORE: email non trovata nella lista"
MSG_EMPTY_NAME = "ERRORE: indica quale email leggere"
MSG_EMPTY_BODY = "ERRORE: email senza testo da leggere"
MSG_GONE = "ERRORE: email non più disponibile"

# Query intere che l'utente/modello usano per “mostra la posta”, non per cercare la parola.
_INBOX_EXACT: frozenset[str] = frozenset(
    {
        "inbox",
        "posta",
        "posta in arrivo",
        "casella",
        "in arrivo",
        "ultime",
        "ultime email",
        "ultime mail",
        "ultime e-mail",
        "email",
        "mail",
        "e-mail",
    }
)

# Frasi italiane → is:unread; applicate prima del mapping `da X` (da leggere ≠ da Mario).
_UNREAD_RE = re.compile(
    r"\b(?:non\s+lett[aeio]|da\s+leggere)\b",
    re.IGNORECASE,
)

# Rumore iniziale vocale: “ultime email da mario” deve lasciare “da mario”.
_LEADING_LIST_NOISE_RE = re.compile(
    r"(?i)^\s*(?:ultime\s+)?(?:e-?mails?|mails?)\s+",
)

# `oggetto fattura` → subject:fattura; non tocca un `subject:` già emesso da Gemini.
_OGGETTO_RE = re.compile(r"(?i)\boggetto\s+(\S+)")

# `da mario` → from:mario; uno token (nomi multi-parola restano a Gemini con from:).
_DA_FROM_RE = re.compile(r"(?i)\bda\s+([^\s:]+)")

# Ore in italiano: Gmail non ha l'unità `h` su newer_than; serve after:<unix>.
# Cattura anche “nelle ultime 48 ore” in mezzo a from:mario …
_HOURS_IT_RE = re.compile(r"(?i)\b(?:nelle\s+)?ultime\s+(\d+)\s+ore\b")

# Giorni in italiano → newer_than:Nd (d/m/y sono le uniche unità Gmail valide).
_DAYS_IT_RE = re.compile(r"(?i)\b(?:negli\s+)?ultimi\s+(\d+)\s+giorni\b")

# Gemini può inventare newer_than:5h: lo riscriviamo in after:epoch sul processo.
_NEWER_THAN_HOURS_RE = re.compile(r"(?i)\bnewer_than:(\d+)h\b")

# Stopword per resolve `name`: restano indice, mittente o oggetto.
_READ_STOPWORDS: frozenset[str] = frozenset(
    {
        "la",
        "il",
        "lo",
        "le",
        "l",
        "un",
        "una",
        "email",
        "mail",
        "e-mail",
        "messaggio",
        "quella",
        "quello",
        "numero",
        "leggi",
        "leggimi",
        "apri",
        "mostrami",
        "mostra",
    }
)

# Ordinali / cardinali parlati → indice 1-based sulla lista in sessione (cap 20).
_INDEX_WORDS: dict[str, int] = {
    "prima": 1,
    "primo": 1,
    "uno": 1,
    "seconda": 2,
    "secondo": 2,
    "due": 2,
    "terza": 3,
    "terzo": 3,
    "tre": 3,
    "quarta": 4,
    "quarto": 4,
    "quattro": 4,
    "quinta": 5,
    "quinto": 5,
    "cinque": 5,
    "sesta": 6,
    "sesto": 6,
    "sei": 6,
    "settima": 7,
    "settimo": 7,
    "sette": 7,
    "ottava": 8,
    "ottavo": 8,
    "otto": 8,
    "nona": 9,
    "nono": 9,
    "nove": 9,
    "decima": 10,
    "decimo": 10,
    "dieci": 10,
}

# Solo cifre, eventualmente “numero 2” già pulito dalle stopword.
_DIGITS_RE = re.compile(r"^\d+$")

# Logo/pixel/firma: sotto questa soglia un'immagine senza disposition=attachment
# non si annuncia (Gmail metadata dà body.size in byte, senza scaricare il binario).
_SMALL_IMAGE_MAX_BYTES = 40 * 1024

# Corpo HTML/plain: anche con filename spurio restano testo, non file da nominare.
_BODY_MIME_TYPES: frozenset[str] = frozenset({"text/plain", "text/html"})

# Nomi tipici di firma/logo Outlook e newsletter; IGNORECASE, estensione opzionale.
# image001.png (cifre obbligatorie) ≠ image.png, che può essere una foto vera.
_DECORATIVE_FILENAME_RE = re.compile(
    r"(?i)^(logo|signature|untitled)(?:\.[^.]+)?$|^image\d+",
)


class GmailReadError(ValueError):
    """Contratto tool lettura (lista vuota, name assente): messaggio per il TTS."""


@dataclass(frozen=True)
class RealAttachment:
    """Allegato vero (non logo/inline): metadati da format=metadata o full.

    `attachment_id` serve al GET binario (save_attachments); qui lo conserviamo
    così elenco, lettura e salvataggio condividono la stessa classificazione.
    """

    filename: str
    mime_type: str
    size: int = 0
    attachment_id: str = ""


@dataclass
class MailboxItem:
    """Una riga della lista vocale: id solo per la GET successiva, mai in reply.

    I campi thread/indirizzo (thread_id, from_address, Reply-To, Message-ID)
    restano in sessione per draft/reply Python: lista e lettura TTS continuano
    a dire solo mittente e oggetto, senza spellingare l'@ in auto.
    """

    gmail_id: str
    sender: str
    subject: str
    # Header From grezzo: il fuzzy può matchare anche l'indirizzo, non solo il display.
    from_header: str = ""
    # Quanti allegati veri (stessa euristica di read/save); 0 = niente suffisso TTS.
    attachment_count: int = 0
    # threadId Gmail (JSON top-level, non un header): il POST send in-reply lo rimpiazza.
    thread_id: str = ""
    # Solo l'@ del From, via parseaddr: destinatario se manca Reply-To.
    from_address: str = ""
    # Reply-To se l'header c'è; vuoto → il reply userà from_address.
    reply_to_address: str = ""
    # Message-ID RFC per In-Reply-To; angle brackets, mai parlato.
    rfc_message_id: str = ""
    # References grezzo: il send lo concatena col Message-ID; vuoto se assente.
    rfc_references: str = ""


@dataclass
class MailboxSession:
    """Stato in-process tra list_emails e read/save (un turno vocale dopo l'altro).

    È lo stato intermedio tra un tool e l'altro, come se fosse uno storico delle
    email recuperate (da leggere o da cui scaricare allegati).
    """

    items: list[MailboxItem] = field(default_factory=list)
    # False finché non è partito un elenco: distingue “non hai ancora elencato”
    # da “elenco andato a vuoto” (read_email deve dirlo in modo diverso).
    listed: bool = False
    last_query: str = ""
    last_gmail_q: str = ""

    def replace(self, items: list[MailboxItem], *, query: str, gmail_q: str) -> None:
        """Sostituisce la mappa 1..N dopo un list_emails (anche se items è vuoto)."""
        self.items = list(items)
        self.listed = True
        self.last_query = query
        self.last_gmail_q = gmail_q

    def clear(self) -> None:
        """Reset di test / nuova sessione: read_email tornerà a chiedere l'elenco."""
        self.items = []
        self.listed = False
        self.last_query = ""
        self.last_gmail_q = ""


# Sessione di default del processo: il loop vocale non deve passare lo spec a ogni tool.
_SESSION = MailboxSession()


def get_mailbox_session() -> MailboxSession:
    """Sessione globale del processo (iniettabile nei tool via argomento `session`)."""
    return _SESSION


def reset_mailbox_session() -> None:
    """Svuota la sessione globale: utile nei test tra un caso e l'altro."""
    _SESSION.clear()


def normalize_gmail_query(raw: str) -> str:
    """Sinonimi italiani → operatori Gmail; il resto (anche q= già valida) passa.

    Non è una whitelist di tre keyword: Gemini può mandare `is:unread`,
    `from:mario is:unread` o una parola libera tipo `fattura`.
    """
    # Collapse spazi STT; query vuota = inbox (comando “ultime email” senza filtri).
    text = " ".join((raw or "").split())
    if not text:
        return "in:inbox"
    if text.casefold() in _INBOX_EXACT:
        return "in:inbox"

    out = text
    # Togli “ultime email ” in testa: resta il filtro (da mario / non lette / fattura).
    stripped = _LEADING_LIST_NOISE_RE.sub("", out).strip()
    if not stripped:
        return "in:inbox"
    out = stripped

    # Prima le non-lette: così “da leggere” non diventa from:leggere.
    out = _UNREAD_RE.sub("is:unread", out)

    # Parola inbox → in:inbox solo se il modello non ha già messo un in:.
    if not re.search(r"(?i)\bin:", out):
        out = re.sub(r"(?i)\binbox\b", "in:inbox", out)

    # oggetto X → subject:X; subject: già presente non matcha `\boggetto`.
    out = _OGGETTO_RE.sub(r"subject:\1", out)

    # da Nome → from:Nome; token con `:` (from:mario) restano intatti.
    def _from_repl(match: re.Match[str]) -> str:
        name = match.group(1)
        return f"from:{name}"

    out = _DA_FROM_RE.sub(_from_repl, out)
    # Tempo relativo dopo i mapping da/oggetto: ore → after:epoch, giorni → newer_than.
    out = _expand_relative_time(out)
    return " ".join(out.split())


def _hours_ago_to_after(hours: int) -> str:
    """Orologio del processo: now - N ore in Unix secondi. Mai un epoch del modello."""
    # int() tronca verso zero: sui timestamp positivi è il grano secondo di Gmail.
    epoch = int(time.time()) - int(hours) * 3600
    return f"after:{epoch}"


def _expand_relative_time(text: str) -> str:
    """Italiano/ore inventate → operatori Gmail; giorni già validi restano intatti.

    Gmail accetta newer_than solo con d/m/y. `newer_than:5h` non è valido: si
    ricalcola after:<unix> qui. `after:YYYY/MM/DD` e `newer_than:3d` passano.
    """

    def _hours_repl(match: re.Match[str]) -> str:
        # group(1) è N sia in “ultime 5 ore” sia in newer_than:5h.
        return _hours_ago_to_after(int(match.group(1)))

    def _days_repl(match: re.Match[str]) -> str:
        return f"newer_than:{match.group(1)}d"

    # Prima l'operatore inventato, poi le frasi italiane (non si sovrappongono).
    out = _NEWER_THAN_HOURS_RE.sub(_hours_repl, text)
    out = _HOURS_IT_RE.sub(_hours_repl, out)
    out = _DAYS_IT_RE.sub(_days_repl, out)
    return out


def clamp_list_limit(limit: object) -> int:
    """Default 5, minimo 1, tetto 20. Accetta int JSON o stringa del modello."""
    if limit is None or limit is False:
        return DEFAULT_LIST_LIMIT
    try:
        # bool è sottoclasse di int: True → 1 sarebbe un limite fuorviante.
        if isinstance(limit, bool):
            return DEFAULT_LIST_LIMIT
        number = int(str(limit).strip())
    except (TypeError, ValueError):
        return DEFAULT_LIST_LIMIT
    if number < 1:
        return 1
    return min(number, MAX_LIST_LIMIT)


def _want_count(count: object) -> bool:
    """True solo per il flag JSON count; un intero o limit non attiva il ramo."""
    # bool è sottoclasse di int: 1 non deve diventare un conteggio.
    if count is True:
        return True
    if isinstance(count, str) and count.strip().casefold() == "true":
        return True
    return False


def _count_filter_phrase(gmail_q: str) -> str:
    """Suffisso parlante del totale: da mittente, non lette, inbox, o niente."""
    match = re.search(r"(?i)\bfrom:([^\s]+)", gmail_q or "")
    if match:
        return f" da {match.group(1)}"
    q = (gmail_q or "").casefold()
    if "is:unread" in q:
        return " non lette"
    if "in:inbox" in q:
        return " in inbox"
    return ""


def format_count_result(total: int, gmail_q: str, *, capped: bool) -> str:
    """Esito parlante del ramo count: numero (o almeno N), senza righe 1. Da …."""
    phrase = _count_filter_phrase(gmail_q)
    if total <= 0:
        return f"OK: nessuna email{phrase}."
    if capped:
        return f"OK: almeno {total} email{phrase}."
    return f"OK: {total} email{phrase}."


def _decode_rfc2047(raw: str) -> str:
    """Decodifica Subject/From MIME encoded-word; fallback al grezzo se rotto."""
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        return str(make_header(decode_header(text))).strip()
    except (LookupError, UnicodeDecodeError, ValueError):
        return text


def _speaker_from_header(from_header: str) -> str:
    """Display name se c'è, altrimenti local-part dell'email, altrimenti sconosciuto."""
    decoded = _decode_rfc2047(from_header)
    display, address = parseaddr(decoded)
    name = (display or "").strip().strip('"')
    if name:
        return name
    email = (address or "").strip()
    if "@" in email:
        return email.split("@", 1)[0]
    if email:
        return email
    if decoded:
        return decoded
    return "mittente sconosciuto"


def _address_from_header(header: str) -> str:
    """Indirizzo SMTP da From o Reply-To: stesso parseaddr del parlato, senza display.

    Serve a draft/reply in sessione: Gemini passa cognome o indice, Python tiene
    l'@ già risolto. Header vuoto o senza parte address con '@' → stringa vuota
    (errore parlante più a valle, niente dominio inventato).
    """
    # Niente parseaddr su header assente: il chiamante tratta '' come “non parsabile”.
    decoded = _decode_rfc2047(header)
    if not decoded:
        return ""
    # Stesso decode RFC2047 + parseaddr di `_speaker_from_header`, ma qui serve l'@.
    _display, address = parseaddr(decoded)
    email = (address or "").strip()
    # Senza '@' non è un destinatario SMTP (display name nudo, token vocale, spazzatura).
    if "@" not in email:
        return ""
    return email


def _subject_from_header(subject_header: str) -> str:
    """Oggetto parlante; vuoto → formula fissa, niente stringa MIME cruda."""
    decoded = _decode_rfc2047(subject_header)
    return decoded if decoded else "senza oggetto"


def _header_map(payload: dict[str, Any]) -> dict[str, str]:
    """Ultima occorrenza per nome header (From/Subject/Reply-To/Message-ID), case-insensitive."""
    found: dict[str, str] = {}
    headers = payload.get("headers")
    if not isinstance(headers, list):
        return found
    for item in headers:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        value = item.get("value")
        if isinstance(name, str) and isinstance(value, str) and name.strip():
            found[name.strip().casefold()] = value
    return found


def _b64url_decode_bytes(data: str) -> bytes:
    """Binario Gmail (allegati): base64url, padding spesso assente. Vuoto se rotto."""
    # Gmail omette il padding `=`; urlsafe_b64decode lo pretende, quindi lo ricalcoliamo.
    compact = "".join((data or "").split())
    if not compact:
        return b""
    padded = compact + "=" * ((4 - len(compact) % 4) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        return b""


def _b64url_decode(data: str) -> str:
    """Corpo Gmail: base64url, padding spesso assente, testo UTF-8 con replace."""
    raw = _b64url_decode_bytes(data)
    if not raw:
        return ""
    return raw.decode("utf-8", errors="replace")


class _HTMLTextExtractor(HTMLParser):
    """Estrae testo visibile da HTML email: niente script/style, break sui blocchi."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.casefold()
        # script/style non si parlano; br/p/div spezzano le frasi per il TTS.
        if lowered in {"script", "style", "head"}:
            self._skip += 1
        elif lowered in {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4"}:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered in {"script", "style", "head"} and self._skip:
            self._skip -= 1
        elif lowered in {"p", "div", "tr", "li", "h1", "h2", "h3", "h4"}:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if data:
            self._chunks.append(data)

    def text(self) -> str:
        # convert_charrefs=True ha già decodificato le entità in handle_data.
        joined = "".join(self._chunks)
        # Collassa spazi e troppe newline: una riga parlabile, non un layout HTML.
        lines = [" ".join(line.split()) for line in joined.splitlines()]
        nonempty = [line for line in lines if line]
        return "\n".join(nonempty).strip()


def strip_html_to_text(html: str) -> str:
    """HTML → testo TTS. Parser stdlib: niente dipendenze extra sul path vocale."""
    extractor = _HTMLTextExtractor()
    try:
        extractor.feed(html or "")
        extractor.close()
    except (ValueError, AssertionError):
        # HTML malformato: fallback grezzo, togli tag con regex e vai comunque.
        plain = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html or "")
        plain = re.sub(r"(?s)<[^>]+>", " ", plain)
        return " ".join(unescape(plain).split())
    return extractor.text()


def _iter_parts(payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Cammina multipart ricorsivo: (mimeType, testo decodificato) senza allegati binari."""
    found: list[tuple[str, str]] = []
    mime = payload.get("mimeType")
    mime_s = mime.strip().casefold() if isinstance(mime, str) else ""
    body = payload.get("body")
    data = body.get("data") if isinstance(body, dict) else None
    # attachmentId senza data = pezzo binario: lo saltiamo (niente MIME in reply).
    if isinstance(data, str) and data.strip() and mime_s.startswith("text/"):
        found.append((mime_s, _b64url_decode(data)))
    parts = payload.get("parts")
    if isinstance(parts, list):
        for part in parts:
            if isinstance(part, dict):
                found.extend(_iter_parts(part))
    return found


def _part_mime(part: dict[str, Any]) -> str:
    """mimeType Gmail in minuscolo; vuoto se assente o non stringa."""
    mime = part.get("mimeType")
    return mime.strip().casefold() if isinstance(mime, str) else ""


def _part_filename(part: dict[str, Any]) -> str:
    """Campo filename del MessagePart (non l'header MIME); strip, niente placeholder."""
    raw = part.get("filename")
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def _part_size_bytes(part: dict[str, Any]) -> int:
    """body.size da metadata/full: byte dichiarati, 0 se manca (non scarichiamo)."""
    body = part.get("body")
    if not isinstance(body, dict):
        return 0
    size = body.get("size")
    try:
        return max(0, int(size))
    except (TypeError, ValueError):
        return 0


def _part_attachment_id(part: dict[str, Any]) -> str:
    """Id per users.messages.attachments.get; vuoto sui pezzi solo-testo."""
    body = part.get("body")
    if not isinstance(body, dict):
        return ""
    aid = body.get("attachmentId")
    return aid.strip() if isinstance(aid, str) else ""


def _part_disposition(headers: dict[str, str]) -> str:
    """Primo token di Content-Disposition: attachment, inline, o vuoto."""
    # Gmail mette "attachment; filename=..." : ci basta il verbo, non il name=.
    raw = headers.get("content-disposition", "")
    token = raw.split(";", 1)[0].strip().casefold()
    return token


def _is_decorative_filename(filename: str) -> bool:
    """True per logo/firma/untitled/image001: non si parlano né si scaricano."""
    # Solo il basename: un path spurio non deve far passare logo.png.
    base = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not base:
        return True
    return _DECORATIVE_FILENAME_RE.search(base) is not None


def _is_real_attachment_part(part: dict[str, Any]) -> bool:
    """Euristica unica elenco/lettura/salvataggio: True solo per file da nominare.

    Vero: filename + disposition attachment, oppure non-immagine con filename
    (pdf/doc/zip anche senza disposition). Falso: inline/cid image/*, nomi
    logo/image001/signature/untitled, immagini < 40 KiB senza attachment.
    """
    filename = _part_filename(part)
    # Senza filename non c'è nulla da annunciare (multipart container, corpo nudo).
    if not filename:
        return False
    mime = _part_mime(part)
    headers = _header_map(part)
    disposition = _part_disposition(headers)
    # text/plain e text/html sono il corpo: un .txt vero arriva come attachment.
    if mime in _BODY_MIME_TYPES and disposition != "attachment":
        return False
    if _is_decorative_filename(filename):
        return False
    is_image = mime.startswith("image/")
    has_cid = bool(headers.get("content-id", "").strip())
    # Logo, pixel tracking, firma HTML: inline o cid + immagine, mai in TTS.
    if is_image and (disposition == "inline" or has_cid):
        return False
    size = _part_size_bytes(part)
    # Foto minuscola senza attachment: quasi sempre icona; la soglia è sui metadata.
    if is_image and disposition != "attachment" and size < _SMALL_IMAGE_MAX_BYTES:
        return False
    if disposition == "attachment":
        return True
    # pdf/doc/zip/csv: i client omettono spesso Content-Disposition.
    if not is_image:
        return True
    # Immagine grande, non inline/cid, con filename: foto allegata senza disposition.
    return True


def iter_real_attachments(payload: dict[str, Any] | None) -> list[RealAttachment]:
    """Cammina il MIME (metadata o full) e tiene solo gli allegati veri.

    format=metadata basta: filename, mimeType, body.size, header disposition.
    Non scarica il binario. Stessa lista per list_emails, read_email, save.
    """

    found: list[RealAttachment] = []

    def _walk(node: dict[str, Any]) -> None:
        # Prima il nodo corrente (messaggio single-part = PDF nudo), poi i figli.
        if _is_real_attachment_part(node):
            found.append(
                RealAttachment(
                    filename=_part_filename(node),
                    mime_type=_part_mime(node),
                    size=_part_size_bytes(node),
                    attachment_id=_part_attachment_id(node),
                )
            )
        children = node.get("parts")
        if isinstance(children, list):
            for child in children:
                if isinstance(child, dict):
                    _walk(child)

    if isinstance(payload, dict):
        _walk(payload)
    return found


def speak_list_attachment_suffix(count: int) -> str:
    """Suffisso riga elenco: ', 1 allegato' / ', N allegati' / vuoto. Niente nomi."""
    if count <= 0:
        return ""
    if count == 1:
        return ", 1 allegato"
    return f", {count} allegati"


def speak_read_attachment_suffix(attachments: list[RealAttachment]) -> str:
    """Suffisso lettura: conta e filename, così Gemini non inventa un PDF."""
    if not attachments:
        return ""
    names = ", ".join(item.filename for item in attachments)
    n = len(attachments)
    if n == 1:
        return f", 1 allegato {names}"
    return f", {n} allegati {names}"


def _local_today() -> date:
    """Giorno civile sull'orologio locale del processo (WSL), non UTC né Date Gmail."""
    # now aware in UTC, poi astimezone() → TZ di sistema (stesso giorno di date.today).
    return datetime.now(tz=UTC).astimezone().date()


def sanitize_attachment_filename(raw: str) -> str:
    """Basename sicuro per il Desktop Windows: niente path, `..`, caratteri vietati.

    Gmail può mandare `../evil.pdf` o `C:\\Windows\\x.pdf` nel campo filename:
    teniamo solo l'ultimo segmento, sostituiamo `<>:\"/\\|?*` e i control char,
    evictiamo i device reserved (CON/PRN/…). Mai un path relativo sotto la
    cartella del giorno. Vuoto dopo la pulizia → `allegato`.
    """
    text = (raw or "").strip()
    # Slash e backslash: solo l'ultimo pezzo, così `../../passwd` diventa `passwd`.
    text = text.replace("\\", "/").rsplit("/", 1)[-1].strip()
    # `.` e `..` da soli non sono un file: cadrebbero sulla cartella padre.
    if text in {"", ".", ".."}:
        return _FALLBACK_ATTACHMENT_NAME
    # NTFS rifiuta questi caratteri; sul mount /mnt/c rompono anche Explorer.
    text = _WIN_FORBIDDEN_RE.sub("_", text)
    # Bordi `_` `.` spazio: Windows non ama trailing dot/space sul basename.
    text = re.sub(r"_+", "_", text).strip(" ._")
    if not text:
        return _FALLBACK_ATTACHMENT_NAME
    # Tetto sul nome, non sul path completo: `_2` e la data devono ancora starci.
    if len(text) > _MAX_ATTACHMENT_FILENAME_LEN:
        suffix = Path(text).suffix[:20]
        stem_budget = _MAX_ATTACHMENT_FILENAME_LEN - len(suffix)
        stem = Path(text).stem[: max(1, stem_budget)]
        text = f"{stem}{suffix}" if suffix else stem
    stem = Path(text).stem
    # CON.txt sul Desktop è un device: prefisso `_` così resta parlabile.
    if stem.casefold() in _WIN_RESERVED_STEMS:
        text = f"_{text}"
    return text


def attachments_day_dir(
    *,
    workspace: Path | None = None,
    today: date | None = None,
) -> Path:
    """Crea (idempotente) WORKSPACE_ROOT/email_attachments/YYYY-MM-DD/.

    La data è l'orologio locale del processo, non la data del messaggio Gmail.
    Crea WORKSPACE_ROOT se manca; non crea notes/ né inbox/ (quelli sono del master).
    """
    # Iniettabile nei test: il default è lo snapshot importato da config.
    root = Path(workspace if workspace is not None else WORKSPACE_ROOT)
    # Giorno civile sull'orologio locale del processo WSL, non UTC né Date header.
    day = today if today is not None else _local_today()
    folder = root / EMAIL_ATTACHMENTS_DIRNAME / day.isoformat()
    # parents=True: manca Ollama_test e/o email_attachments → le ricreiamo.
    folder.mkdir(parents=True, exist_ok=True)
    resolved_root = root.resolve()
    resolved_folder = folder.resolve()
    # Difesa extra: un symlink malevolo non deve farci scrivere fuori dal workspace.
    try:
        resolved_folder.relative_to(resolved_root)
    except ValueError as exc:
        raise GmailReadError("ERRORE: cartella allegati fuori dal workspace") from exc
    return resolved_folder


def _unique_attachment_path(directory: Path, filename: str) -> Path:
    """Se il nome esiste già nel giorno, suffisso `_2`, `_3` prima dell'estensione."""
    # Figlio diretto della cartella del giorno: niente sotto-cartelle dal filename.
    first = directory / filename
    if not first.exists():
        return first
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    # Il primo file tiene il nome originale (è quello che il TTS deve pronunciare).
    for index in range(2, 1000):
        candidate = directory / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
    # Tetto anti-loop: meglio un errore parlante che appendere all'infinito.
    raise GmailReadError("ERRORE: troppi file con lo stesso nome nella cartella del giorno")


def format_save_attachments_result(
    day_stamp: str,
    saved_names: list[str],
    *,
    n_too_big: int = 0,
    n_undownloadable: int = 0,
) -> str:
    """Esito TTS del salvataggio: path relativo parlante, skip senza bloccare.

    Con file scritti: `OK: N allegati salvati in email_attachments/YYYY-MM-DD: a, b.`
    Zero write: `OK: nessun allegato.` più eventuale `1 troppo grande, saltato`.
    """
    if saved_names:
        folder = f"{EMAIL_ATTACHMENTS_DIRNAME}/{day_stamp}"
        names = ", ".join(saved_names)
        n = len(saved_names)
        if n == 1:
            header = f"OK: 1 allegato salvato in {folder}: {names}."
        else:
            header = f"OK: {n} allegati salvati in {folder}: {names}."
    else:
        header = MSG_NO_ATTACHMENTS
    extras: list[str] = []
    if n_too_big == 1:
        extras.append("1 troppo grande, saltato")
    elif n_too_big > 1:
        extras.append(f"{n_too_big} troppo grandi, saltati")
    if n_undownloadable == 1:
        extras.append("1 non scaricabile, saltato")
    elif n_undownloadable > 1:
        extras.append(f"{n_undownloadable} non scaricabili, saltati")
    if not extras:
        return header
    # Header ha già il punto finale: lo teniamo e accodiamo gli skip parlanti.
    return f"{header} {' '.join(f'{item}.' for item in extras)}"


def extract_message_text(message: dict[str, Any]) -> str:
    """Preferisce text/plain; se manca, HTML strip; poi snippet API. Mai id/MIME."""
    payload = message.get("payload")
    parts: list[tuple[str, str]] = []
    if isinstance(payload, dict):
        parts = _iter_parts(payload)

    plain_chunks = [text for mime, text in parts if mime.startswith("text/plain") and text.strip()]
    if plain_chunks:
        body = "\n\n".join(chunk.strip() for chunk in plain_chunks)
        return body.strip()

    html_chunks = [text for mime, text in parts if mime.startswith("text/html") and text.strip()]
    if html_chunks:
        stripped = strip_html_to_text("\n".join(html_chunks))
        if stripped:
            return stripped

    snippet = message.get("snippet")
    if isinstance(snippet, str) and snippet.strip():
        return snippet.strip()
    return ""


def truncate_tts_body(text: str, max_chars: int = MAX_BODY_CHARS) -> tuple[str, bool]:
    """Taglia ~1500 caratteri per il loop vocale; il chiamante annota `(troncato)`."""
    body = (text or "").strip()
    if max_chars > 0 and len(body) > max_chars:
        return body[:max_chars].rstrip(), True
    return body, False


def item_from_message(message: dict[str, Any]) -> MailboxItem | None:
    """Costruisce una riga di sessione da un resource Gmail (metadata o full).

    Mittente e oggetto restano i soli campi parlati. threadId (JSON top-level)
    e gli header From/Reply-To/Message-ID/References restano in sessione per
    far risolvere l'indirizzo e il thread a Python, non a Gemini.
    """
    gmail_id = message.get("id")
    if not isinstance(gmail_id, str) or not gmail_id.strip():
        return None
    payload = message.get("payload")
    payload_dict = payload if isinstance(payload, dict) else {}
    headers = _header_map(payload_dict) if payload_dict else {}
    from_raw = headers.get("from", "")
    subject_raw = headers.get("subject", "")
    # Chiavi casefold di `_header_map`: in lista arrivano solo se chiesti in metadataHeaders.
    reply_to_raw = headers.get("reply-to", "")
    message_id_raw = headers.get("message-id", "")
    references_raw = headers.get("references", "")
    # threadId non è un header RFC: Gmail lo mette sul resource anche in format=metadata.
    thread_raw = message.get("threadId")
    thread_id = thread_raw.strip() if isinstance(thread_raw, str) else ""
    # Stessa euristica della lettura: in lista basta metadata (filename/size).
    attachment_count = len(iter_real_attachments(payload_dict))
    return MailboxItem(
        gmail_id=gmail_id.strip(),
        sender=_speaker_from_header(from_raw),
        subject=_subject_from_header(subject_raw),
        from_header=from_raw,
        attachment_count=attachment_count,
        thread_id=thread_id,
        from_address=_address_from_header(from_raw),
        reply_to_address=_address_from_header(reply_to_raw),
        rfc_message_id=message_id_raw.strip(),
        rfc_references=references_raw.strip(),
    )


def _speak_list_prefix(count: int, gmail_q: str) -> str:
    """Etichetta breve dopo il numero: non lette vs inbox vs generico."""
    q = (gmail_q or "").casefold()
    if "is:unread" in q:
        return f"OK: {count} email non lette."
    if "in:inbox" in q:
        return f"OK: {count} email in inbox."
    return f"OK: {count} email."


def format_list_result(items: list[MailboxItem], gmail_q: str) -> str:
    """Esito parlante numerato, senza id Gmail. Lista vuota = nessuna email trovata."""
    if not items:
        return "OK: nessuna email trovata."
    numbered = " ".join(
        (
            f"{index}. Da {item.sender}, oggetto {item.subject}"
            f"{speak_list_attachment_suffix(item.attachment_count)}."
        )
        for index, item in enumerate(items, start=1)
    )
    return f"{_speak_list_prefix(len(items), gmail_q)} {numbered}"


def _clean_read_name(raw: str) -> str:
    """Toglie stopword di comando; resta indice, mittente o pezzo di oggetto."""
    tokens = re.findall(r"[0-9a-zàèéìòù]+", (raw or "").casefold())
    kept = [tok for tok in tokens if tok not in _READ_STOPWORDS]
    return " ".join(kept).strip()


def resolve_listed_item(name: str, session: MailboxSession) -> MailboxItem:
    """Indice parlato (`1`, `la prima`) o RapidFuzz su mittente/oggetto della lista."""
    if not session.listed:
        raise GmailReadError(MSG_LIST_FIRST)
    if not session.items:
        raise GmailReadError(MSG_EMPTY_LIST)

    cleaned = _clean_read_name(name)
    if not cleaned:
        raise GmailReadError(MSG_EMPTY_NAME)

    n_items = len(session.items)
    # ultima/ultimo: posizione N, non un mittente chiamato “ultima”.
    if cleaned in {"ultima", "ultimo"}:
        return session.items[-1]

    index: int | None = None
    if cleaned in _INDEX_WORDS:
        index = _INDEX_WORDS[cleaned]
    elif _DIGITS_RE.match(cleaned):
        index = int(cleaned)

    if index is not None:
        if 1 <= index <= n_items:
            return session.items[index - 1]
        raise GmailReadError(MSG_NOT_IN_LIST)

    # Quattro chiavi fisse per item: l'indice RapidFuzz si divide per 4 → riga sessione.
    choices: list[str] = []
    for item in session.items:
        choices.append(f"Da {item.sender}, oggetto {item.subject}")
        choices.append(item.sender)
        choices.append(item.subject)
        choices.append(item.from_header)

    hit = process.extractOne(cleaned, choices, scorer=fuzz.WRatio)
    if hit is None:
        raise GmailReadError(MSG_NOT_IN_LIST)
    _best, score, pos = hit
    if float(score) < float(_FUZZY_THRESHOLD):
        raise GmailReadError(MSG_NOT_IN_LIST)
    item_index = int(pos) // 4
    if 0 <= item_index < n_items:
        return session.items[item_index]
    raise GmailReadError(MSG_NOT_IN_LIST)


def _auth_headers(creds: Credentials) -> dict[str, str]:
    """Bearer dall'access token già rinfrescato da get_gmail_credentials."""
    token = creds.token
    if not token:
        raise GmailAuthError(MSG_GMAIL_NOT_LINKED)
    return {"Authorization": f"Bearer {token}"}


def _gmail_get_json(
    http: httpx.Client,
    url: str,
    headers: dict[str, str],
    *,
    params: Any = None,
) -> dict[str, Any]:
    """GET JSON Gmail: 401/403 → auth parlante; rete → non raggiungibile; mai browser."""
    try:
        response = http.get(url, headers=headers, params=params)
    except httpx.HTTPError as exc:
        raise GmailAuthError(MSG_PROFILE_UNREACHABLE) from exc
    if response.status_code in (401, 403):
        raise GmailAuthError(MSG_GMAIL_NOT_LINKED)
    if response.status_code == 404:
        raise GmailReadError(MSG_GONE)
    if response.status_code >= 400:
        raise GmailAuthError(f"ERRORE: Gmail HTTP {response.status_code}")
    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise GmailAuthError(MSG_PROFILE_UNREACHABLE) from exc
    if not isinstance(payload, dict):
        raise GmailAuthError(MSG_PROFILE_UNREACHABLE)
    return payload


def _message_url(gmail_id: str) -> str:
    """Dettaglio users/me/messages/{id}; l'id resta nel path HTTP, non nel TTS."""
    return f"{GMAIL_MESSAGES_URL}/{gmail_id}"


def _attachment_url(gmail_id: str, attachment_id: str) -> str:
    """GET binario users/me/messages/{id}/attachments/{aid}; id mai in TTS."""
    # Stesso host della list: gmail.readonly copre questo GET, non serve modify.
    return f"{GMAIL_MESSAGES_URL}/{gmail_id}/attachments/{attachment_id}"


def _fetch_attachment_bytes(
    http: httpx.Client,
    headers: dict[str, str],
    gmail_id: str,
    attachment_id: str,
) -> bytes | None:
    """Scarica il binario di un allegato; None se manca o il payload è rotto.

    401/403 restano GmailAuthError (abort dell'intero tool). 404 o data assente
    → None, così gli altri file della stessa email si salvano comunque.
    """
    try:
        resource = _gmail_get_json(
            http,
            _attachment_url(gmail_id, attachment_id),
            headers,
        )
    except GmailReadError:
        # MSG_GONE sul singolo file: non cancelliamo il resto del salvataggio.
        return None
    data = resource.get("data")
    if not isinstance(data, str) or not data.strip():
        return None
    blob = _b64url_decode_bytes(data)
    return blob if blob else None


def _message_ids_from_listing(listing: dict[str, Any]) -> list[str]:
    """Id dalla list API; Gmail omette `messages` se la query non ha match."""
    raw_ids = listing.get("messages")
    ids: list[str] = []
    if not isinstance(raw_ids, list):
        return ids
    for entry in raw_ids:
        if isinstance(entry, dict):
            mid = entry.get("id")
            if isinstance(mid, str) and mid.strip():
                ids.append(mid.strip())
    return ids


def _next_page_token(listing: dict[str, Any]) -> str | None:
    """Token pagina successiva; assente o blank = ultima pagina."""
    token = listing.get("nextPageToken")
    if isinstance(token, str) and token.strip():
        return token.strip()
    return None


def _count_matching_messages(
    http: httpx.Client,
    headers: dict[str, str],
    gmail_q: str,
) -> tuple[int, bool]:
    """Conta gli id su users/me/messages, pagina 500, tetto COUNT_CAP.

    Non fa GET metadata. Ritorna (totale, capped): capped se si supera il tetto
    o se a COUNT_CAP resta ancora un nextPageToken (casella più grande).
    """
    total = 0
    page_token: str | None = None
    while True:
        # maxResults=500 è il massimo Gmail: niente default 5 del ramo elenco.
        params: dict[str, str | int] = {"q": gmail_q, "maxResults": COUNT_PAGE_SIZE}
        if page_token:
            params["pageToken"] = page_token
        listing = _gmail_get_json(http, GMAIL_MESSAGES_URL, headers, params=params)
        ids = _message_ids_from_listing(listing)
        # Pagina vuota: stop anche se Google mandasse un token spurio (anti-loop).
        if not ids:
            return total, False
        if total + len(ids) > COUNT_CAP:
            return COUNT_CAP, True
        total += len(ids)
        next_token = _next_page_token(listing)
        if total >= COUNT_CAP:
            # Esattamente il tetto: "almeno" solo se esiste un'altra pagina.
            return COUNT_CAP, next_token is not None
        if next_token is None:
            return total, False
        page_token = next_token


def _load_credentials(
    *,
    credentials: Credentials | None,
    settings: Settings | None,
) -> Credentials:
    """Credenziali iniettate (test) oppure token su disco; mai InstalledAppFlow."""
    if credentials is not None:
        return credentials
    return get_gmail_credentials(settings=settings)


def _spoken_error(exc: BaseException) -> str:
    """Normalizza eccezioni a stringa ERRORE: (GmailAuthError è già prefissata)."""
    text = str(exc).strip()
    if text.startswith("ERRORE:"):
        return text
    return f"ERRORE: {text}"


def list_emails(
    query: str = "",
    *,
    limit: object = None,
    count: object = None,
    session: MailboxSession | None = None,
    client: httpx.Client | None = None,
    settings: Settings | None = None,
    credentials: Credentials | None = None,
) -> str:
    """Elenca o conta messaggi; ritorna OK:/ERRORE: parlante.

    `query` libera (operatori Gmail o italiano). `limit` opzionale, default 5 cap 20.
    `count` true: ignora limit, pagina gli id, non tocca la MailboxSession.
    Side-effect elenco: GET lista + GET metadata per riga; scrive `session`.
    """
    # Sessione iniettabile per i test; a runtime è quella del processo vocale.
    box = session if session is not None else _SESSION
    # Mapping italiano → q= Gmail; operatori già validi passano invariati.
    gmail_q = normalize_gmail_query(query)
    counting = _want_count(count)
    # Il default 5 vale solo per l'elenco vocale, mai per “quante email”.
    max_results = clamp_list_limit(limit)
    owns_client = client is None
    http = client or httpx.Client(timeout=_HTTP_TIMEOUT)
    try:
        try:
            # Token da disco o finto nei test: qui non parte mai InstalledAppFlow.
            creds = _load_credentials(credentials=credentials, settings=settings)
            headers = _auth_headers(creds)
            if counting:
                # Solo id + nextPageToken: niente metadata, sessione intatta.
                total, capped = _count_matching_messages(http, headers, gmail_q)
                return format_count_result(total, gmail_q, capped=capped)
            listing = _gmail_get_json(
                http,
                GMAIL_MESSAGES_URL,
                headers,
                params={"q": gmail_q, "maxResults": max_results},
            )
            # Inbox vuota: Gmail omette `messages`; non è un errore, è lista vuota.
            ids = _message_ids_from_listing(listing)
            items: list[MailboxItem] = []
            # N+1: id dalla list; From/Subject/Reply-To/Message-ID e flag allegati
            # dal dettaglio metadata (filename/size/disposition, niente body).
            # threadId arriva nel JSON del messaggio, non come metadataHeader.
            for gmail_id in ids[:max_results]:
                detail = _gmail_get_json(
                    http,
                    _message_url(gmail_id),
                    headers,
                    params=[
                        ("format", "metadata"),
                        ("metadataHeaders", "From"),
                        ("metadataHeaders", "Subject"),
                        ("metadataHeaders", "Message-ID"),
                        ("metadataHeaders", "Reply-To"),
                        ("metadataHeaders", "References"),
                    ],
                )
                item = item_from_message(detail)
                if item is not None:
                    items.append(item)
            # Anche zero risultati: listed=True così read_email non dice “prima elenca”.
            box.replace(items, query=query or "", gmail_q=gmail_q)
            return format_list_result(items, gmail_q)
        except (GmailAuthError, GmailReadError) as exc:
            return _spoken_error(exc)
    finally:
        # Chiudiamo solo i client creati qui, non il MockTransport iniettato dai test.
        if owns_client:
            http.close()


def read_email(
    name: str,
    *,
    session: MailboxSession | None = None,
    client: httpx.Client | None = None,
    settings: Settings | None = None,
    credentials: Credentials | None = None,
    max_chars: int = MAX_BODY_CHARS,
) -> str:
    """Legge un messaggio della ultima lista (indice o fuzzy). Corpo TTS, niente id.

    Senza list_emails prima → ERRORE: prima elenca le email.
    Preferisce text/plain; altrimenti HTML strip; tronca ~1500 caratteri.
    Header parlante: se ci sono allegati veri, conta e nomi file (non i logo).
    """
    box = session if session is not None else _SESSION
    # Name blank: niente resolve né GET; il modello deve ripetere con un indice.
    if not (name or "").strip():
        return MSG_EMPTY_NAME

    owns_client = client is None
    http = client or httpx.Client(timeout=_HTTP_TIMEOUT)
    try:
        try:
            # Indice o fuzzy sulla ultima lista; l'id Gmail non esce da qui.
            item = resolve_listed_item(name, box)
            creds = _load_credentials(credentials=credentials, settings=settings)
            headers = _auth_headers(creds)
            detail = _gmail_get_json(
                http,
                _message_url(item.gmail_id),
                headers,
                params={"format": "full"},
            )
            # plain > HTML strip > snippet; MIME e id restano fuori dalla reply.
            body = extract_message_text(detail)
            if not body:
                return MSG_EMPTY_BODY
            spoken, truncated = truncate_tts_body(body, max_chars=max_chars)
            note = " (troncato)" if truncated else ""
            # Payload full: stessi veri della lista, ma qui nominiamo i file a Gemini.
            payload = detail.get("payload")
            attach = speak_read_attachment_suffix(
                iter_real_attachments(payload if isinstance(payload, dict) else None)
            )
            return (
                f"OK: email da {item.sender}, oggetto {item.subject}{attach}{note}.\n{spoken}"
            )
        except (GmailAuthError, GmailReadError) as exc:
            return _spoken_error(exc)
    finally:
        if owns_client:
            http.close()


def save_attachments(
    name: str,
    *,
    session: MailboxSession | None = None,
    client: httpx.Client | None = None,
    settings: Settings | None = None,
    credentials: Credentials | None = None,
    workspace: Path | None = None,
    today: date | None = None,
) -> str:
    """Scarica gli allegati veri dell'email risolta sulla ultima lista.

    Stesso `name` di `read_email` (indice o mittente). Senza elenco → stesso
    ERRORE. Solo file classificati da `iter_real_attachments` (niente logo/cid).
    Side-effect: GET `attachments.get` + write sotto email_attachments/oggi/.
    Non chiama `ensure_workspace`: niente notes/ né inbox/. Tetto 15 MiB a file:
    skip parlante, gli altri si salvano. Path traversal nel filename → sanitizza.
    """
    box = session if session is not None else _SESSION
    # Name blank: niente resolve né GET; il modello deve ripetere con un indice.
    if not (name or "").strip():
        return MSG_EMPTY_NAME

    owns_client = client is None
    http = client or httpx.Client(timeout=_HTTP_TIMEOUT)
    try:
        try:
            # Indice o fuzzy sulla ultima lista; l'id Gmail non esce da qui.
            item = resolve_listed_item(name, box)
            creds = _load_credentials(credentials=credentials, settings=settings)
            headers = _auth_headers(creds)
            # format=full: attachmentId (e body.data sui pezzi piccoli) senza N GET.
            detail = _gmail_get_json(
                http,
                _message_url(item.gmail_id),
                headers,
                params={"format": "full"},
            )
            payload = detail.get("payload")
            reals = iter_real_attachments(payload if isinstance(payload, dict) else None)
            # Solo logo/firma: stessa formula della lettura senza suffisso, zero disco.
            if not reals:
                return MSG_NO_ATTACHMENTS

            day = today if today is not None else _local_today()
            # mkdir solo al primo write: tutti skip (troppo grandi) non lasciano cartelle vuote.
            day_dir: Path | None = None
            saved_names: list[str] = []
            n_too_big = 0
            n_undownloadable = 0
            for att in reals:
                # Metadata size prima del GET: non scarichiamo un 20 MiB per poi skippare.
                if att.size > MAX_ATTACHMENT_BYTES:
                    n_too_big += 1
                    continue
                if not att.attachment_id:
                    # Senza attachmentId Gmail non espone il binario su questo pezzo.
                    n_undownloadable += 1
                    continue
                blob = _fetch_attachment_bytes(http, headers, item.gmail_id, att.attachment_id)
                if blob is None:
                    n_undownloadable += 1
                    continue
                # Metadata bugiardo: il tetto vale anche sui byte reali decodificati.
                if len(blob) > MAX_ATTACHMENT_BYTES:
                    n_too_big += 1
                    continue
                if day_dir is None:
                    day_dir = attachments_day_dir(workspace=workspace, today=day)
                safe_name = sanitize_attachment_filename(att.filename)
                dest = _unique_attachment_path(day_dir, safe_name)
                # Ultimo check: il resolve non deve uscire dalla cartella del giorno.
                try:
                    dest.resolve().relative_to(day_dir)
                except ValueError:
                    n_undownloadable += 1
                    continue
                dest.write_bytes(blob)
                # TTS usa il nome scritto (sanitizzato / _2), non il filename Gmail crudo.
                saved_names.append(dest.name)
            return format_save_attachments_result(
                day.isoformat(),
                saved_names,
                n_too_big=n_too_big,
                n_undownloadable=n_undownloadable,
            )
        except OSError as exc:
            # Disco pieno / Desktop non montato: parlante, niente stacktrace.
            return _spoken_error(exc)
        except (GmailAuthError, GmailReadError) as exc:
            return _spoken_error(exc)
    finally:
        if owns_client:
            http.close()
