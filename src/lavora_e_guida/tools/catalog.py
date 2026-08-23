"""Catalogo tool canonico (gesto Impesud) in forma Gemini `functionDeclarations`.

Una dichiarazione è `{name, description, parameters}` OpenAPI ristretto.
`to_gemini_tools` produce l'involucro `generateContent`, senza lo wrapper
OpenAI `{"type":"function","function":{...}}`.

Il 3B non entra: questo modulo è il contratto del runtime Gemini.
Side-effect: nessuno (solo dati). I dispatch restano negli specialisti.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolDeclaration:
    """Una funzione che Gemini può chiamare: nome, quando usarla, JSON Schema.

    `parameters` è un oggetto OpenAPI 3 ristretto (`type=object`, `properties`,
    `required`). Niente `$ref` né nidificazioni profonde: i tool vocali usano
    string/integer/boolean.
    """

    name: str
    description: str
    parameters: dict[str, Any]

    def to_gemini(self) -> dict[str, Any]:
        """Oggetto `FunctionDeclaration` per `tools[].functionDeclarations`."""
        # Copia: il caller non deve mutare lo schema condiviso del catalogo.
        return {
            "name": self.name,
            "description": self.description,
            "parameters": dict(self.parameters),
        }


def object_schema(
    properties: dict[str, Any],
    required: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Schema `type=object` per i parametri di una declaration."""
    # Gemini vuole l'oggetto radice sempre object: i tool vocali non hanno
    # parametri posizionali, solo chiavi nominate (name/content/query).
    schema: dict[str, Any] = {
        "type": "object",
        "properties": dict(properties),
    }
    if required:
        schema["required"] = list(required)
    return schema


def string_param(description: str) -> dict[str, Any]:
    """Proprietà stringa con descrizione per il modello (niente placeholder)."""
    return {"type": "string", "description": description}


def integer_param(description: str) -> dict[str, Any]:
    """Proprietà intera (limit, count, …) descritta in italiano."""
    return {"type": "integer", "description": description}


def boolean_param(description: str) -> dict[str, Any]:
    """Proprietà booleana (es. count sulle email)."""
    return {"type": "boolean", "description": description}


def to_gemini_tools(
    declarations: Sequence[ToolDeclaration],
) -> list[dict[str, Any]]:
    """Lista `tools` per `generateContent`: un Tool con tutte le declaration.

    Non è il catalogo OpenAI. Il loop passa questo valore nel body HTTP.
    """
    # Un solo elemento `tools[]`: Gemini raggruppa le functionDeclarations.
    return [
        {
            "functionDeclarations": [item.to_gemini() for item in declarations],
        }
    ]
