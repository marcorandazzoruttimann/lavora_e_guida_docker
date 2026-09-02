"""CLI `--agent`: default router, fs = vecchio master, gmail/web skip RAG."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from lavora_e_guida import main as main_mod
from lavora_e_guida.config import Settings
from lavora_e_guida.fs.agent import FS_LOOP_SPEC
from lavora_e_guida.gmail.agent import GMAIL_LOOP_SPEC
from lavora_e_guida.gmail.oauth import MSG_GMAIL_NOT_LINKED
from lavora_e_guida.llm.cloud import GeminiChat
from lavora_e_guida.master.agent import MASTER_LOOP_SPEC
from lavora_e_guida.rag.index_sync import SyncStats
from lavora_e_guida.web.agent import WEB_LOOP_SPEC


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    """Settings isolate: mock audio, token sotto tmp, niente .env del developer."""
    values: dict[str, Any] = {
        "audio_driver": "mock",
        "index_root": tmp_path / "index",
    }
    # Override per il ramo web: chiave Tavily presente o assente a piacere.
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _stub_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    **overrides: Any,
) -> Settings:
    """Salta dotenv, ping LLM e rete: resta da testare il ramo agente."""
    settings = _settings(tmp_path, **overrides)
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
    """Senza flag: master (router) + gemini (prodotto). Ollama solo se --llm ollama."""
    args = main_mod._parse_args([])
    assert args.agent == "master"
    assert args.llm == "gemini"


def test_parse_args_agent_fs_is_accepted() -> None:
    """`--agent fs` è nelle choices: specialista Desktop, non più il default."""
    args = main_mod._parse_args(["--agent", "fs"])
    assert args.agent == "fs"
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


def test_parse_args_agent_web_defaults_to_gemini() -> None:
    """`--agent web` accettato da argparse: ricerca online con lo stesso Gemini."""
    args = main_mod._parse_args(["--agent", "web"])
    assert args.agent == "web"
    assert args.llm == "gemini"


def test_parse_args_rejects_unknown_agent() -> None:
    """choices argparse: niente handoff/crewai, solo master|fs|gmail|web."""
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
    assert "tools=list_emails,read_email,save_attachments,draft_email,reply_email,reply_all_email,send_email" in err
    assert "find_file" not in err
    assert "RAG sync" not in err


def test_web_mode_fail_fast_missing_tavily_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Chiave Tavily assente: exit 1 con istruzioni, niente workspace/RAG/loop."""
    # Settings senza TAVILY_API_KEY: è il caso del .env non ancora compilato.
    _stub_process(monkeypatch, tmp_path, tavily_api_key=None)

    def _boom_workspace() -> Path:
        raise AssertionError("ensure_workspace non deve partire in modalità web")

    def _boom_sync(*_args: object, **_kwargs: object) -> SyncStats:
        raise AssertionError("sync RAG non deve partire in modalità web")

    monkeypatch.setattr(main_mod, "ensure_workspace", _boom_workspace)
    monkeypatch.setattr(main_mod, "sync_workspace_index", _boom_sync)
    # La ricerca web non ha nulla a che fare con la mailbox: nessun token OAuth.
    monkeypatch.setattr(
        main_mod,
        "get_gmail_credentials",
        lambda **_k: (_ for _ in ()).throw(AssertionError("token Gmail")),
    )
    monkeypatch.setattr(
        main_mod,
        "run_chat_loop",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("loop")),
    )

    with pytest.raises(SystemExit) as exc_info:
        main_mod.main(["--agent", "web"])
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert main_mod.MSG_MISSING_TAVILY_KEY in err
    assert "TAVILY_API_KEY" in err
    # Il banner non deve comparire: il loop non è mai partito.
    assert "agent=web" not in err


def test_web_mode_skips_rag_and_uses_web_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Chiave presente: skip Desktop/RAG e token Gmail, banner web, spec web_search."""
    _stub_process(monkeypatch, tmp_path, tavily_api_key="tvly-fake-key")

    def _boom_workspace() -> Path:
        raise AssertionError("ensure_workspace non deve partire in modalità web")

    def _boom_sync(*_args: object, **_kwargs: object) -> SyncStats:
        raise AssertionError("sync RAG non deve partire in modalità web")

    monkeypatch.setattr(main_mod, "ensure_workspace", _boom_workspace)
    monkeypatch.setattr(main_mod, "sync_workspace_index", _boom_sync)
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
        main_mod.main(["--agent", "web"])
    assert exc_info.value.code == 0
    assert captured["spec"] is WEB_LOOP_SPEC
    err = capsys.readouterr().err
    assert "agent=web" in err
    assert "provider=gemini" in err
    assert "tools=web_search" in err
    # Nessuna traccia degli altri agenti nel banner né del sync indice.
    assert "find_file" not in err
    assert "list_emails" not in err
    assert "RAG sync" not in err
    # La chiave non deve mai finire nei log di avvio.
    assert "tvly-fake-key" not in err


def test_master_mode_prepares_workspace_and_uses_router_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Default master: workspace + RAG, MASTER_LOOP_SPEC, banner ask_*, niente gate Gmail/Tavily."""
    # tavily_api_key assente di proposito: il router non deve fare fail-fast.
    _stub_process(monkeypatch, tmp_path, tavily_api_key=None)
    data_ws = tmp_path / "desktop"
    monkeypatch.setattr(main_mod, "ensure_workspace", lambda: data_ws)
    monkeypatch.setattr(
        main_mod,
        "sync_workspace_index",
        lambda *_a, **_k: SyncStats(),
    )
    # Gate Gmail lazy nel dispatch, non all'avvio: se la CLI chiama il token, boom.
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
    assert captured["spec"] is MASTER_LOOP_SPEC
    err = capsys.readouterr().err
    assert "agent=gmail" not in err
    assert "agent=fs" not in err
    assert "tools=ask_fs,ask_gmail,ask_web" in err
    assert f"data={data_ws}" in err
    assert "index=" in err
    assert "create_text_file" not in err
    assert "web_search" not in err
    assert "provider=gemini" in err
    assert "RAG sync" in err
    assert main_mod.MSG_MISSING_TAVILY_KEY not in err


def test_fs_mode_prepares_workspace_and_uses_fs_spec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--agent fs`: come il vecchio master, FS_LOOP_SPEC e banner create/append/read/find."""
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
        main_mod.main(["--agent", "fs"])
    assert exc_info.value.code == 0
    assert captured["spec"] is FS_LOOP_SPEC
    err = capsys.readouterr().err
    assert "agent=fs" in err
    assert "tools=create_text_file,append_note,read_file,find_file" in err
    assert f"data={data_ws}" in err
    assert "index=" in err
    assert "ask_fs" not in err
    assert "agent=gmail" not in err
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
