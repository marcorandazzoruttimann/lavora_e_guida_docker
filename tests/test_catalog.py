"""Catalogo tool: dichiarazione canonica e involucro Gemini, non OpenAI."""

from __future__ import annotations

import pytest

from lavora_e_guida.fs.agent import FS_AGENT_SPEC, FS_GEMINI_TOOLS, FS_TOOL_DECLARATIONS
from lavora_e_guida.gmail.agent import GMAIL_GEMINI_TOOLS, GMAIL_TOOL_DECLARATIONS
from lavora_e_guida.master.agent import (
    MASTER_AGENT_SPEC,
    MASTER_GEMINI_TOOLS,
    MASTER_TOOL_DECLARATIONS,
)
from lavora_e_guida.tools.catalog import (
    ToolDeclaration,
    enum_param,
    object_schema,
    string_param,
    to_gemini_tools,
)
from lavora_e_guida.web.agent import WEB_GEMINI_TOOLS, WEB_TOOL_DECLARATIONS


def test_to_gemini_tools_has_function_declarations_not_openai_wrapper() -> None:
    """Impesud-su-Gemini: nido functionDeclarations, niente type=function."""
    decl = ToolDeclaration(
        name="append_note",
        description="Aggiunge una riga.",
        parameters=object_schema(
            {"name": string_param("Nome nota."), "content": string_param("Testo.")},
            required=("name", "content"),
        ),
    )
    tools = to_gemini_tools((decl,))
    assert len(tools) == 1
    inner = tools[0]["functionDeclarations"]
    assert inner[0]["name"] == "append_note"
    assert inner[0]["parameters"]["type"] == "object"
    assert "type" not in tools[0]
    dumped = str(tools)
    assert '"type": "function"' not in dumped
    assert "functionDeclarations" in tools[0]


def test_enum_param_declares_closed_value_set() -> None:
    """`enum` nello schema: tipo string più insieme chiuso, copiato dal chiamante."""
    values = ["general", "news"]
    param = enum_param("Argomento della ricerca.", values)
    # Gemini non ha un tipo `enum`: è un vincolo sul valore di una stringa.
    assert param["type"] == "string"
    assert param["enum"] == ["general", "news"]
    # Copia difensiva: la declaration è condivisa, non deve seguire la lista fuori.
    values.append("finance")
    assert param["enum"] == ["general", "news"]
    # Enum vuoto: schema insensato, meglio fallire all'import del catalogo.
    with pytest.raises(ValueError):
        enum_param("Vuoto.", ())


def test_fs_gmail_and_web_catalogs_are_isolated() -> None:
    """Quattro agenti: tre cataloghi di dominio disgiunti; master solo i tre ask_*."""
    fs_names = {item.name for item in FS_TOOL_DECLARATIONS}
    gmail_names = {item.name for item in GMAIL_TOOL_DECLARATIONS}
    web_names = {item.name for item in WEB_TOOL_DECLARATIONS}
    master_names = {item.name for item in MASTER_TOOL_DECLARATIONS}
    assert fs_names == {
        "create_text_file",
        "append_note",
        "read_file",
        "find_file",
    }
    assert gmail_names == {
        "list_emails",
        "read_email",
        "save_attachments",
        "draft_email",
        "reply_email",
        "reply_all_email",
        "send_email",
    }
    # Specialista web: un tool solo, isolato dagli altri due cataloghi.
    assert web_names == {"web_search"}
    assert "append_note" not in gmail_names
    assert "list_emails" not in fs_names
    assert "web_search" not in fs_names
    assert "web_search" not in gmail_names
    assert not web_names & (fs_names | gmail_names)
    assert FS_AGENT_SPEC.name == "fs"
    assert FS_GEMINI_TOOLS[0]["functionDeclarations"]
    assert GMAIL_GEMINI_TOOLS[0]["functionDeclarations"]
    assert WEB_GEMINI_TOOLS[0]["functionDeclarations"]
    # Router: Gemini del master non vede create_text_file / list_emails / web_search.
    assert MASTER_AGENT_SPEC.name == "master"
    assert master_names == {"ask_fs", "ask_gmail", "ask_web"}
    assert not master_names & (fs_names | gmail_names | web_names)
    assert MASTER_GEMINI_TOOLS[0]["functionDeclarations"]
