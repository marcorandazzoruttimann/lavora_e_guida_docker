"""OAuth Gmail: token, refresh, profile e CLI a tavolino. Nessun hit a Google."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from lavora_e_guida.config import Settings
from lavora_e_guida.gmail import auth_cli
from lavora_e_guida.gmail.oauth import (
    GMAIL_PROFILE_URL,
    GMAIL_SCOPES,
    INCLUDE_GRANTED_SCOPES,
    MSG_GMAIL_NOT_LINKED,
    MSG_INSUFFICIENT_SCOPES,
    MSG_MISSING_CLIENT,
    SCOPE_READONLY,
    SCOPE_SEND,
    GmailAuthError,
    build_installed_app_flow,
    get_gmail_credentials,
    gmail_profile,
    installed_client_config,
    save_credentials,
)

# Mailbox fittizia: i test non devono coincidere con un account reale nel .env.
_USER = "tester@gmail.com"
_CLIENT_ID = "desktop-id.apps.googleusercontent.com"
_CLIENT_SECRET = "desktop-secret"


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    """Settings isolate: niente .env, token sotto tmp_path, kwargs vincono sull'env."""
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


def _make_creds(
    *,
    scopes: list[str] | None = None,
    token: str = "access-token",
    expired: bool = False,
    refresh_token: str | None = "refresh-token",
) -> Credentials:
    """Credentials da disco/memoria: expiry futura = valid, passata = refresh."""
    now = datetime.now(UTC)
    expiry = now - timedelta(hours=1) if expired else now + timedelta(hours=1)
    return Credentials(
        token=token,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=_CLIENT_ID,
        client_secret=_CLIENT_SECRET,
        scopes=list(scopes if scopes is not None else GMAIL_SCOPES),
        expiry=expiry,
    )


def _write_token(tmp_path: Path, creds: Credentials) -> Path:
    """Serializza come il runtime (0600) così load_credentials vede lo stesso JSON."""
    path = tmp_path / "gmail_token.json"
    save_credentials(creds, path)
    return path


def _boom_browser(*_args: Any, **_kwargs: Any) -> Credentials:
    """Se il runtime apre il listener OAuth, il test deve fallire subito."""
    raise AssertionError("run_local_server non deve partire: niente browser nei test")


def _profile_client(email: str, status: int = 200) -> httpx.Client:
    """MockTransport: GET profile senza socket verso gmail.googleapis.com."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Contratto runtime: stesso URL e Bearer; i test non verificano il token.
        assert str(request.url) == GMAIL_PROFILE_URL
        if status != 200:
            return httpx.Response(status, json={"error": {"code": status}})
        return httpx.Response(status, json={"emailAddress": email})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_installed_client_config_is_desktop_dict() -> None:
    """Dict `installed` da env: niente file client_secret.json obbligatorio."""
    cfg = installed_client_config(_CLIENT_ID, _CLIENT_SECRET)
    installed = cfg["installed"]
    assert installed["client_id"] == _CLIENT_ID
    assert installed["client_secret"] == _CLIENT_SECRET
    assert "auth_uri" in installed
    assert "token_uri" in installed


def test_build_flow_from_settings(tmp_path: Path) -> None:
    """Client id/secret in Settings → InstalledAppFlow Desktop, scope fase 1."""
    settings = _settings(tmp_path)
    flow = build_installed_app_flow(settings=settings)
    assert isinstance(flow, InstalledAppFlow)
    assert list(flow.oauth2session.scope) == list(GMAIL_SCOPES)


def test_build_flow_missing_client(tmp_path: Path) -> None:
    """Senza id/secret il CLI deve fallire prima del listener, messaggio parlante."""
    settings = _settings(tmp_path, gmail_client_id="", gmail_client_secret="")
    with pytest.raises(GmailAuthError, match="GMAIL_CLIENT_ID"):
        build_installed_app_flow(settings=settings)
    # Stesso contratto del modulo: costante, non un testo ad hoc nel test.
    with pytest.raises(GmailAuthError, match=MSG_MISSING_CLIENT.split("ERRORE: ", 1)[-1]):
        build_installed_app_flow(settings=settings)


def test_valid_token_does_not_open_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token valido su disco: get_gmail_credentials carica e non tocca il flow."""
    monkeypatch.setattr(InstalledAppFlow, "run_local_server", _boom_browser)
    creds = _make_creds(token="valid-access", expired=False)
    _write_token(tmp_path, creds)
    loaded = get_gmail_credentials(settings=_settings(tmp_path))
    assert loaded.token == "valid-access"
    assert loaded.has_scopes(list(GMAIL_SCOPES))


def test_expired_token_refresh_rewrites_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Access token scaduto: refresh mockato, JSON riscritto, niente browser."""
    monkeypatch.setattr(InstalledAppFlow, "run_local_server", _boom_browser)

    def fake_refresh(self: Credentials, _request: Any) -> None:
        # Simula Google: nuovo access token e expiry futura, stesso refresh_token.
        self.token = "refreshed-access"
        self.expiry = datetime.now(UTC) + timedelta(hours=1)

    monkeypatch.setattr(Credentials, "refresh", fake_refresh)
    _write_token(tmp_path, _make_creds(token="stale-access", expired=True))
    token_path = tmp_path / "gmail_token.json"
    before = token_path.read_text(encoding="utf-8")
    loaded = get_gmail_credentials(settings=_settings(tmp_path), request=object())
    after = token_path.read_text(encoding="utf-8")
    assert loaded.token == "refreshed-access"
    assert after != before
    assert "refreshed-access" in after


def test_missing_token_does_not_open_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """File assente: ERRORE parlante. Solo il CLI può lanciare InstalledAppFlow."""
    monkeypatch.setattr(InstalledAppFlow, "run_local_server", _boom_browser)
    with pytest.raises(GmailAuthError, match="non collegata"):
        get_gmail_credentials(settings=_settings(tmp_path))
    assert str(GmailAuthError(MSG_GMAIL_NOT_LINKED)) == MSG_GMAIL_NOT_LINKED


def test_readonly_token_insufficient_for_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Token solo readonly + caller send: niente refresh/browser, re-auth a tavolino."""
    monkeypatch.setattr(InstalledAppFlow, "run_local_server", _boom_browser)
    _write_token(tmp_path, _make_creds(scopes=[SCOPE_READONLY], expired=False))
    required = (SCOPE_READONLY, SCOPE_SEND)
    with pytest.raises(GmailAuthError, match="scope Gmail insufficienti"):
        get_gmail_credentials(settings=_settings(tmp_path), scopes=required)
    assert MSG_INSUFFICIENT_SCOPES.startswith("ERRORE:")


def test_gmail_profile_mismatch(tmp_path: Path) -> None:
    """Profile diverso da GMAIL_USER: account sbagliato nel browser, niente ambiguità."""
    creds = _make_creds()
    client = _profile_client("altro@gmail.com")
    with pytest.raises(GmailAuthError, match="diverso da GMAIL_USER"):
        gmail_profile(
            creds,
            expected_user=_USER,
            settings=_settings(tmp_path),
            client=client,
        )


def test_gmail_profile_match_returns_address(tmp_path: Path) -> None:
    """Match case-insensitive: Gmail tratta l'indirizzo senza distinzione maiuscole."""
    creds = _make_creds()
    client = _profile_client("Tester@gmail.com")
    actual = gmail_profile(
        creds,
        expected_user=_USER,
        settings=_settings(tmp_path),
        client=client,
    )
    assert actual == "Tester@gmail.com"


def test_bootstrap_mismatch_does_not_write_token(tmp_path: Path) -> None:
    """Account sbagliato: gmail_token.json non viene creato (token precedente intatto)."""
    settings = _settings(tmp_path)
    token_path = settings.gmail_token_file
    assert token_path is not None
    assert not token_path.exists()

    def fake_run(_flow: InstalledAppFlow) -> Credentials:
        return _make_creds()

    with pytest.raises(GmailAuthError, match="diverso da GMAIL_USER"):
        auth_cli.bootstrap_gmail(
            settings=settings,
            run_flow=fake_run,
            profile_client=_profile_client("altro@gmail.com"),
        )
    assert not token_path.exists()


def test_bootstrap_success_writes_token(tmp_path: Path) -> None:
    """Consenso mockato + profile ok: token su disco, indirizzo restituito."""
    settings = _settings(tmp_path)

    def fake_run(_flow: InstalledAppFlow) -> Credentials:
        return _make_creds(token="bootstrap-access")

    email = auth_cli.bootstrap_gmail(
        settings=settings,
        run_flow=fake_run,
        profile_client=_profile_client(_USER),
    )
    assert email == _USER
    token_path = settings.gmail_token_file
    assert token_path is not None and token_path.is_file()
    assert "bootstrap-access" in token_path.read_text(encoding="utf-8")


def test_bootstrap_missing_gmail_user(tmp_path: Path) -> None:
    """CLI senza GMAIL_USER: errore prima del listener (match post-consenso obbligatorio)."""
    settings = _settings(tmp_path, gmail_user="")
    with pytest.raises(GmailAuthError, match="GMAIL_USER assente"):
        auth_cli.bootstrap_gmail(settings=settings, run_flow=_boom_browser)


def test_run_desktop_consent_wsl_kwargs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Listener 127.0.0.1, browser spento, include_granted_scopes stringa true."""
    captured: dict[str, Any] = {}

    def fake_run_local_server(self: InstalledAppFlow, **kwargs: Any) -> Credentials:
        captured.update(kwargs)
        return _make_creds()

    monkeypatch.setattr(InstalledAppFlow, "run_local_server", fake_run_local_server)
    flow = build_installed_app_flow(settings=_settings(tmp_path))
    creds = auth_cli.run_desktop_consent(flow, port=8090)
    assert creds.token == "access-token"
    assert captured["open_browser"] is False
    assert captured["bind_addr"] == "127.0.0.1"
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8090
    assert captured["include_granted_scopes"] == INCLUDE_GRANTED_SCOPES
    assert captured["access_type"] == "offline"
    assert captured["prompt"] == "consent"
