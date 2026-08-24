"""Catalogo tool: dichiarazione canonica e involucro Gemini, non OpenAI."""

from __future__ import annotations

from lavora_e_guida.agent import FS_GEMINI_TOOLS, FS_TOOL_DECLARATIONS, MASTER_AGENT_SPEC
from lavora_e_guida.gmail.agent import GMAIL_GEMINI_TOOLS, GMAIL_TOOL_DECLARATIONS
from lavora_e_guida.tools.catalog import (
    ToolDeclaration,
    object_schema,
    string_param,
    to_gemini_tools,
)


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


def test_fs_and_gmail_catalogs_are_isolated() -> None:
    """Due mappe: FS non contiene list_emails; Gmail non contiene append_note."""
    fs_names = {item.name for item in FS_TOOL_DECLARATIONS}
    gmail_names = {item.name for item in GMAIL_TOOL_DECLARATIONS}
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
        "send_email",
    }
    assert "append_note" not in gmail_names
    assert "list_emails" not in fs_names
    assert MASTER_AGENT_SPEC.name == "fs"
    assert FS_GEMINI_TOOLS[0]["functionDeclarations"]
    assert GMAIL_GEMINI_TOOLS[0]["functionDeclarations"]
