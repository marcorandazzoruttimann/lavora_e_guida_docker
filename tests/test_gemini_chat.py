"""Client Gemini: body function calling e parse di text / functionCall."""

from __future__ import annotations

import json
from typing import Any

import httpx

from lavora_e_guida.agent import _assistant_function_call_message
from lavora_e_guida.llm.cloud import (
    GeminiChat,
    _messages_to_gemini_body,
    parse_gemini_turn,
)
from lavora_e_guida.llm.turn import FunctionCall
from lavora_e_guida.tools.catalog import (
    ToolDeclaration,
    object_schema,
    string_param,
    to_gemini_tools,
)


def _fs_tools() -> list[dict[str, Any]]:
    decl = ToolDeclaration(
        name="append_note",
        description="Aggiunge una riga.",
        parameters=object_schema(
            {"name": string_param("n"), "content": string_param("c")},
            required=("name", "content"),
        ),
    )
    return to_gemini_tools((decl,))


def test_messages_body_includes_tools_and_skips_json_mime() -> None:
    """Con declaration: tools + AUTO, niente responseMimeType application/json."""
    messages = [
        {"role": "system", "content": "Sei l'assistente."},
        {"role": "user", "content": "Aggiungi latte alla spesa"},
    ]
    body = _messages_to_gemini_body(messages, tools=_fs_tools(), options={"temperature": 0.1})
    assert "tools" in body
    assert body["toolConfig"]["functionCallingConfig"]["mode"] == "AUTO"
    gen = body.get("generationConfig") or {}
    assert gen.get("responseMimeType") is None
    assert gen.get("temperature") == 0.1
    assert body["systemInstruction"]["parts"][0]["text"] == "Sei l'assistente."


def test_messages_body_function_response_roundtrip() -> None:
    """Storia call + esito: parts functionCall e functionResponse, non testo JSON tool."""
    messages = [
        {"role": "user", "content": "latte"},
        {
            "role": "assistant",
            "function_calls": [
                {"name": "append_note", "args": {"name": "spesa", "content": "latte"}},
            ],
        },
        {
            "role": "user",
            "function_response": {
                "name": "append_note",
                "result": "OK: riga aggiunta",
            },
        },
    ]
    body = _messages_to_gemini_body(messages, tools=_fs_tools(), options=None)
    contents = body["contents"]
    assert contents[1]["role"] == "model"
    assert "functionCall" in contents[1]["parts"][0]
    assert contents[1]["parts"][0]["functionCall"]["name"] == "append_note"
    # Mock senza firma: la chiave non deve comparire (Gemini 3 la valida solo se presente).
    assert "thoughtSignature" not in contents[1]["parts"][0]
    assert contents[2]["role"] == "user"
    assert contents[2]["parts"][0]["functionResponse"]["name"] == "append_note"
    assert contents[2]["parts"][0]["functionResponse"]["response"]["result"] == (
        "OK: riga aggiunta"
    )


def test_parse_gemini_turn_function_call() -> None:
    """candidates parts functionCall → LlmTurn con args dict."""
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "functionCall": {
                                "name": "append_note",
                                "args": {"name": "spesa", "content": "latte"},
                                "id": "call-1",
                            }
                        }
                    ],
                    "role": "model",
                }
            }
        ]
    }
    turn = parse_gemini_turn(payload)
    assert turn.text == ""
    assert len(turn.function_calls) == 1
    call = turn.function_calls[0]
    assert call == FunctionCall(
        name="append_note",
        args={"name": "spesa", "content": "latte"},
        call_id="call-1",
        thought_signature=None,
    )


def test_parse_gemini_turn_thought_signature() -> None:
    """Part con thoughtSignature → FunctionCall.thought_signature, blob intatto."""
    signature = "opaque-base64-blob"
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "functionCall": {
                                "name": "list_emails",
                                "args": {"query": "inbox"},
                            },
                            "thoughtSignature": signature,
                        }
                    ],
                    "role": "model",
                }
            }
        ]
    }
    turn = parse_gemini_turn(payload)
    call = turn.function_calls[0]
    assert call.name == "list_emails"
    assert call.thought_signature == signature


def test_parse_gemini_turn_thought_signature_inside_call() -> None:
    """Difesa: thoughtSignature dentro l'oggetto call, non sulla part."""
    signature = "nested-opaque-blob"
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "functionCall": {
                                "name": "append_note",
                                "args": {"name": "spesa", "content": "latte"},
                                "thoughtSignature": signature,
                            }
                        }
                    ]
                }
            }
        ]
    }
    turn = parse_gemini_turn(payload)
    assert turn.function_calls[0].thought_signature == signature


def test_parse_gemini_turn_thought_signature_snake_case_on_part() -> None:
    """Difesa: thought_signature snake_case sulla part, blob intatto."""
    signature = "snake-case-opaque-blob"
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "functionCall": {
                                "name": "list_emails",
                                "args": {"query": "inbox"},
                            },
                            "thought_signature": signature,
                        }
                    ]
                }
            }
        ]
    }
    turn = parse_gemini_turn(payload)
    assert turn.function_calls[0].thought_signature == signature


def test_assistant_message_omits_signature_when_absent() -> None:
    """Mock senza firma: history e body del secondo turno senza thoughtSignature."""
    # Stesso costruttore dei test loop (gmail/audio): default None, niente chiave.
    call = FunctionCall(name="append_note", args={"name": "spesa", "content": "latte"})
    history_item = _assistant_function_call_message(call)
    assert "thought_signature" not in history_item["function_calls"][0]
    body = _messages_to_gemini_body(
        [
            {"role": "user", "content": "latte"},
            history_item,
        ],
        tools=_fs_tools(),
        options=None,
    )
    assert "thoughtSignature" not in body["contents"][1]["parts"][0]


def test_thought_signature_roundtrip_on_second_turn_body() -> None:
    """Parse → history → serialize: parts[0].thoughtSignature identico, non in functionCall."""
    signature = "opaque-base64-blob"
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "functionCall": {
                                "name": "list_emails",
                                "args": {"query": "inbox"},
                            },
                            "thoughtSignature": signature,
                        }
                    ]
                }
            }
        ]
    }
    turn = parse_gemini_turn(payload)
    # Stesso dict che il loop appende prima del secondo generateContent.
    history_item = _assistant_function_call_message(turn.function_calls[0])
    body = _messages_to_gemini_body(
        [
            {"role": "user", "content": "lista inbox"},
            history_item,
            {
                "role": "user",
                "function_response": {
                    "name": "list_emails",
                    "result": "OK: 3 messaggi",
                },
            },
        ],
        tools=_fs_tools(),
        options=None,
    )
    # contents[1] = turno model (dopo il messaggio user): è lì che scattava il 400.
    model_part = body["contents"][1]["parts"][0]
    assert model_part["thoughtSignature"] == signature
    assert "thoughtSignature" not in model_part["functionCall"]


def test_parse_gemini_turn_text_only() -> None:
    """Reply parlata: parts text, zero functionCall."""
    payload = {
        "candidates": [
            {"content": {"parts": [{"text": "Ho aggiunto latte."}], "role": "model"}}
        ]
    }
    turn = parse_gemini_turn(payload)
    assert turn.text == "Ho aggiunto latte."
    assert turn.function_calls == ()


def test_gemini_chat_posts_tools_and_returns_function_call() -> None:
    """GeminiChat.chat: body con tools, return LlmTurn dalla risposta mock."""
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "functionCall": {
                                        "name": "append_note",
                                        "args": {"name": "spesa", "content": "latte"},
                                    }
                                }
                            ]
                        }
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 4,
                },
            },
        )

    client = httpx.Client(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        transport=httpx.MockTransport(handler),
    )
    llm = GeminiChat(api_key="fake", model="fake-model", client=client)
    try:
        turn = llm.chat(
            [{"role": "user", "content": "aggiungi latte"}],
            tools=_fs_tools(),
        )
    finally:
        client.close()
    assert turn.function_calls[0].name == "append_note"
    assert llm.last_usage.prompt_tokens == 10
    assert captured[0]["tools"]
    assert "responseMimeType" not in (captured[0].get("generationConfig") or {})
