"""Testo parlato verso edge-tts: regola di prompt e pulizia markdown.

Ogni specialista (FS, Gmail, futuri) manda `parts[].text` al TTS. Markdown e
etichette da schermo (`**Da:**`, trattini, parentesi) diventano rumore a voce.
`SPOKEN_REPLY_RULE` sta in ogni system prompt; `prepare_spoken_text` è la rete
di sicurezza se il modello ignora la regola. Side-effect: nessuno.
"""

from __future__ import annotations

import re

# Un solo testo per tutti gli spec: niente drift FS vs Gmail vs agenti nuovi.
# Esempio solo “giusto”: un esempio sbagliato con asterischi verrebbe copiato.
SPOKEN_REPLY_RULE = (
    "Ogni tua frase la legge un TTS: scrivi come si parla, non come si legge "
    "su schermo. Niente markdown, asterischi, trattini da elenco, parentesi di "
    "markup né etichette con due punti come Da o Oggetto. Liste in frasi "
    "ordinali. Giusto: La prima è da Mario, oggetto Fattura. La seconda è da "
    "Anna, oggetto Riunione."
)

# Grassetto markdown: **Da:** → Da: (poi le etichette perdono i due punti).
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
# Etichette da elenco email / analoghi: il TTS leggerebbe “da due punti”.
_LABEL_RE = re.compile(r"(?i)\b(da|oggetto|from|subject)\s*:\s*")
# Trattino usato come separatore di campi (“mittente - oggetto”), non i trattini
# dentro una parola o un dominio.
_FIELD_DASH_RE = re.compile(r"\s+-\s+")


def prepare_spoken_text(text: str) -> str:
    """Toglie markup da schermo prima del TTS; mittenti e oggetti restano.

    Non è una riscrittura semantica: se Gemini ha già prodotto frasi ordinali
    pulite, l'output coincide col testo (a spazi collassati). Side-effect: nessuno.
    """
    # Prima il grassetto: così **Da:** diventa Da: e il passo etichette lo prende.
    out = _BOLD_RE.sub(r"\1", text)
    # Underscore markdown e asterischi rimasti (elenco * o corsivo non chiuso).
    out = out.replace("__", "").replace("*", "")
    # Da: / Oggetto: → stessa parola senza due punti; keep-case abbassato.
    out = _LABEL_RE.sub(lambda match: match.group(1).casefold() + " ", out)
    # Separatore di campi → pausa da virgola, più naturale di “trattino”.
    out = _FIELD_DASH_RE.sub(", ", out)
    # Parentesi: edge-tts le nomina; lo spazio tiene le parole staccate.
    out = out.replace("(", " ").replace(")", " ")
    # Newline da elenco markdown e spazi multipli → una sola pausa.
    return " ".join(out.split())
