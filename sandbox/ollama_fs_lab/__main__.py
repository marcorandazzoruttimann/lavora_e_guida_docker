"""Entry: `PYTHONPATH=src:. python -m sandbox.ollama_fs_lab`.

Avvia il loop MockSTT/TTS + Ollama con create_text_file + append_note (Step 3).
"""

from __future__ import annotations

import sys

from lavora_e_guida.audio.mock import MockSTT, MockTTS
from lavora_e_guida.llm.local_ollama import LocalOllama

from sandbox.ollama_fs_lab.agent import build_default_llm, run_chat_loop
from sandbox.ollama_fs_lab.config import OLLAMA_MODEL, OLLAMA_URL, WORKSPACE_ROOT
from sandbox.ollama_fs_lab.tools_fs import ensure_workspace


def main() -> None:
    """Ping Ollama, prepara workspace Desktop, poi loop fino a 'esci' / EOF."""
    # Client di proprietà di questo processo: lo chiudiamo sempre in finally.
    llm: LocalOllama = build_default_llm()
    try:
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
            name == OLLAMA_MODEL or name.startswith(f"{OLLAMA_MODEL}")
            for name in models
        )
        if not model_ok:
            print(
                f"Modello {OLLAMA_MODEL!r} assente. Installati: {models}. "
                f"Esegui: ollama pull {OLLAMA_MODEL}",
                file=sys.stderr,
            )
            raise SystemExit(1)

        # Root Desktop + notes/ + inbox/: i file dell'agente restano fuori dal repo.
        try:
            root = ensure_workspace()
        except OSError as exc:
            print(
                f"Workspace non accessibile ({WORKSPACE_ROOT}): {exc}",
                file=sys.stderr,
            )
            raise SystemExit(1) from exc

        # Mock: tastiera = STT, stdout con [TTS] = altoparlante.
        stt = MockSTT(prompt="Tu (mock STT)> ")
        tts = MockTTS(prefix="[TTS] ")

        # Banner: modello, URL e tool FS attivi (create + append note).
        print(
            f"[lab] modello={OLLAMA_MODEL} url={OLLAMA_URL} "
            f"workspace={root} tools=create_text_file,append_note",
            file=sys.stderr,
        )
        code = run_chat_loop(stt, tts, llm)
    finally:
        # Chiude httpx sottostante anche se Ctrl+C / SystemExit dopo ping.
        llm.close()
    raise SystemExit(code)


if __name__ == "__main__":
    main()
