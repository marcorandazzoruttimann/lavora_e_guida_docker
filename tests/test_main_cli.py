"""CLI `--agent`: master invariato, gmail skip RAG e fail-fast sul token."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from lavora_e_guida import main as main_mod
from lavora_e_guida.config import Settings
from lavora_e_guida.gmail.agent import GMAIL_LOOP_SPEC
from lavora_e_guida.gmail.oauth import MSG_GMAIL_NOT_LINKED
from lavora_e_guida.llm.cloud import GeminiChat
from lavora_e_guida.rag.index_sync import SyncStats


def _settings(tmp_path: Path) -> Settings:
    """Settings isolate: mock audio, token sotto tmp, niente .env del developer."""
    return Settings(
        _env_file=None,
        audio_driver="mock",
        index_root=tmp_path / "index",
    )


def _stub_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    """Salta dotenv, ping LLM e rete: resta da testare il ramo agente."""
    settings = _settings(tmp_path)
    monkeypatch.setattr(main_mod, "load_dotenv", lambda: None)
    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)

    def fake_build_llm(
        provider: str, model: str | None = None
    ) -> GeminiChat:
        # Client iniettato: zero rete. Il tipo deve matchare l'assert in main().
        client = httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200))
        )
        return GeminiChat(
            model=model or "fake-gemini",
            api_key="fake-key",
            client=client,
        )

    monkeypatch.setattr(main_mod, "build_llm", fake_build_llm)
    monkeypatch.setattr(main_mod, "_startup_gemini", lambda llm: None)
    return settings


def test_parse_args_agent_default_is_master() -> None:
    """Senza flag: master + gemini (prodotto). Ollama solo se --llm ollama."""
    args = main_mod._parse_args([])
    assert args.agent == "master"
    assert args.llm == "gemini"


def test_parse_args_llm_ollama_is_explicit_backup() -> None:
    """--llm ollama resta nel parser: il runtime poi fa fail-fast, non avvia Qwen."""
    args = main_mod._parse_args(["--llm", "ollama"])
    assert args.llm == "ollama"
    assert args.agent == "master"


def test_parse_args_agent_gmail_defaults_to_gemini() -> None:
    """Gmail senza --llm: stesso Gemini di prodotto, non serve ripetere il flag."""
    args = main_mod._parse_args(["--agent", "gmail"])
    assert args.agent == "gmail"
    assert args.llm == "gemini"


def test_parse_args_rejects_unknown_agent() -> None:
    """choices argparse: niente handoff/crewai, solo master|gmail."""
    with pytest.raises(SystemExit) as exc_info:
        main_mod._parse_args(["--agent", "crewai"])
    assert exc_info.value.code == 2


def test_gmail_mode_fail_fast_missing_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Token assente: autenticazione a tavolino, niente workspace/RAG/loop."""
    _stub_process(monkeypatch, tmp_path)

    def _boom_workspace() -> Path:
        raise AssertionError("ensure_workspace non deve partire in modalità gmail")

    def _boom_sync(*_args: object, **_kwargs: object) -> SyncStats:
        raise AssertionError("sync RAG non deve partire in modalità gmail")

    monkeypatch.setattr(main_mod, "ensure_workspace", _boom_workspace)
    monkeypatch.setattr(main_mod, "sync_workspace_index", _boom_sync)
    monkeypatch.setattr(
        main_mod,
        "run_chat_loop",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("loop")),
    )

    with pytest.raises(SystemExit) as exc_info:
        main_mod.main(["--agent", "gmail"])
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert MSG_GMAIL_NOT_LINKED in err
    assert "tavolino" in err
    assert "agent=gmail" not in err


def test_gmail_mode_skips_rag_and_uses_gmail_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Token ok: skip Desktop/RAG, banner gmail, spec list/read/save."""
    _stub_process(monkeypatch, tmp_path)
    monkeypatch.setattr(main_mod, "get_gmail_credentials", lambda settings=None: object())

    def _boom_workspace() -> Path:
        raise AssertionError("ensure_workspace non deve partire in modalità gmail")

    def _boom_sync(*_args: object, **_kwargs: object) -> SyncStats:
        raise AssertionError("sync RAG non deve partire in modalità gmail")

    monkeypatch.setattr(main_mod, "ensure_workspace", _boom_workspace)
    monkeypatch.setattr(main_mod, "sync_workspace_index", _boom_sync)

    captured: dict[str, Any] = {}

    def fake_loop(*_args: object, spec: object = None, **_kwargs: object) -> int:
        captured["spec"] = spec
        return 0

    monkeypatch.setattr(main_mod, "run_chat_loop", fake_loop)
    monkeypatch.setattr(
        main_mod,
        "create_audio_pair",
        lambda _settings: (MagicMock(), MagicMock()),
    )

    with pytest.raises(SystemExit) as exc_info:
        main_mod.main(["--agent", "gmail"])
    assert exc_info.value.code == 0
    assert captured["spec"] is GMAIL_LOOP_SPEC
    err = capsys.readouterr().err
    assert "agent=gmail" in err
    assert "provider=gemini" in err
    assert "tools=list_emails,read_email,save_attachments,draft_email,reply_email,send_email" in err
    assert "find_file" not in err
    assert "RAG sync" not in err


def test_master_mode_still_prepares_workspace_and_default_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default master: workspace + RAG, loop senza spec Gmail, banner FS."""
    _stub_process(monkeypatch, tmp_path)
    data_ws = tmp_path / "desktop"
    monkeypatch.setattr(main_mod, "ensure_workspace", lambda: data_ws)
    monkeypatch.setattr(
        main_mod,
        "sync_workspace_index",
        lambda *_a, **_k: SyncStats(),
    )
    monkeypatch.setattr(
        main_mod,
        "get_gmail_credentials",
        lambda **_k: (_ for _ in ()).throw(AssertionError("token Gmail")),
    )

    captured: dict[str, Any] = {}

    def fake_loop(*_args: object, spec: object = None, **_kwargs: object) -> int:
        captured["spec"] = spec
        return 0

    monkeypatch.setattr(main_mod, "run_chat_loop", fake_loop)
    monkeypatch.setattr(
        main_mod,
        "create_audio_pair",
        lambda _settings: (MagicMock(), MagicMock()),
    )

    with pytest.raises(SystemExit) as exc_info:
        main_mod.main([])
    assert exc_info.value.code == 0
    assert captured["spec"] is None
    err = capsys.readouterr().err
    assert "agent=gmail" not in err
    assert "tools=create_text_file,append_note,read_file,find_file" in err
    assert "provider=gemini" in err
    assert "RAG sync" in err


def test_llm_ollama_fail_fast_does_not_start_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--llm ollama: messaggio parlante, niente Gemini, niente loop, niente Qwen."""
    _stub_process(monkeypatch, tmp_path)

    def _boom_llm(*_a: object, **_k: object) -> None:
        raise AssertionError("build_llm non deve partire con --llm ollama")

    def _boom_loop(*_a: object, **_k: object) -> int:
        raise AssertionError("run_chat_loop non deve partire con --llm ollama")

    monkeypatch.setattr(main_mod, "build_llm", _boom_llm)
    monkeypatch.setattr(main_mod, "run_chat_loop", _boom_loop)
    tts = MagicMock()
    monkeypatch.setattr(
        main_mod,
        "create_audio_pair",
        lambda _settings: (MagicMock(), tts),
    )

    with pytest.raises(SystemExit) as exc_info:
        main_mod.main(["--llm", "ollama"])
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert main_mod.MSG_OLLAMA_LOOP_UNSUPPORTED in err
    tts.speak.assert_called_once_with(main_mod.MSG_OLLAMA_LOOP_UNSUPPORTED)
