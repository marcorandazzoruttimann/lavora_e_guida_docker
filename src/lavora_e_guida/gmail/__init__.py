"""Client Gmail: OAuth a tavolino e tool di sola lettura.

L'agente master (FS/RAG) non deve importare questo pacchetto: i tool email
restano nello specialista Gmail (`list_emails` / `read_email`).
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
from lavora_e_guida.gmail.read import (
    MailboxSession,
    list_emails,
    read_email,
)

__all__ = [
    "GMAIL_SCOPES",
    "SCOPE_MODIFY",
    "SCOPE_READONLY",
    "SCOPE_SEND",
    "GmailAuthError",
    "MailboxSession",
    "get_gmail_credentials",
    "gmail_profile",
    "list_emails",
    "read_email",
]
