"""OAuth Gmail: token su disco, refresh, ping profile. Mai un browser a runtime.

Contratto per l'agente email (non per il master vocale):
`get_gmail_credentials()` e `gmail_profile(creds)`. Se manca il token, il
refresh fallisce o gli scope non bastano → `GmailAuthError` parlante.
Il consenso `InstalledAppFlow` vive nel CLI a tavolino; questo modulo lo
costruisce, non lo avvia.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from lavora_e_guida.config import Settings, get_settings

# Scope nominati: in Console sono già tutti dichiarati; a runtime fase 1
# chiediamo solo readonly. Stesso file token quando la lista GMAIL_SCOPES crescerà.
SCOPE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_SEND = "https://www.googleapis.com/auth/gmail.send"
SCOPE_MODIFY = "https://www.googleapis.com/auth/gmail.modify"

# Fase 1: lettura mailbox. Send/modify si aggiungono qui al re-consenso incrementale.
GMAIL_SCOPES: tuple[str, ...] = (SCOPE_READONLY,)

# Endpoint REST (httpx, stesso stile di Gemini): niente google-api-python-client.
GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"

# URI OAuth Desktop: il dict `installed` evita di tenere client_secret.json su disco.
_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
_TOKEN_URI = "https://oauth2.googleapis.com/token"

# Google vuole la stringa "true"; il bool Python diventerebbe "True" e verrebbe ignorato.
INCLUDE_GRANTED_SCOPES = "true"

# Permessi del file token: refresh token = secret, non world-readable sul FS Linux.
_TOKEN_FILE_MODE = 0o600

# Messaggi TTS-friendly: prefisso ERRORE, niente stacktrace verso l'utente.
MSG_GMAIL_NOT_LINKED = "ERRORE: Gmail non collegata, esegui autenticazione a tavolino"
MSG_INSUFFICIENT_SCOPES = (
    "ERRORE: scope Gmail insufficienti, riesegui autenticazione a tavolino"
)
MSG_MISSING_CLIENT = (
    "ERRORE: credenziali OAuth Gmail assenti, "
    "imposta GMAIL_CLIENT_ID e GMAIL_CLIENT_SECRET"
)
MSG_PROFILE_UNREACHABLE = "ERRORE: Gmail non raggiungibile, riprova più tardi"


class GmailAuthError(RuntimeError):
    """Auth Gmail fallita: token assente, refresh, scope o account sbagliato."""


def resolve_token_path(settings: Settings) -> Path:
    """Path del JSON refresh: default INDEX_ROOT/gmail_token.json (validator Settings)."""
    path = settings.gmail_token_file
    # Difensivo: il model_validator imposta sempre index_root/gmail_token.json.
    if path is None:
        return Path(settings.index_root) / "gmail_token.json"
    return Path(path)


def installed_client_config(client_id: str, client_secret: str) -> dict[str, Any]:
    """Dict `installed` per InstalledAppFlow, senza file client_secret.json."""
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": _AUTH_URI,
            "token_uri": _TOKEN_URI,
            # Desktop: il listener locale del CLI fissa il redirect su 127.0.0.1.
            "redirect_uris": ["http://localhost"],
        }
    }


def build_installed_app_flow(
    *,
    settings: Settings | None = None,
    scopes: Sequence[str] | None = None,
) -> InstalledAppFlow:
    """Costruisce il flow Desktop da id/secret in Settings. Non apre il browser.

    Il CLI chiama `run_local_server(open_browser=False, ...)` e passa
    `include_granted_scopes=INCLUDE_GRANTED_SCOPES` così un consenso futuro
    per send/modify aggiunge permessi senza perdere readonly.
    """
    cfg = settings if settings is not None else get_settings()
    client_id = (cfg.gmail_client_id or "").strip()
    client_secret = (cfg.gmail_client_secret or "").strip()
    # Senza client Desktop il consenso non può partire: errore chiaro, non stack Google.
    if not client_id or not client_secret:
        raise GmailAuthError(MSG_MISSING_CLIENT)
    requested = tuple(scopes) if scopes is not None else GMAIL_SCOPES
    return InstalledAppFlow.from_client_config(
        installed_client_config(client_id, client_secret),
        scopes=list(requested),
    )


def save_credentials(creds: Credentials, token_path: Path) -> None:
    """Serializza il token e forza permessi 0600 (best-effort su /mnt/c NTFS)."""
    # Parent: INDEX_ROOT può non esistere ancora al primo bootstrap.
    token_path.parent.mkdir(parents=True, exist_ok=True)
    payload = creds.to_json().encode("utf-8")
    # O_CREAT con mode 0600 vale solo se il file non esiste già.
    fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _TOKEN_FILE_MODE)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    # File preesistente poteva essere 0644: chmod dopo la scrittura lo restringe.
    try:
        os.chmod(token_path, _TOKEN_FILE_MODE)
    except OSError:
        # NTFS via /mnt/c spesso ignora chmod: il default INDEX_ROOT è sul FS Linux.
        pass


def load_credentials(token_path: Path) -> Credentials:
    """Legge Credentials dal JSON su disco. Non passa scopes al loader.

    Se passassimo GMAIL_SCOPES a `from_authorized_user_file`, `has_scopes`
    vedrebbe gli scope *richiesti* e non quelli *concessi*: un token solo
    readonly passerebbe un check send. I scopes restano quelli scritti nel file.
    """
    try:
        raw = token_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise GmailAuthError(MSG_GMAIL_NOT_LINKED) from exc
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GmailAuthError(MSG_GMAIL_NOT_LINKED) from exc
    if not isinstance(info, dict):
        raise GmailAuthError(MSG_GMAIL_NOT_LINKED)
    try:
        # scopes=None → usa info["scopes"] dal disco (lista concessa al consenso).
        return Credentials.from_authorized_user_info(info)
    except ValueError as exc:
        # Manca refresh_token / client_id / client_secret: token inutilizzabile.
        raise GmailAuthError(MSG_GMAIL_NOT_LINKED) from exc


def token_covers_scopes(creds: Credentials, scopes: Sequence[str]) -> bool:
    """True se il token copre tutti gli scope richiesti (has_scopes sul disco)."""
    return creds.has_scopes(list(scopes))


def _refresh_and_persist(
    creds: Credentials,
    token_path: Path,
    request: Any | None,
) -> Credentials:
    """Refresh Google + riscrittura file. request iniettabile (test senza rete)."""
    if not creds.refresh_token:
        # Senza refresh_token l'SDK non può rinnovo: serve di nuovo il CLI a tavolino.
        raise GmailAuthError(MSG_GMAIL_NOT_LINKED)
    transport = request if request is not None else Request()
    try:
        creds.refresh(transport)
    except (RefreshError, TransportError) as exc:
        # RefreshError = grant rifiutato; TransportError = rete. Stesso messaggio parlante.
        raise GmailAuthError(MSG_GMAIL_NOT_LINKED) from exc
    # Google aggiorna access token / expiry nel JSON: va riscritto, non vive nel .env.
    save_credentials(creds, token_path)
    return creds


def get_gmail_credentials(
    *,
    settings: Settings | None = None,
    scopes: Sequence[str] | None = None,
    request: Any | None = None,
) -> Credentials:
    """Carica il token, rinfresca se scaduto, verifica gli scope. Mai apre il browser.

    `scopes` default = GMAIL_SCOPES (readonly). Un caller futuro che chiede anche
    send/modify su un token solo-readonly ottiene GmailAuthError (re-consenso CLI).
    """
    cfg = settings if settings is not None else get_settings()
    token_path = resolve_token_path(cfg)
    required = tuple(scopes) if scopes is not None else GMAIL_SCOPES
    # Assenza file: il loop vocale non deve lanciare InstalledAppFlow.
    if not token_path.is_file():
        raise GmailAuthError(MSG_GMAIL_NOT_LINKED)
    creds = load_credentials(token_path)
    # Scope insufficienti prima del refresh: niente hit a Google se manca send/modify.
    if not token_covers_scopes(creds, required):
        raise GmailAuthError(MSG_INSUFFICIENT_SCOPES)
    # valid = access token presente e non scaduto; altrimenti refresh + 0600.
    if not creds.valid:
        creds = _refresh_and_persist(creds, token_path, request)
    return creds


def _mismatch_message(actual: str, expected: str) -> str:
    """Account sbagliato nel browser vs GMAIL_USER nel .env (chiaro a tavolino)."""
    return (
        f"ERRORE: account Gmail collegato ({actual}) diverso da GMAIL_USER ({expected})"
    )


def gmail_profile(
    creds: Credentials,
    *,
    expected_user: str | None = None,
    settings: Settings | None = None,
    client: httpx.Client | None = None,
) -> str:
    """GET users/me/profile con Bearer; ritorna emailAddress.

    Beare Token: autenticazione nell'header delle chiamate http:
    GET /gmail/v1/users/me/profile HTTP/1.1
    Host: gmail.googleapis.com
    Authorization: Bearer ya29.a0Axoo123456789...

    Se `expected_user` (o Settings.gmail_user) è valorizzato e non coincide,
    solleva GmailAuthError (account sbagliato al consenso).
    """
    access_token = creds.token
    if not access_token:
        raise GmailAuthError(MSG_GMAIL_NOT_LINKED)
    # Argomento esplicito vince; altrimenti mailbox attesa da Settings / .env.
    if expected_user is None:
        cfg = settings if settings is not None else get_settings()
        expected_user = cfg.gmail_user
    owns_client = client is None
    http = client or httpx.Client(timeout=30.0)
    try:
        try:
            response = http.get(
                GMAIL_PROFILE_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise GmailAuthError(MSG_PROFILE_UNREACHABLE) from exc
        # 401/403: token revocato o scope sbagliati → stesso messaggio del token assente.
        if response.status_code in (401, 403):
            raise GmailAuthError(MSG_GMAIL_NOT_LINKED)
        if response.status_code >= 400:
            raise GmailAuthError(f"ERRORE: profilo Gmail HTTP {response.status_code}")
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise GmailAuthError(MSG_PROFILE_UNREACHABLE) from exc
        if not isinstance(payload, dict):
            raise GmailAuthError(MSG_PROFILE_UNREACHABLE)
        email = payload.get("emailAddress")
        if not isinstance(email, str) or not email.strip():
            raise GmailAuthError("ERRORE: profilo Gmail senza indirizzo")
        actual = email.strip()
        expected = (expected_user or "").strip()
        # Gmail è case-insensitive sull'indirizzo: confrontiamo in casefold.
        if expected and actual.casefold() != expected.casefold():
            raise GmailAuthError(_mismatch_message(actual, expected))
        return actual
    finally:
        # Chiudiamo solo i client creati qui, non quelli iniettati dai test (MockTransport).
        if owns_client:
            http.close()
