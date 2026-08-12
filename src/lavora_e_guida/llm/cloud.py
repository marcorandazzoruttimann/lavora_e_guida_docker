"""Client HTTP verso OpenAI Chat Completions (senza SDK ufficiale).

Stesso contratto di `LocalOllama` usato dal lab (`chat` / `close` / `ping`):
il loop `SupportsChat` può scambiare provider senza cambiare il protocollo.
Auth: header `Authorization: Bearer …` da `OPENAI_API_KEY` o argomento esplicito.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from lavora_e_guida.llm.errors import LLMError

# Endpoint pubblico di default; override utile per proxy / Azure-compat.
_DEFAULT_BASE_URL = "https://api.openai.com/v1"
# Modello economico e affidabile per tool-calling JSON nel sandbox lab.
_DEFAULT_MODEL = "gpt-4o-mini"


class OpenAIError(LLMError):
    """Errore di trasporto, auth o payload non valido da OpenAI."""


def _openai_error_fields(response: httpx.Response) -> tuple[str | None, str | None, str | None]:
    """Estrae message/type/code dal JSON OpenAI; (None,None,None) se non parsabile."""
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return None, None, None
    if not isinstance(payload, dict):
        return None, None, None
    err = payload.get("error") or {}
    if not isinstance(err, dict):
        return None, None, None
    msg = err.get("message")
    typ = err.get("type")
    code = err.get("code")
    return (
        str(msg) if msg is not None else None,
        str(typ) if typ is not None else None,
        str(code) if code is not None else None,
    )

def _openai_http_error_message(response: httpx.Response) -> str:
    """Messaggio parlante/italiano per TTS e lab: quota ≠ rate-limit generico."""
    status = response.status_code
    err_msg, err_type, err_code = _openai_error_fields(response)
    # Crediti/billing: OpenAI manda 429 con insufficient_quota (non RPM).
    if (
        err_type == "insufficient_quota"
        or err_code in ("insufficient_quota", "credit_balance_exhausted")
        or (err_msg and "credits remaining" in err_msg.casefold())
    ):
        return (
            "Crediti OpenAI esauriti: ricarica il billing su "
            "platform.openai.com/settings/organization/billing "
            "oppure usa --llm ollama."
        )
    # Rate-limit vero (RPM/TPM): retry-after presente o messaggio rate limit.
    retry_after = response.headers.get("retry-after")
    if status == 429:
        if retry_after:
            return (
                f"Rate limit OpenAI (429): riprova tra circa {retry_after}s "
                f"({err_code or err_type or 'rate_limit'})."
            )
        return (
            f"Rate limit o quota OpenAI (429)"
            + (f": {err_msg}" if err_msg else ".")
        )
    if status == 401:
        return "Chiave OpenAI non valida (401): verifica OPENAI_API_KEY."
    # Fallback: status + snippet body API (senza stack httpx).
    detail = err_msg or (response.text or "")[:200] or "nessun dettaglio"
    return f"chat fallita HTTP {status}: {detail}"

class OpenAIChat:
    """Wrapper minimale su `POST /v1/chat/completions` via httpx.

    Contratto allineato a `LocalOllama.chat`: restituisce testo plain
    (`choices[0].message.content`); il parsing JSON resta nel chiamante.
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
        resolved = (api_key if api_key is not None else os.environ.get("OPENAI_API_KEY")) or ""
        self.api_key = resolved.strip()
        # base_url senza slash finale: path relativi (`/chat/completions`) prevedibili.
        self.base_url = base_url.rstrip("/")
        # Tag modello OpenAI (es. gpt-4o-mini); override CLI via factory.
        self.model = model
        # Timeout alto: cold start / rate limit transienti non devono abortire subito.
        self.timeout = timeout
        # Client iniettabile → MockTransport nei test senza rete reale.
        self._client = client
        self._owns_client = client is None

    def _auth_headers(self) -> dict[str, str]:
        # Fail early: senza Bearer ogni chiamata 401; messaggio chiaro al lab.
        if not self.api_key:
            raise OpenAIError(
                "OPENAI_API_KEY assente: imposta la variabile d'ambiente o passa api_key="
            )
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _get_client(self) -> httpx.Client:
        # Lazy: import del modulo non apre socket verso api.openai.com.
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
            # Stesso endpoint usato da molti health-check: leggero e auth-aware.
            response = self._get_client().get("/models", headers=self._auth_headers())
            
            return response.status_code == 200
        except (httpx.HTTPError, OpenAIError):
            # Rete giù, chiave vuota, timeout: il lab esce con messaggio chiaro.
            return False

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        format_json: bool = False,
        options: dict[str, Any] | None = None,
    ) -> str:
        """Chat non-stream; legge `choices[0].message.content` come stringa.

        Se `format_json=True` richiede `response_format: json_object` (equivalente
        di `format=json` Ollama). `options["temperature"]` → campo top-level.
        """
        # Body allineato a Chat Completions: stream=false per risposta unica.
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }
        # JSON mode: obbliga un oggetto; il system prompt del lab deve menzionarlo.
        if format_json:
            body["response_format"] = {"type": "json_object"}
        # Solo temperature oggi usata dal lab; altri option Ollama non mappati.
        if options and "temperature" in options:
            body["temperature"] = options["temperature"]

        

        try:
            response = self._get_client().post(
                "/chat/completions",
                json=body,
                headers=self._auth_headers(),
            )
            
            # Non raise_for_status grezzo: quota 429 ≠ rate-limit; messaggio chiaro.
            if response.status_code >= 400:
                
                raise OpenAIError(mapped or _openai_http_error_message(response))
        except OpenAIError:
            raise
        except httpx.HTTPError as exc:
            
            # Wrappiamo: il chiamante cattura LLMError/OpenAIError senza httpx.
            raise OpenAIError(f"chat fallita: {exc}") from exc

        data = response.json()
        # Forma tipica: choices[0].message.content (stringa).
        choices = data.get("choices") or []
        if not choices:
            raise OpenAIError(f"choices assente o vuoto: {data!r}")
        message = choices[0].get("message") or {}
        content = message.get("content")
        if not isinstance(content, str):
            raise OpenAIError(f"message.content assente: {data!r}")
        
        return content

# Alias retrocompatibile: import storici `CloudLLM` puntano al client reale.
CloudLLM = OpenAIChat
