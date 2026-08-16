"""CLI consenso Gmail a tavolino. Mai dal loop vocale.

Avvio: dalla root del repo, venv attivo, `lavora-e-guida-gmail-auth`.
Non tocca STT, TTS né LLM. Il listener resta su 127.0.0.1; l'URL si apre
nel browser Windows (WSL mirrored: il redirect torna al processo Python).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from typing import Any

from dotenv import load_dotenv
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from lavora_e_guida.config import Settings, get_settings
from lavora_e_guida.gmail.oauth import (
    INCLUDE_GRANTED_SCOPES,
    GmailAuthError,
    build_installed_app_flow,
    gmail_profile,
    resolve_token_path,
    save_credentials,
)

# Redirect URI visibile a Google e bind del listener: stesso 127.0.0.1.
# Non usiamo "localhost": su WSL può risolvere in ::1 e il browser Windows
# non raggiungerebbe il processo Linux.
_REDIRECT_HOST = "127.0.0.1"
_BIND_ADDR = "127.0.0.1"
# Porta fissa: in NAT/vecchio WSL si inoltra questa verso il listener.
# Desktop OAuth accetta qualsiasi porta su 127.0.0.1.
DEFAULT_REDIRECT_PORT = 8090

# GMAIL_USER è obbligatorio nel CLI: dopo il consenso confrontiamo il profile.
MSG_MISSING_USER = (
    "ERRORE: GMAIL_USER assente, imposta l'indirizzo Gmail nel file .env"
)
# {url} lo riempie InstalledAppFlow.run_local_server dopo aver scelto la porta.
MSG_AUTH_PROMPT = (
    "Apri questo URL nel browser Windows (non in WSL) e accedi "
    "con l'account GMAIL_USER:\n{url}"
)
# HTML minimo nella tab dopo il redirect: l'utente può chiudere la finestra.
MSG_BROWSER_SUCCESS = (
    "Autenticazione Gmail completata. Puoi chiudere questa finestra."
)
MSG_FLOW_FAILED = "ERRORE: consenso Gmail interrotto o timeout, riprova a tavolino"
MSG_CANCELLED = "ERRORE: consenso Gmail annullato"


def run_desktop_consent(
    flow: InstalledAppFlow,
    *,
    port: int = DEFAULT_REDIRECT_PORT,
) -> Credentials:
    """Listener locale, URL stampato, nessun webbrowser da WSL.

    `include_granted_scopes` è la stringa "true" (non il bool Python): un
    consenso futuro per send/modify aggiunge permessi senza togliere readonly.
    `prompt=consent` + `access_type=offline` forzano un refresh_token anche
    al re-consenso incrementale.
    """
    return flow.run_local_server(
        host=_REDIRECT_HOST,
        bind_addr=_BIND_ADDR,
        port=port,
        open_browser=False,
        include_granted_scopes=INCLUDE_GRANTED_SCOPES,
        access_type="offline",
        prompt="consent",
        authorization_prompt_message=MSG_AUTH_PROMPT,
        success_message=MSG_BROWSER_SUCCESS,
    )


def bootstrap_gmail(
    *,
    settings: Settings | None = None,
    run_flow: Callable[[InstalledAppFlow], Credentials] | None = None,
    profile_client: Any | None = None,
) -> str:
    """Flow Desktop → ping profile → token su disco. Ritorna l'indirizzo collegato.

    `run_flow` e `profile_client` sono iniettabili: i test non aprono listener
    né colpiscono Google. Il file token si scrive solo dopo il match su
    GMAIL_USER, così un account sbagliato nel browser non resta su disco.
    """
    cfg = settings if settings is not None else get_settings()
    expected = (cfg.gmail_user or "").strip()
    # Senza mailbox attesa il match post-consenso sarebbe un no-op silenzioso.
    if not expected:
        raise GmailAuthError(MSG_MISSING_USER)

    # Client id/secret mancanti → GmailAuthError già parlante, prima del listener.
    flow = build_installed_app_flow(settings=cfg)
    runner = run_flow if run_flow is not None else run_desktop_consent
    try:
        creds = runner(flow)
    except GmailAuthError:
        raise
    except OSError as exc:
        # Bind fallito (porta occupata) o socket: niente traceback Google/WSGI.
        raise GmailAuthError(
            f"ERRORE: listener Gmail su {_BIND_ADDR}:{DEFAULT_REDIRECT_PORT} "
            "non avviabile"
        ) from exc
    except Exception as exc:
        # SDK OAuth / timeout WSGI: niente traceback verso l'utente a tavolino.
        raise GmailAuthError(MSG_FLOW_FAILED) from exc

    # Profile prima del save: mismatch → GmailAuthError, gmail_token.json intatto.
    actual = gmail_profile(
        creds,
        expected_user=expected,
        settings=cfg,
        client=profile_client,
    )
    try:
        save_credentials(creds, resolve_token_path(cfg))
    except OSError as exc:
        raise GmailAuthError("ERRORE: impossibile salvare il token Gmail su disco") from exc
    return actual


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Solo --help: nessun flag runtime (il consenso è sempre a tavolino)."""
    parser = argparse.ArgumentParser(
        prog="lavora-e-guida-gmail-auth",
        description=(
            "Collega Gmail con OAuth Desktop a tavolino. "
            "Stampa un URL da aprire nel browser Windows. Non tocca STT, TTS né LLM."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Entry point console: OAuth una tantum, zero stacktrace verso l'utente."""
    # .env in cwd/repo: stesso schema di lavora-e-guida (GEMINI_API_KEY / GMAIL_*).
    load_dotenv()
    _parse_args(argv)
    try:
        settings = get_settings()
        email = bootstrap_gmail(settings=settings)
    except GmailAuthError as exc:
        # from None: argparse/SystemExit non devono mostrare la catena OAuth.
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print(MSG_CANCELLED, file=sys.stderr)
        raise SystemExit(1) from None
    token_path = resolve_token_path(settings)
    print(f"OK: Gmail collegata ({email}), token in {token_path}")


if __name__ == "__main__":
    # Permette `python -m lavora_e_guida.gmail.auth_cli` oltre alla console script.
    main()
