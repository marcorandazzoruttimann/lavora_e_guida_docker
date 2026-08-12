"""Entry: `PYTHONPATH=src:. python -m sandbox.ollama_fs_lab`.

Avvia il loop MockSTT/TTS + LLM (Ollama o OpenAI) con create/append/read_file.
Switch: `--llm ollama|openai` (default ollama) e `--model` opzionale.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Literal

from dotenv import load_dotenv

from lavora_e_guida.audio.mock import MockSTT, MockTTS
from lavora_e_guida.llm.cloud import OpenAIChat
from lavora_e_guida.llm.local_ollama import LocalOllama

from sandbox.ollama_fs_lab.agent import build_llm, run_chat_loop
from sandbox.ollama_fs_lab.config import INDEX_ROOT, OLLAMA_URL, WORKSPACE_ROOT
from sandbox.ollama_fs_lab.rag.index_sync import sync_workspace_index
from sandbox.ollama_fs_lab.tools_fs import ensure_workspace


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """CLI lab: provider LLM e override modello (retrocompatibile senza flag)."""
    parser = argparse.ArgumentParser(
        prog="python -m sandbox.ollama_fs_lab",
        description="Lab FS vocale: MockSTT/TTS + tool file sul Desktop.",
    )
    # Default ollama: stessi comandi di prima senza --llm.
    parser.add_argument(
        "--llm",
        choices=("ollama", "openai"),
        default="ollama",
        help="Provider LLM (default: ollama).",
    )
    # None → factory usa OLLAMA_MODEL / OPENAI_MODEL da config.
    parser.add_argument(
        "--model",
        default=None,
        help="Override modello (Ollama tag o id OpenAI).",
    )
    return parser.parse_args(argv)


def _startup_ollama(llm: LocalOllama, model: str) -> None:
    """Ping daemon + presenza tag modello; SystemExit(1) se Step 0 fallisce."""
    # Soft-fail chiaro: senza daemon il lab non ha senso (Step 0 obbligatorio).
    if not llm.ping():
        print(
            f"Ollama non raggiungibile su {OLLAMA_URL}. "
            "Avvia `ollama serve` in WSL (vedi README Step 0).",
            file=sys.stderr,
        )
        raise SystemExit(1)

    # Conferma modello presente: evita chat fallita a metà per tag mancante.
    models = llm.list_models()
    # Ollama a volte espone `qwen2.5:3b` o varianti con digest/tag diversi.
    model_ok = any(
        name == model or name.startswith(f"{model}")
        for name in models
    )
    if not model_ok:
        print(
            f"Modello {model!r} assente. Installati: {models}. "
            f"Esegui: ollama pull {model}",
            file=sys.stderr,
        )
        raise SystemExit(1)


def _startup_openai(llm: OpenAIChat) -> None:
    """Chiave presente + ping GET /models; SystemExit(1) se auth/rete falliscono."""
    # Fail early: senza chiave il primo chat darebbe 401 poco chiaro.
    if not (os.environ.get("OPENAI_API_KEY") or "").strip():
        print(
            "OPENAI_API_KEY assente. Imposta la variabile (o .env) e riprova. "
            "Esempio: export OPENAI_API_KEY=sk-...",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if not llm.ping():
        print(
            "OpenAI non raggiungibile o chiave non valida "
            "(GET /v1/models fallito). Verifica rete e OPENAI_API_KEY.",
            file=sys.stderr,
        )
        raise SystemExit(1)


def main(argv: list[str] | None = None) -> None:
    """Parse CLI, ping provider, prepara workspace Desktop, loop fino a 'esci'."""
    # .env in cwd/repo: OPENAI_API_KEY senza export manuale in shell.
    load_dotenv()

    args = _parse_args(argv)
    provider: Literal["ollama", "openai"] = args.llm

    # Client di proprietà di questo processo: lo chiudiamo sempre in finally.
    llm = build_llm(provider, model=args.model)
    try:
        if provider == "openai":
            # OpenAIChat: assert per type-checker + check auth/rete.
            assert isinstance(llm, OpenAIChat)
            _startup_openai(llm)
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
                f"[lab] RAG non disponibile (installa extra lab): {exc}",
                file=sys.stderr,
            )
        except OSError as exc:
            print(f"[lab] RAG sync fallita: {exc}", file=sys.stderr)

        # Mock: tastiera = STT, stdout con [TTS] = altoparlante.
        stt = MockSTT(prompt="Tu (mock STT)> ")
        tts = MockTTS(prefix="[TTS] ")

        # Banner: provider + modello risolto (override --model incluso).
        print(
            f"[lab] provider={provider} modello={llm.model} "
            f"data={root} index={INDEX_ROOT} "
            f"tools=create_text_file,append_note,read_file,find_file",
            file=sys.stderr,
        )
        code = run_chat_loop(stt, tts, llm)
    finally:
        # Chiude httpx sottostante anche se Ctrl+C / SystemExit dopo ping.
        llm.close()
    raise SystemExit(code)


if __name__ == "__main__":
    main()
