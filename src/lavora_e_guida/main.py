"""Entrypoint vocale: Mock/HTTP STT/TTS + Gemini (function calling) + agente.

Avvio: `lavora-e-guida` oppure `python -m lavora_e_guida`.
Agente: `--agent master|gmail` (master = specialista FS/RAG).
Switch LLM: `--llm gemini|ollama` (default gemini). `--llm ollama` è fail-fast
parlante: il 3B non avvia il loop.
Audio: `AUDIO_DRIVER=mock|http` da Settings (factory, non Mock hardcoded).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

from lavora_e_guida.agent import LoopSpec, build_llm, run_chat_loop
from lavora_e_guida.audio import create_audio_pair
from lavora_e_guida.config import (
    INDEX_ROOT,
    WORKSPACE_ROOT,
    Settings,
    get_settings,
)
from lavora_e_guida.gmail.agent import GMAIL_LOOP_SPEC
from lavora_e_guida.gmail.oauth import GmailAuthError, get_gmail_credentials
from lavora_e_guida.llm.cloud import GeminiChat
from lavora_e_guida.rag.index_sync import sync_workspace_index
from lavora_e_guida.tools.fs import ensure_workspace

# Nomi tool nel banner: allineati allo spec, così a occhio si vede cosa parla.
# master = specialista FS (non un router).
_MASTER_TOOLS_BANNER = "create_text_file,append_note,read_file,find_file"
_GMAIL_TOOLS_BANNER = (
    "list_emails,read_email,save_attachments,draft_email,reply_email,reply_all_email,send_email"
)

# --llm ollama: extra di studio, non avvia il loop né il daemon.
MSG_OLLAMA_LOOP_UNSUPPORTED = (
    "Il loop vocale usa Gemini con function calling nativo. "
    "--llm ollama è extra di studio e non avvia il 3B."
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI: agente, provider LLM e override modello (senza flag = master+gemini)."""
    parser = argparse.ArgumentParser(
        prog="lavora-e-guida",
        description=(
            "Assistente vocale: specialista FS/RAG sul Desktop (`master`) "
            "oppure Gmail (lettura e invio con conferma)."
        ),
    )
    # Default master: specialista FS; il router-only è un piano successivo.
    parser.add_argument(
        "--agent",
        choices=("master", "gmail"),
        default="master",
        help="Agente vocale: master (FS/RAG) o gmail. Default: master.",
    )
    # Ollama resta nel parser per il fail-fast parlante, non per avviare Qwen.
    parser.add_argument(
        "--llm",
        choices=("ollama", "gemini"),
        default="gemini",
        help="Provider LLM (default: gemini). ollama = extra di studio, non avvia il loop.",
    )
    # None → factory usa OLLAMA_MODEL / GEMINI_MODEL da config.
    parser.add_argument(
        "--model",
        default=None,
        help="Override modello (Ollama tag o id Gemini).",
    )
    return parser.parse_args(argv)


def _startup_gemini(llm: GeminiChat) -> None:
    """Chiave presente + ping GET /models; SystemExit(1) se auth/rete falliscono."""
    # Fail early: senza chiave il primo chat darebbe 401 poco chiaro.
    if not (os.environ.get("GEMINI_API_KEY") or "").strip():
        print(
            "GEMINI_API_KEY assente. Imposta la variabile (o .env) e riprova. "
            "Esempio: export GEMINI_API_KEY=...",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if not llm.ping():
        print(
            "Gemini non raggiungibile o chiave non valida "
            "(GET /v1beta/models fallito). Verifica rete e GEMINI_API_KEY.",
            file=sys.stderr,
        )
        raise SystemExit(1)


def _prepare_master_workspace() -> Path:
    """Crea Desktop notes/inbox e sincronizza l'indice RAG. Solo agente master.

    Gmail non deve toccare il workspace né Chroma: lo specialista email non
    mescola FS/RAG. SystemExit(1) se il Desktop non è montato (WSL → Host).
    """
    # Root Desktop + notes/ + inbox/: i file dell'agente restano fuori dal repo.
    try:
        root = ensure_workspace()
    except OSError as exc:
        print(
            f"Workspace non accessibile ({WORKSPACE_ROOT}): {exc}",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    # Sync indice RAG (SQLite + Chroma) nel repo: find_file pronto al primo turno.
    try:
        sync_stats = sync_workspace_index(root, index_root=INDEX_ROOT)
        print(
            f"[lab] RAG sync: scanned={sync_stats.scanned} "
            f"upserted={sync_stats.upserted} "
            f"skipped={sync_stats.skipped_unchanged} "
            f"deleted={sync_stats.deleted}",
            file=sys.stderr,
        )
        if sync_stats.errors:
            print(f"[lab] RAG sync errori: {sync_stats.errors}", file=sys.stderr)
    except ImportError as exc:
        print(
            f"[lab] RAG non disponibile (chromadb assente): {exc}",
            file=sys.stderr,
        )
    except OSError as exc:
        print(f"[lab] RAG sync fallita: {exc}", file=sys.stderr)
    return root


def _require_gmail_token(settings: Settings) -> None:
    """Fail-fast: senza token il loop Gmail non parte. Mai apre il browser.

    Stesso contratto di `get_gmail_credentials`: file assente, refresh o scope
    → GmailAuthError parlante (autenticazione a tavolino). SystemExit(1).
    """
    try:
        # Carica/rinfresca il JSON su disco; non lancia InstalledAppFlow.
        get_gmail_credentials(settings=settings)
    except GmailAuthError as exc:
        # from None: niente traceback OAuth verso l'utente in auto.
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None


def _print_startup_banner(
    *,
    agent: Literal["master", "gmail"],
    provider: str,
    model: str,
    audio_driver: str,
    workspace_root: Path | None,
) -> None:
    """Stderr di avvio: master invariato; gmail senza data/index FS."""
    # Master: stesso testo di prima così i log e gli script a occhio non cambiano.
    if agent == "master":
        print(
            f"[lab] provider={provider} modello={model} "
            f"data={workspace_root} index={INDEX_ROOT} "
            f"audio={audio_driver} "
            f"tools={_MASTER_TOOLS_BANNER}",
            file=sys.stderr,
        )
        return
    # Gmail: niente workspace Desktop; i tool parlanti includono bozza/invio.
    print(
        f"[lab] agent=gmail provider={provider} modello={model} "
        f"audio={audio_driver} "
        f"tools={_GMAIL_TOOLS_BANNER}",
        file=sys.stderr,
    )


def main(argv: list[str] | None = None) -> None:
    """Parse CLI, ping Gemini, prepara master o Gmail, loop fino a 'esci'."""
    # .env in cwd/repo: GEMINI_API_KEY / GMAIL_* senza export manuale in shell.
    load_dotenv()

    args = _parse_args(argv)
    agent: Literal["master", "gmail"] = args.agent
    # Settings dopo dotenv: AUDIO_DRIVER, URL bridge e path token già risolti.
    settings = get_settings()

    # Extra di studio: TTS + stderr, niente daemon Ollama né loop.
    if args.llm == "ollama":
        stt = tts = None
        try:
            stt, tts = create_audio_pair(settings)
            print(MSG_OLLAMA_LOOP_UNSUPPORTED, file=sys.stderr)
            tts.speak(MSG_OLLAMA_LOOP_UNSUPPORTED)
        finally:
            for endpoint in (stt, tts):
                if endpoint is None:
                    continue
                close = getattr(endpoint, "close", None)
                if callable(close):
                    close()
        raise SystemExit(1)

    provider: Literal["gemini"] = "gemini"
    llm = build_llm(provider, model=args.model)
    stt = tts = None
    try:
        assert isinstance(llm, GeminiChat)
        _startup_gemini(llm)

        # Spec del loop: None = default specialista FS (test e banner invariati).
        loop_spec: LoopSpec | None = None
        workspace_root: Path | None = None
        if agent == "gmail":
            # Niente ensure_workspace né sync RAG: solo gate sul token a disco.
            _require_gmail_token(settings)
            loop_spec = GMAIL_LOOP_SPEC
        else:
            workspace_root = _prepare_master_workspace()

        stt, tts = create_audio_pair(settings)

        _print_startup_banner(
            agent=agent,
            provider=provider,
            model=llm.model,
            audio_driver=settings.audio_driver,
            workspace_root=workspace_root,
        )
        code = run_chat_loop(stt, tts, llm, spec=loop_spec)
    finally:
        llm.close()
        for endpoint in (stt, tts):
            if endpoint is None:
                continue
            close = getattr(endpoint, "close", None)
            if callable(close):
                close()
    raise SystemExit(code)


if __name__ == "__main__":
    # Permette `python src/.../main.py` oltre all'entry point console.
    main()
