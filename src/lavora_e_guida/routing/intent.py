"""Intent classifier minimale (Phase 2).

Etichette iniziali per esercitare il loop — non congelano la topologia
multi-agente (decisione in docs/phase2_topology.md dopo il benchmark).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from lavora_e_guida.llm.local_ollama import LocalOllama, OllamaError


class IntentLabel(str, Enum):
    """Domini grezzi del router vocale (estendibili post-eval)."""

    SYSTEM_FILE = "SYSTEM_FILE"
    CURSOR = "CURSOR"
    WEB_EMAIL = "WEB_EMAIL"
    GENERAL = "GENERAL"


# Prompt 3B-safe: niente placeholder <LABEL>, chiavi fisse, un esempio reale.
_SYSTEM_PROMPT = (
    "Sei un classificatore di intent per un assistente vocale in auto. "
    "Rispondi ESCLUSIVAMENTE con un oggetto JSON valido. "
    "Nessun markdown, nessun testo fuori dal JSON.\n\n"
    'SCHEMA: {"intent": "string", "confidence": 0.0}\n'
    "intent ammessi: SYSTEM_FILE, CURSOR, WEB_EMAIL, GENERAL.\n"
    "SYSTEM_FILE = file, cartelle, path, copia/sposta/elimina.\n"
    "CURSOR = codice, repository, Cursor IDE, debug, refactor.\n"
    "WEB_EMAIL = ricerca web, browse, email, Gmail, calendario.\n"
    "GENERAL = tutto il resto.\n\n"
    "REGOLE TASSATIVE:\n"
    "1. Emetti UN SOLO oggetto JSON per risposta.\n"
    "2. confidence deve essere un numero tra 0 e 1.\n\n"
    "ESEMPIO CORRETTO:\n"
    "Utente: elenca i file nella cartella documenti\n"
    'JSON: {"intent": "SYSTEM_FILE", "confidence": 0.9}'
)


class SupportsGenerate(Protocol):
    """Protocollo minimo per iniettare mock nei test senza Ollama reale."""

    model: str

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        format_json: bool = False,
        options: dict | None = None,
    ) -> str: ...


@dataclass(frozen=True)
class IntentResult:
    """Esito classificazione: etichetta + confidenza + provenienza."""

    label: IntentLabel
    confidence: float
    # "ollama" | "heuristic" | "fallback" — utile a logging e TTS di status.
    source: str
    raw: str = ""


def _clamp_confidence(value: object) -> float:
    """Normalizza confidence in [0, 1]; valori sporchi → 0.5 prudente."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.5
    return max(0.0, min(1.0, number))


def _parse_label(raw: object) -> IntentLabel | None:
    """Mappa stringa modello → enum; None se sconosciuta."""
    if not isinstance(raw, str):
        return None
    key = raw.strip().upper().replace("-", "_").replace(" ", "_")
    try:
        return IntentLabel(key)
    except ValueError:
        return None


def classify_heuristic(user_text: str) -> IntentResult:
    """Fallback keyword IT/EN quando Ollama è offline o JSON invalido.

    Non sostituisce il modello: serve a non bloccare il loop in auto
    (meglio GENERAL sbagliato che silenzio).
    """
    text = user_text.casefold()

    # Pattern filesystem: path, file, cartella, comandi tipici.
    if re.search(
        r"\b(file|cartella|directory|path|percorso|copia|elimina|sposta|"
        r"rinomina|ls|mkdir|rm |chmod)\b",
        text,
    ):
        return IntentResult(IntentLabel.SYSTEM_FILE, 0.55, "heuristic")

    # Cursor / codice / IDE.
    if re.search(
        r"\b(cursor|codice|repo|repository|refactor|debug|pull request|"
        r"commit|ide|typescript|python)\b",
        text,
    ):
        return IntentResult(IntentLabel.CURSOR, 0.55, "heuristic")

    # Web / email / calendario.
    if re.search(
        r"\b(email|gmail|mail|cerca sul web|google|duckduckgo|calendario|"
        r"agenda|browser|naviga|sito)\b",
        text,
    ):
        return IntentResult(IntentLabel.WEB_EMAIL, 0.55, "heuristic")

    # Default sicuro del piano: GENERAL.
    return IntentResult(IntentLabel.GENERAL, 0.4, "heuristic")


class IntentClassifier:
    """Classifica testo utente → IntentLabel via Ollama + fallback."""

    def __init__(self, llm: SupportsGenerate | None = None) -> None:
        # None → solo heuristic (utile offline / test senza daemon).
        self._llm = llm

    def classify(self, user_text: str) -> IntentResult:
        """Classifica; mai solleva se l'LLM fallisce (→ heuristic/fallback)."""
        stripped = user_text.strip()
        # Input vuoto: non chiamiamo il modello (latenza e rumore inutili).
        if not stripped:
            return IntentResult(IntentLabel.GENERAL, 0.0, "fallback", raw="")

        if self._llm is None:
            return classify_heuristic(stripped)

        try:
            # temperature bassa: intent deve essere stabile tra turni simili.
            raw = self._llm.generate(
                stripped,
                system=_SYSTEM_PROMPT,
                format_json=True,
                options={"temperature": 0.0, "num_predict": 64},
            )
            # LocalOllama.extract_json_object è statico; se llm è mock senza
            # il metodo, parsiano con la stessa helper importata.
            parsed = LocalOllama.extract_json_object(raw)
            label = _parse_label(parsed.get("intent"))
            if label is None:
                # Modello ha risposto JSON ma label invalida → heuristic.
                return classify_heuristic(stripped)
            confidence = _clamp_confidence(parsed.get("confidence", 0.7))
            return IntentResult(label, confidence, "ollama", raw=raw)
        except (OllamaError, OSError, ValueError):
            # Qualsiasi glitch → heuristic; il loop resta usable hands-free.
            return classify_heuristic(stripped)


def stub_response_for(intent: IntentLabel, user_text: str) -> str:
    """Risposta stub Phase 2: conferma intent senza eseguire tools reali."""
    # Messaggi in italiano (direttiva progetto / TTS utente).
    snippets = {
        IntentLabel.SYSTEM_FILE: (
            "Ho classificato la richiesta come file di sistema. "
            "In Phase 3 collegherò i tool filesystem. "
            f"Hai detto: {user_text}"
        ),
        IntentLabel.CURSOR: (
            "Ho classificato la richiesta come Cursor o codice. "
            "In Phase 3 userò la CLI Cursor in modalità Ask. "
            f"Hai detto: {user_text}"
        ),
        IntentLabel.WEB_EMAIL: (
            "Ho classificato la richiesta come web o email. "
            "I tool di ricerca e Google Workspace arriveranno più avanti. "
            f"Hai detto: {user_text}"
        ),
        IntentLabel.GENERAL: (
            "Intent generale. Per ora rispondo solo con uno stub. "
            f"Hai detto: {user_text}"
        ),
    }
    return snippets[intent]
