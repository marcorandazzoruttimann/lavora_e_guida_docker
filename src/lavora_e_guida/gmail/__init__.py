"""Client Gmail: OAuth a tavolino, lettura e invio con HITL.

L'agente master (FS/RAG) non deve importare questo pacchetto: i tool email
restano nello specialista Gmail.
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
    save_attachments,
)
from lavora_e_guida.gmail.send import draft_email, send_email

__all__ = [
    "GMAIL_SCOPES",
    "SCOPE_MODIFY",
    "SCOPE_READONLY",
    "SCOPE_SEND",
    "GmailAuthError",
    "MailboxSession",
    "draft_email",
    "get_gmail_credentials",
    "gmail_profile",
    "list_emails",
    "read_email",
    "save_attachments",
    "send_email",
]
