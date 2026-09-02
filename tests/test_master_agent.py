"""Router master: whitelist ask_*, storie isolate, HITL inoltrato, gate lazy.

Niente rete: LLM scriptato, Tavily e token Gmail monkeypatchati. Lo specialista
nested gira sul vero `run_specialist_task` (stesso client del loop).
"""

from __future__ import annotations

from collections.abc import Iterator
from io import StringIO
from pathlib import Path
from typing import Any

import pytest

from lavora_e_guida.agent import _default_loop_spec, run_chat_loop
from lavora_e_guida.audio.mock import MockSTT, MockTTS
from lavora_e_guida.gmail.oauth import MSG_GMAIL_NOT_LINKED
from lavora_e_guida.gmail.send import get_draft_session, reset_draft_session
from lavora_e_guida.llm.spoken import SPOKEN_REPLY_RULE
from lavora_e_guida.llm.turn import FunctionCall, LlmTurn
from lavora_e_guida.llm.usage import TokenUsage
from lavora_e_guida.master.agent import (
    MASTER_AGENT_SPEC,
    MASTER_GEMINI_TOOLS,
    MASTER_LOOP_SPEC,
    MASTER_TOOL_DECLARATIONS,
    dispatch_master_tool,
    get_specialist_history,
    reset_specialist_histories,
)
from lavora_e_guida.web.search import MSG_MISSING_KEY


def _declared_names(tools: object) -> frozenset[str]:
    """Nomi in `functionDeclarations` del kwarg `tools` di `chat`."""
    if not isinstance(tools, list):
        return frozenset()
    names: set[str] = set()
    for group in tools:
        if not isinstance(group, dict):
            continue
        for item in group.get("functionDeclarations") or []:
            if isinstance(item, dict) and item.get("name"):
                names.add(str(item["name"]))
    return frozenset(names)


class _RoutingLLM:
    """Un solo client: smista le reply in base al catalogo del chiamante.

    Master vede `ask_*`; lo specialista web vede `web_search`; Gmail vede
    `draft_email`. Così la catena ricerca → bozza gira senza Tavily né REST.
    """

    last_usage = TokenUsage()

    def __init__(self) -> None:
        self.calls = 0
        self.master_rounds = 0
        self.function_call_names: list[str] = []
        self.catalogs: list[frozenset[str]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> LlmTurn:
        self.calls += 1
        names = _declared_names(tools)
        self.catalogs.append(names)

        if "ask_web" in names:
            self.master_rounds += 1
            if self.master_rounds == 1:
                self.function_call_names.append("ask_web")
                return LlmTurn(
                    function_calls=(
                        FunctionCall(
                            name="ask_web",
                            args={"query": "meteo Roma"},
                        ),
                    ),
                )
            if self.master_rounds == 2:
                self.function_call_names.append("ask_gmail")
                return LlmTurn(
                    function_calls=(
                        FunctionCall(
                            name="ask_gmail",
                            args={
                                "query": (
                                    "manda a mario@x.it oggetto Meteo "
                                    "testo Domani a Roma sereno, trenta gradi"
                                ),
                            },
                        ),
                    ),
                )
            raise AssertionError("il master Gemini non deve parlare dopo la bozza HITL")

        if "web_search" in names:
            return LlmTurn(
                text="Domani a Roma sereno, trenta gradi, fonte ansa punto it.",
            )

        if "draft_email" in names:
            self.function_call_names.append("draft_email")
            return LlmTurn(
                function_calls=(
                    FunctionCall(
                        name="draft_email",
                        args={
                            "to": "mario@x.it",
                            "subject": "Meteo",
                            "body": "Domani a Roma sereno, trenta gradi.",
                        },
                    ),
                ),
            )

        if "create_text_file" in names or "append_note" in names:
            raise AssertionError("lo specialista FS non deve partire in questa catena")

        raise AssertionError(f"catalogo inatteso: {sorted(names)}")

    def close(self) -> None:
        return None


class _TextLLM:
    """Specialista che risponde solo a voce, senza tool di dominio."""

    last_usage = TokenUsage()

    def __init__(self, spoken: str) -> None:
        self.spoken = spoken
        self.catalogs: list[frozenset[str]] = []

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> LlmTurn:
        self.catalogs.append(_declared_names(tools))
        return LlmTurn(text=self.spoken)

    def close(self) -> None:
        return None


class _GmailDraftLLM:
    """Master chiede ask_gmail; nested Gmail apre una bozza (HITL)."""

    last_usage = TokenUsage()

    def __init__(self) -> None:
        self.calls = 0

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> LlmTurn:
        self.calls += 1
        names = _declared_names(tools)
        if "ask_gmail" in names:
            return LlmTurn(
                function_calls=(
                    FunctionCall(
                        name="ask_gmail",
                        args={
                            "query": "scrivi a mario@x.it oggetto Ciao testo Ciao.",
                        },
                    ),
                ),
            )
        if "draft_email" in names:
            return LlmTurn(
                function_calls=(
                    FunctionCall(
                        name="draft_email",
                        args={
                            "to": "mario@x.it",
                            "subject": "Ciao",
                            "body": "Ciao.",
                        },
                    ),
                ),
            )
        raise AssertionError(f"catalogo inatteso dopo la bozza: {sorted(names)}")

    def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def _isolated_master_state() -> Iterator[None]:
    """Storie specialist e bozza Gmail pulite: lo stato è di processo."""
    reset_specialist_histories()
    reset_draft_session()
    yield
    reset_specialist_histories()
    reset_draft_session()


def test_master_catalog_is_only_the_three_ask_tools() -> None:
    """Whitelist del router: niente create_text_file, list_emails, web_search."""
    names = {item.name for item in MASTER_TOOL_DECLARATIONS}
    assert names == {"ask_fs", "ask_gmail", "ask_web"}
    assert set(MASTER_AGENT_SPEC.tools) == names
    assert MASTER_AGENT_SPEC.name == "master"
    decls = MASTER_GEMINI_TOOLS[0]["functionDeclarations"]
    assert {item["name"] for item in decls} == names
    for decl in MASTER_TOOL_DECLARATIONS:
        assert decl.parameters["required"] == ["query"]
        assert "<" not in decl.description


def test_dispatch_unknown_domain_tool_is_spoken_error() -> None:
    """I tool di dominio non partono: Gemini del master non li ha in catalogo."""
    result = dispatch_master_tool(
        "create_text_file",
        {"name": "spesa", "content": "latte"},
    )
    assert result.startswith("ERRORE:")
    assert "tool sconosciuto" in result
    assert "ask_fs" in result
    assert "ask_gmail" in result
    assert "ask_web" in result
    assert "create_text_file" not in result.split("Consentiti:", 1)[-1]


def test_ask_web_without_tavily_is_spoken_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate lazy: chiave assente → ERRORE parlante, niente nested Gemini."""

    def _boom_llm(*args: object, **kwargs: object) -> LlmTurn:
        raise AssertionError("ask_web senza Tavily non deve chiamare lo specialista")

    monkeypatch.setattr(
        "lavora_e_guida.master.agent.tavily_key_is_present",
        lambda: False,
    )
    llm = _TextLLM("non usato")
    llm.chat = _boom_llm  # type: ignore[method-assign]
    result = dispatch_master_tool(
        "ask_web",
        {"query": "meteo Roma"},
        llm=llm,
    )
    assert result == MSG_MISSING_KEY
    assert result.startswith("ERRORE:")
    assert get_specialist_history("web") == []


def test_ask_gmail_without_token_is_spoken_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate lazy: token Gmail assente → ERRORE parlante, niente nested Gemini."""
    monkeypatch.setattr(
        "lavora_e_guida.master.agent.gmail_token_is_present",
        lambda: False,
    )
    result = dispatch_master_tool(
        "ask_gmail",
        {"query": "ultime email"},
        llm=_TextLLM("non usato"),
    )
    assert result == MSG_GMAIL_NOT_LINKED
    assert get_specialist_history("gmail") == []


def test_specialist_histories_are_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ask_fs e ask_gmail non condividono system prompt né turni."""
    monkeypatch.setattr(
        "lavora_e_guida.master.agent.gmail_token_is_present",
        lambda: True,
    )
    fs_llm = _TextLLM("Ho aggiunto latte alla spesa.")
    gmail_llm = _TextLLM("Hai tre email in inbox.")

    fs_out = dispatch_master_tool(
        "ask_fs",
        {"query": "aggiungi latte alla spesa"},
        llm=fs_llm,
    )
    gmail_out = dispatch_master_tool(
        "ask_gmail",
        {"query": "ultime email"},
        llm=gmail_llm,
    )
    assert "latte" in fs_out
    assert "email" in gmail_out

    fs_history = get_specialist_history("fs")
    gmail_history = get_specialist_history("gmail")
    assert fs_history[0]["role"] == "system"
    assert gmail_history[0]["role"] == "system"
    assert "Desktop" in fs_history[0]["content"]
    assert "Gmail" in gmail_history[0]["content"]
    assert fs_history[0]["content"] != gmail_history[0]["content"]
    fs_blob = str(fs_history)
    gmail_blob = str(gmail_history)
    assert "aggiungi latte alla spesa" in fs_blob
    assert "ultime email" in gmail_blob
    assert "ultime email" not in fs_blob
    assert "aggiungi latte alla spesa" not in gmail_blob
    assert "ask_fs" not in fs_llm.catalogs[0]
    assert "create_text_file" in fs_llm.catalogs[0]


def test_web_then_gmail_chain_does_not_create_a_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Un enunciato: ask_web poi ask_gmail. Nessun create_text_file. HITL sulla bozza."""
    monkeypatch.setattr(
        "lavora_e_guida.master.agent.tavily_key_is_present",
        lambda: True,
    )
    monkeypatch.setattr(
        "lavora_e_guida.master.agent.gmail_token_is_present",
        lambda: True,
    )
    llm = _RoutingLLM()
    outfile = StringIO()
    code = run_chat_loop(
        MockSTT(
            infile=StringIO(
                "Cerca il meteo di Roma e mandalo a mario@x.it\nesci\n"
            ),
            outfile=outfile,
            prompt="",
        ),
        MockTTS(outfile=outfile, prefix="[TTS] "),
        llm,
        report_latency=False,
        telemetry_db=tmp_path / "telemetry.db",
        spec=MASTER_LOOP_SPEC,
    )
    assert code == 0
    spoken = outfile.getvalue()
    assert "mario@x.it" in spoken
    assert "sì per inviare" in spoken
    assert "Domani a Roma sereno" in spoken
    assert "create_text_file" not in llm.function_call_names
    assert "ask_fs" not in llm.function_call_names
    assert llm.function_call_names == ["ask_web", "ask_gmail", "draft_email"]
    # Master + web (testo) + master + gmail (bozza): niente quinto chat del master.
    assert llm.calls == 4
    assert llm.master_rounds == 2
    session = get_draft_session()
    assert session.awaiting_confirm is True
    assert session.draft is not None
    assert session.draft.to == "mario@x.it"


def test_hitl_after_ask_gmail_skips_master_gemini(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Dopo la bozza nested il loop parla la conferma: il master Gemini non riformula."""
    monkeypatch.setattr(
        "lavora_e_guida.master.agent.gmail_token_is_present",
        lambda: True,
    )
    llm = _GmailDraftLLM()
    outfile = StringIO()
    code = run_chat_loop(
        MockSTT(
            infile=StringIO("scrivi a mario ciao\nesci\n"),
            outfile=outfile,
            prompt="",
        ),
        MockTTS(outfile=outfile, prefix="[TTS] "),
        llm,
        report_latency=False,
        telemetry_db=tmp_path / "telemetry.db",
        spec=MASTER_LOOP_SPEC,
    )
    assert code == 0
    spoken = outfile.getvalue()
    assert "sì per inviare" in spoken
    assert "mario@x.it" in spoken
    # Due chat: master (ask_gmail) + gmail (draft). Niente terza del master.
    assert llm.calls == 2
    assert get_draft_session().awaiting_confirm is True


def test_hitl_on_utterance_forwards_yes_after_delegated_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sì vocale dopo ask_gmail: inoltro a gmail_hitl_on_utterance, senza Gemini."""
    monkeypatch.setattr(
        "lavora_e_guida.master.agent.gmail_token_is_present",
        lambda: True,
    )
    sent: list[str] = []

    def _fake_send() -> str:
        sent.append("sent")
        reset_draft_session()
        return "OK: email inviata a mario@x.it."

    monkeypatch.setattr("lavora_e_guida.gmail.agent.send_email", _fake_send)
    llm = _GmailDraftLLM()
    outfile = StringIO()
    code = run_chat_loop(
        MockSTT(
            infile=StringIO("scrivi a mario ciao\nsì\nesci\n"),
            outfile=outfile,
            prompt="",
        ),
        MockTTS(outfile=outfile, prefix="[TTS] "),
        llm,
        report_latency=False,
        telemetry_db=tmp_path / "telemetry.db",
        spec=MASTER_LOOP_SPEC,
    )
    assert code == 0
    spoken = outfile.getvalue()
    assert "email inviata a mario@x.it" in spoken
    assert sent == ["sent"]
    # Il sì non apre un terzo round Gemini (resta 2: ask_gmail + draft).
    assert llm.calls == 2


def test_default_loop_spec_is_the_master_router() -> None:
    """spec=None nel motore: lazy import del router, non più lo specialista FS."""
    assert _default_loop_spec() is MASTER_LOOP_SPEC
    assert MASTER_LOOP_SPEC.hitl_on_utterance is not None
    assert MASTER_LOOP_SPEC.hitl_after_tool is not None
    assert SPOKEN_REPLY_RULE in MASTER_LOOP_SPEC.system_prompt
    assert "ask_fs" in MASTER_LOOP_SPEC.system_prompt
    assert "create_text_file" not in MASTER_LOOP_SPEC.system_prompt
