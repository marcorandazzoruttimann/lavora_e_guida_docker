"""Specialista di ricerca web: un solo tool `web_search` sopra Tavily.

Isolato come il pacchetto `gmail/`: il master FS/RAG non deve importare da
qui, e questo pacchetto non importa i tool FS. La sintesi parlata la produce
Gemini nel loop (`include_answer=False` su Tavily: portiamo il nostro LLM).
"""

from lavora_e_guida.web.search import (
    WebResult,
    WebSearchError,
    get_last_web_results,
    get_tavily_client,
    reset_tavily_client,
    web_search,
)

__all__ = [
    "WebResult",
    "WebSearchError",
    "get_last_web_results",
    "get_tavily_client",
    "reset_tavily_client",
    "web_search",
]
