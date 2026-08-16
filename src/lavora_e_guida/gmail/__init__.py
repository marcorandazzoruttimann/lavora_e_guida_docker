"""Client Gmail condiviso: OAuth a tavolino, nessun tool nel master vocale.

Il futuro agente email importa da qui. L'agente master (FS/RAG) non deve
importare questo pacchetto: Gmail non entra nel loop vocale né nel system prompt.
"""

from lavora_e_guida.gmail.oauth import (
    GMAIL_SCOPES,
    SCOPE_MODIFY,
    SCOPE_READONLY,
    SCOPE_SEND,
    GmailAuthError,
    get_gmail_credentials,
    gmail_profile,
)

__all__ = [
    "GMAIL_SCOPES",
    "SCOPE_MODIFY",
    "SCOPE_READONLY",
    "SCOPE_SEND",
    "GmailAuthError",
    "get_gmail_credentials",
    "gmail_profile",
]
