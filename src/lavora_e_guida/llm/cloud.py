"""Client HTTP verso Google Gemini generateContent (senza SDK ufficiale).

Stesso contratto di `LocalOllama` usato dal lab (`chat` / `close` / `ping`):
il loop `SupportsChat` può scambiare provider senza cambiare il protocollo.
Auth: header `x-goog-api-key` da `GEMINI_API_KEY` o argomento esplicito.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import quote

import httpx

from lavora_e_guida.llm.errors import LLMError

# Endpoint pubblico Google AI Studio (v1beta).
_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# Default economico e veloce per tool-calling JSON nel sandbox lab.
_DEFAULT_MODEL = "gemini-3.5-flash"


class GeminiError(LLMError):
    """Errore di trasporto, auth o payload non valido da Gemini."""


def _gemini_error_fields(response: httpx.Response) -> tuple[str | None, str | None, int | None]:
    """Estrae message/status/code dal JSON Gemini; (None,None,None) se non parsabile."""
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return None, None, None
    if not isinstance(payload, dict):
        return None, None, None
    # Forma tipica: {"error": {"code": 401, "message": "...", "status": "UNAUTHENTICATED"}}
    err = payload.get("error") or {}
    if not isinstance(err, dict):
        return None, None, None
    msg = err.get("message")
    status = err.get("status")
    code = err.get("code")
    return (
        str(msg) if msg is not None else None,
        str(status) if status is not None else None,
        int(code) if isinstance(code, int) else None,
    )


def _gemini_http_error_message(response: httpx.Response) -> str:
    """Messaggio parlante/italiano per TTS e lab: quota ≠ rate-limit generico."""
    status = response.status_code
    err_msg, err_status, err_code = _gemini_error_fields(response)
    msg_fold = (err_msg or "").casefold()
    # Crediti/billing: RESOURCE_EXHAUSTED con messaggio quota, non solo RPM.
    if (
        err_status == "RESOURCE_EXHAUSTED"
        and ("quota" in msg_fold or "billing" in msg_fold)
    ) or "exceeded your current quota" in msg_fold:
        return (
            "Quota Gemini esaurita: verifica billing/limiti su "
            "aistudio.google.com oppure usa --llm ollama."
        )
    # Rate-limit vero (RPM/TPM): retry-after presente o RESOURCE_EXHAUSTED generico.
    retry_after = response.headers.get("retry-after")
    if status == 429 or err_status == "RESOURCE_EXHAUSTED":
        if retry_after:
            return (
                f"Rate limit Gemini (429): riprova tra circa {retry_after}s "
                f"({err_status or err_code or 'rate_limit'})."
            )
        return (
            f"Rate limit o quota Gemini (429)"
            + (f": {err_msg}" if err_msg else ".")
        )
    if status in (401, 403) or err_status in ("UNAUTHENTICATED", "PERMISSION_DENIED"):
        return "Chiave Gemini non valida (401/403): verifica GEMINI_API_KEY."
    # Fallback: status + snippet body API (senza stack httpx).
    detail = err_msg or (response.text or "")[:200] or "nessun dettaglio"
    return f"chat fallita HTTP {status}: {detail}"


def _messages_to_gemini_body(
    messages: list[dict[str, str]],
    *,
    format_json: bool,
    options: dict[str, Any] | None,
) -> dict[str, Any]:
    """Converte messaggi OpenAI-like (system/user/assistant) nel body generateContent.

    - system → systemInstruction.parts[].text (concatenati se più di uno)
    - user → contents role=user
    - assistant → contents role=model
    """
    system_chunks: list[str] = []
    contents: list[dict[str, Any]] = []

    for raw in messages:
        # Accettiamo solo dict con role/content stringa; skip difensivi.
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "").strip().casefold()
        content = raw.get("content")
        if not isinstance(content, str):
            continue
        # System: Gemini lo vuole fuori da contents.
        if role == "system":
            text = content.strip()
            if text:
                system_chunks.append(text)
            continue
        # assistant OpenAI → model Gemini; resto (user/altro) → user.
        gemini_role = "model" if role == "assistant" else "user"
        contents.append(
            {
                "role": gemini_role,
                "parts": [{"text": content}],
            }
        )

    body: dict[str, Any] = {"contents": contents}
    if system_chunks:
        # Un solo blocco systemInstruction: più system messaggi uniti.
        body["systemInstruction"] = {
            "parts": [{"text": "\n\n".join(system_chunks)}],
        }

    # generationConfig: JSON mime + temperature opzionale dal lab.
    generation_config: dict[str, Any] = {}
    if format_json:
        # Equivalente di response_format json_object / format=json Ollama.
        generation_config["responseMimeType"] = "application/json"
    if options and "temperature" in options:
        generation_config["temperature"] = options["temperature"]
    if generation_config:
        body["generationConfig"] = generation_config

    return body


def _extract_candidate_text(data: dict[str, Any]) -> str:
    """Legge il testo concatenato da candidates[0].content.parts[].text."""
    candidates = data.get("candidates") or []
    if not candidates:
        raise GeminiError(f"candidates assente o vuoto: {data!r}")
    content = candidates[0].get("content") or {}
    parts = content.get("parts") or []
    texts: list[str] = []
    for part in parts:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            texts.append(part["text"])
    if not texts:
        raise GeminiError(f"parts[].text assente: {data!r}")
    return "".join(texts)


class GeminiChat:
    """Wrapper minimale su `POST /v1beta/models/{model}:generateContent` via httpx.

    Contratto allineato a `LocalOllama.chat`: restituisce testo plain
    (parts concatenate); il parsing JSON resta nel chiamante.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = _DEFAULT_BASE_URL,
        model: str = _DEFAULT_MODEL,
        timeout: float = 120.0,
        client: httpx.Client | None = None,
    ) -> None:
        # Chiave esplicita ha priorità; altrimenti env (anche da `.env` caricato
        # a monte). Strip: evita spazi accidentali da export shell.
        resolved = (api_key if api_key is not None else os.environ.get("GEMINI_API_KEY")) or ""
        self.api_key = resolved.strip()
        # base_url senza slash finale: path relativi prevedibili.
        self.base_url = base_url.rstrip("/")
        # Tag modello Gemini (es. gemini-3.5-flash); override CLI via factory.
        self.model = model
        # Timeout alto: rete pubblica / transienti non devono abortire subito.
        self.timeout = timeout
        # Client iniettabile → MockTransport nei test senza rete reale.
        self._client = client
        self._owns_client = client is None

    def _auth_headers(self) -> dict[str, str]:
        # Fail early: senza chiave ogni chiamata 401/403; messaggio chiaro al lab.
        if not self.api_key:
            raise GeminiError(
                "GEMINI_API_KEY assente: imposta la variabile d'ambiente o passa api_key="
            )
        return {
            "x-goog-api-key": self.api_key,
            "Content-Type": "application/json",
        }

    def _get_client(self) -> httpx.Client:
        # Lazy: import del modulo non apre socket verso Google.
        if self._client is None:
            self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)
        return self._client

    def close(self) -> None:
        # Chiudiamo solo i client di nostra proprietà (non quelli iniettati nei test).
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def ping(self) -> bool:
        """True se `GET /models` risponde 200 (chiave valida / API raggiungibile)."""
        try:
            # List models: health-check leggero e auth-aware.
            response = self._get_client().get("/models", headers=self._auth_headers())
            return response.status_code == 200
        except (httpx.HTTPError, GeminiError):
            # Rete giù, chiave vuota, timeout: il lab esce con messaggio chiaro.
            return False

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        format_json: bool = False,
        options: dict[str, Any] | None = None,
    ) -> str:
        """Chat non-stream; legge il testo da candidates[0].content.parts.

        Se `format_json=True` richiede `responseMimeType: application/json`
        (equivalente di `format=json` Ollama). `options["temperature"]` → generationConfig.
        """
        body = _messages_to_gemini_body(
            messages, format_json=format_json, options=options
        )
        # Path con modello URL-encoded (evita rotture su tag con `/` o `:`).
        path = f"/models/{quote(self.model, safe='')}:generateContent"

        try:
            response = self._get_client().post(
                path,
                json=body,
                headers=self._auth_headers(),
            )
            # Non raise_for_status grezzo: quota 429 ≠ rate-limit; messaggio chiaro.
            if response.status_code >= 400:
                raise GeminiError(_gemini_http_error_message(response))
        except GeminiError:
            raise
        except httpx.HTTPError as exc:
            # Wrappiamo: il chiamante cattura LLMError/GeminiError senza httpx.
            raise GeminiError(f"chat fallita: {exc}") from exc

        data = response.json()
        if not isinstance(data, dict):
            raise GeminiError(f"risposta non-oggetto JSON: {data!r}")
        return _extract_candidate_text(data)


# Alias retrocompatibile: import storici `CloudLLM` puntano al client reale.
CloudLLM = GeminiChat
