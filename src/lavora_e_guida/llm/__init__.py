"""Local and cloud LLM clients (Phase 2 / 4)."""

from lavora_e_guida.llm.cloud import CloudLLM, OpenAIChat, OpenAIError
from lavora_e_guida.llm.errors import LLMError
from lavora_e_guida.llm.local_ollama import LocalOllama, OllamaError

__all__ = [
    "CloudLLM",
    "LLMError",
    "LocalOllama",
    "OllamaError",
    "OpenAIChat",
    "OpenAIError",
]
