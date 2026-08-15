"""Entrypoint vocale: Mock/HTTP STT/TTS + LLM (Ollama o Gemini) + tool FS/RAG.

Avvio: `lavora-e-guida` oppure `python -m lavora_e_guida`.
Switch: `--llm ollama|gemini` (default ollama) e `--model` opzionale.
Audio: `AUDIO_DRIVER=mock|http` da Settings (factory, non Mock hardcoded).
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Literal

from dotenv import load_dotenv

from lavora_e_guida.agent import build_llm, run_chat_loop
from lavora_e_guida.audio import create_audio_pair
from lavora_e_guida.config import INDEX_ROOT, OLLAMA_URL, WORKSPACE_ROOT, get_settings
from lavora_e_guida.llm.cloud import GeminiChat
from lavora_e_guida.llm.local_ollama import LocalOllama
from lavora_e_guida.rag.index_sync import sync_workspace_index
from lavora_e_guida.tools.fs import ensure_workspace


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI: provider LLM e override modello (retrocompatibile senza flag)."""
    parser = argparse.ArgumentParser(
        prog="lavora-e-guida",
        description="Assistente vocale FS: STT/TTS + tool file sul Desktop.",
    )
    # Default ollama: stessi comandi di prima senza --llm.
    parser.add_argument(
        "--llm",
        choices=("ollama", "gemini"),
        default="ollama",
        help="Provider LLM (default: ollama).",
    )
    # None → factory usa OLLAMA_MODEL / GEMINI_MODEL da config.
    parser.add_argument(
        "--model",
        default=None,
        help="Override modello (Ollama tag o id Gemini).",
    )
    return parser.parse_args(argv)


def _startup_ollama(llm: LocalOllama, model: str) -> None:
    """Ping daemon + presenza tag modello; SystemExit(1) se Ollama non è pronto."""
    # Soft-fail chiaro: senza daemon il loop non ha senso.
    if not llm.ping():
        print(
            f"Ollama non raggiungibile su {OLLAMA_URL}. "
            "Avvia `ollama serve` in WSL (vedi docs/ollama.md).",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # Conferma modello presente: evita chat fallita a metà per tag mancante.
    models = llm.list_models()
    # Ollama a volte espone `qwen2.5:3b` o varianti con digest/tag diversi.
    model_ok = any(name == model or name.startswith(f"{model}") for name in models)
    if not model_ok:
        print(
            f"Modello {model!r} assente. Installati: {models}. "
            f"Esegui: ollama pull {model}",
            file=sys.stderr,
        )
        raise SystemExit(1)


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


def main(argv: list[str] | None = None) -> None:
    """Parse CLI, ping provider, prepara workspace Desktop, loop fino a 'esci'."""
    # .env in cwd/repo: GEMINI_API_KEY senza export manuale in shell.
    load_dotenv()

    args = _parse_args(argv)
    provider: Literal["ollama", "gemini"] = args.llm
    # Settings dopo dotenv: AUDIO_DRIVER e URL bridge già risolti.
    settings = get_settings()

    # Client di proprietà di questo processo: lo chiudiamo sempre in finally.
    llm = build_llm(provider, model=args.model)
    stt = tts = None
    try:
        if provider == "gemini":
            # GeminiChat: assert per type-checker + check auth/rete.
            assert isinstance(llm, GeminiChat)
            _startup_gemini(llm)
        else:
            assert isinstance(llm, LocalOllama)
            _startup_ollama(llm, model=llm.model)

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

        # Factory: mock (CLI, prompt di default già «Tu (mock STT)> »)
        # oppure HTTP verso il bridge Windows (`AUDIO_DRIVER=http`).
        stt, tts = create_audio_pair(settings)

        # Banner: provider + modello risolto (override --model incluso).
        print(
            f"[lab] provider={provider} modello={llm.model} "
            f"data={root} index={INDEX_ROOT} "
            f"audio={settings.audio_driver} "
            f"tools=create_text_file,append_note,read_file,find_file",
            file=sys.stderr,
        )
        code = run_chat_loop(stt, tts, llm)
    finally:
        # Chiude httpx sottostante anche se Ctrl+C / SystemExit dopo ping.
        llm.close()
        # HTTP bridge: chiude i client; Mock non ha close obbligatorio.
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
