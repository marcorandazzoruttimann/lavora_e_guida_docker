"""Tool filesystem e ricerca semantica sul workspace utente."""

from lavora_e_guida.tools.catalog import ToolDeclaration, to_gemini_tools
from lavora_e_guida.tools.file_resolver import resolve_file_path
from lavora_e_guida.tools.find import FindToolError, find_file
from lavora_e_guida.tools.fs import (
    FsToolError,
    append_note,
    create_text_file,
    ensure_workspace,
    read_file,
)

__all__ = [
    "FindToolError",
    "FsToolError",
    "ToolDeclaration",
    "append_note",
    "create_text_file",
    "ensure_workspace",
    "find_file",
    "read_file",
    "resolve_file_path",
    "to_gemini_tools",
]
