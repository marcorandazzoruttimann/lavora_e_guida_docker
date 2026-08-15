"""Client HTTP verso Ollama locale.

Un solo modello alla volta per vincoli RAM (Ryzen 3 / ~4–12 Gi).
Niente SDK ufficiale: `httpx` su `/api/chat` e `/api/generate` così
possiamo mockare facilmente nei test e fallire soft se il daemon è giù.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from lavora_e_guida.llm.errors import LLMError
from lavora_e_guida.llm.usage import TokenUsage, parse_ollama_usage


class OllamaError(LLMError):
    """Errore di trasporto o payload non valido da Ollama."""


class LocalOllama:
    """Wrapper minimale sull'API HTTP di Ollama.

    Contratto: `generate` / `chat` restituiscono testo plain; il parsing
    JSON dell'intent resta in `routing.intent` (prompt corto + estrazione).
    Side-effect: `last_usage` dopo ogni generate/chat (campi assenti → 0).
    """

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "qwen2.5:3b",
        timeout: float = 120.0,
        client: httpx.Client | None = None,
    ) -> None:
        # base_url senza slash finale: concatena path in modo prevedibile.
        self.base_url = base_url.rstrip("/")
        # Nome tag Ollama (es. qwen2.5:3b); deve essere già pullato.
        self.model = model
        # Timeout alto: cold start modello piccolo può superare i 30s.
        self.timeout = timeout
        # Client iniettabile → MockTransport nei test senza rete reale.
        self._client = client
        self._owns_client = client is None
        # Telemetria: ultimo conteggio token; 0 finché non arriva un payload.
        self.last_usage = TokenUsage()

    def _get_client(self) -> httpx.Client:
        # Lazy: creiamo il client solo al primo uso (import non apre socket).
        if self._client is None:
            self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)
        return self._client

    def close(self) -> None:
        # Chiudiamo solo i client di nostra proprietà (non quelli iniettati).
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def ping(self) -> bool:
        """True se `/api/tags` risponde 200 (daemon raggiungibile)."""
        try:
            # Path relativo al base_url del client.
            response = self._get_client().get("/api/tags")
            return response.status_code == 200
        except httpx.HTTPError:
            # Daemon spento / firewall WSL: il loop userà fallback heuristic.
            return False

    def list_models(self) -> list[str]:
        """Nomi modello installati (campo `name` di ogni entry in `models`)."""
        response = self._get_client().get("/api/tags")
        # raise_for_status: errori HTTP non devono diventare lista vuota silente.
        response.raise_for_status()
        payload = response.json()
        models = payload.get("models") or []
        # Alcune versioni usano `name`, altre `model`: accettiamo entrambi.
        return [str(m.get("name") or m.get("model") or "") for m in models if m]

    def generate(
        self,
        prompt: str,
        *,
        system: str | None = None,
        format_json: bool = False,
        options: dict[str, Any] | None = None,
    ) -> str:
        """Completamento non-stream; opzionale `format=json` per classifier.

        Side-effect: carica il modello in RAM se non già resident.
        """
        # Body allineato alla docs Ollama: stream=false per risposta unica.
        body: dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
        }
        # System prompt separato: meglio per instruction-following sui piccoli.
        if system:
            body["system"] = system
        # format=json chiede al modello di emettere JSON valido (best-effort).
        if format_json:
            body["format"] = "json"
        # options tipiche: temperature bassa per classificazione deterministica.
        if options:
            body["options"] = options

        try:
            response = self._get_client().post("/api/generate", json=body)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Wrappiamo: il chiamante può fare fallback senza conoscere httpx.
            raise OllamaError(f"generate fallita: {exc}") from exc

        data = response.json()
        # Side-effect telemetria: last_usage senza cambiare il return str.
        self.last_usage = parse_ollama_usage(data)
        text = data.get("response")
        if not isinstance(text, str):
            raise OllamaError(f"campo response assente o non stringa: {data!r}")
        return text

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        format_json: bool = False,
        options: dict[str, Any] | None = None,
    ) -> str:
    #qui avviene interazione con Ollama: request e response via http locale
        """Chat non-stream; stesso contratto di `generate` sul campo `message.content`."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        if format_json:
            body["format"] = "json"
        if options:
            body["options"] = options

        try:
            response = self._get_client().post("/api/chat", json=body)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OllamaError(f"chat fallita: {exc}") from exc

        data = response.json()
        # Stesso contratto di generate: token sul payload, testo in return.
        self.last_usage = parse_ollama_usage(data)
        message = data.get("message") or {}
        content = message.get("content")
        if not isinstance(content, str):
            raise OllamaError(f"message.content assente: {data!r}")
        return content

    @staticmethod
    def extract_json_object(text: str) -> dict[str, Any]:
        """Estrae il primo oggetto JSON da testo eventualmente rumoroso.

        I modelli 2–3B a volte aggiungono prefissi; cerchiamo `{...}` e
        facciamo `json.loads`. Fallisce con OllamaError se non c'è JSON.
        """
        # Strip markdown fence comuni (` ```json `) senza dipendere da regex complesse.
        cleaned = text.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            # Rimuove prima e ultima riga fence se presenti.
            if len(lines) >= 2 and lines[-1].strip().startswith("```"):
                cleaned = "\n".join(lines[1:-1]).strip()
            elif lines[0].startswith("```"):
                cleaned = "\n".join(lines[1:]).strip()

        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end < 0 or end <= start:
            raise OllamaError(f"nessun oggetto JSON in: {text!r}")
        try:
            parsed = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise OllamaError(f"JSON non valido: {exc}; testo={text!r}") from exc
        if not isinstance(parsed, dict):
            raise OllamaError(f"JSON non-oggetto: {parsed!r}")
        return parsed
