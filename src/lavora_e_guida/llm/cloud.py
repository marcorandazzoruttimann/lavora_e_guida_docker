"""Client HTTP verso Google Gemini generateContent (senza SDK ufficiale).

Contratto del loop vocale: `chat` restituisce `LlmTurn` (testo e/o functionCall),
non una stringa JSON `{"tool","args"}`. Auth: header `x-goog-api-key`.
Ollama non usa questo client: il loop di prodotto è solo Gemini.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import quote

import httpx

from lavora_e_guida.llm.errors import LLMError
from lavora_e_guida.llm.turn import FunctionCall, LlmTurn
from lavora_e_guida.llm.usage import TokenUsage, parse_gemini_usage

# Endpoint pubblico Google AI Studio (v1beta).
_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# Default economico e veloce per function calling nativo nel loop vocale.
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
            "aistudio.google.com."
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
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None,
    options: dict[str, Any] | None,
) -> dict[str, Any]:
    """Converte la storia del loop nel body `generateContent`.

    - system → systemInstruction (concatenati se più di uno)
    - user/assistant con `content` testo → parts[].text
    - assistant con `function_calls` → parts[].functionCall
    - user con `function_response` → parts[].functionResponse

    Con `tools` si attiva AUTO function calling e **non** si imposta
    `responseMimeType: application/json` (confligge con functionCall).
    """
    system_chunks: list[str] = []
    contents: list[dict[str, Any]] = []

    for raw in messages:
        # Accettiamo solo dict; skip difensivi su elementi spuri.
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "").strip().casefold()
        # System: Gemini lo vuole fuori da contents, mai come turno user/model.
        if role == "system":
            content = raw.get("content")
            if isinstance(content, str) and content.strip():
                system_chunks.append(content.strip())
            continue

        # assistant OpenAI → model Gemini; resto (user/altro) → user.
        gemini_role = "model" if role == "assistant" else "user"
        parts = _message_to_gemini_parts(raw)
        # Turno senza parts (dict vuoto): non mandiamo contents invalidi.
        if not parts:
            continue
        contents.append({"role": gemini_role, "parts": parts})

    body: dict[str, Any] = {"contents": contents}
    if system_chunks:
        # Un solo blocco systemInstruction: più system messaggi uniti.
        body["systemInstruction"] = {
            "parts": [{"text": "\n\n".join(system_chunks)}],
        }

    # Tool nativi: catalogo Impesud già avvolto da to_gemini_tools().
    if tools:
        body["tools"] = tools
        # AUTO: Gemini sceglie testo parlato oppure functionCall.
        body["toolConfig"] = {
            "functionCallingConfig": {"mode": "AUTO"},
        }

    generation_config: dict[str, Any] = {}
    if options and "temperature" in options:
        generation_config["temperature"] = options["temperature"]
    if generation_config:
        body["generationConfig"] = generation_config

    return body


def _copy_thought_signature(
    part: dict[str, Any], raw_fc: dict[str, Any]
) -> str | None:
    """Copia il blob `thoughtSignature` senza alterarlo.

    Gemini 3 valida che la prima functionCall del turno corrente porti la
    stessa firma ricevuta. In REST sta sulla part (sorella di functionCall),
    non dentro `args`. Accettiamo camelCase e snake_case; in difesa anche
    il campo dentro l'oggetto call. Non strip/decode: il blob è opaco.
    """
    # Prima la part (forma ufficiale), poi l'oggetto call (payload atipici).
    for source in (part, raw_fc):
        for key in ("thoughtSignature", "thought_signature"):
            value = source.get(key)
            # Solo stringhe non vuote: None / tipi spuri non vanno in history.
            if isinstance(value, str) and value:
                return value
    return None


def _message_to_gemini_parts(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """Parts di un turno: testo, functionCall o functionResponse (mutualmente usabili).

    Se la history ha `thought_signature` sulla call, la rimettiamo sulla part
    come `thoughtSignature` (non dentro `functionCall`): altrimenti Gemini 3
    risponde 400 al generateContent dopo l'esecuzione del tool.
    """
    parts: list[dict[str, Any]] = []
    # Call del modello: il loop ne tiene una (la prima eseguita).
    calls = raw.get("function_calls")
    if isinstance(calls, list):
        for item in calls:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            args = item.get("args")
            fc: dict[str, Any] = {
                "name": name,
                "args": args if isinstance(args, dict) else {},
            }
            # id Gemini: va riecheggiato nella functionResponse se c'era.
            call_id = item.get("id")
            if isinstance(call_id, str) and call_id.strip():
                fc["id"] = call_id.strip()
            # Firma sulla part, non dentro functionCall: il validator guarda lì.
            part: dict[str, Any] = {"functionCall": fc}
            signature = item.get("thought_signature") or item.get("thoughtSignature")
            if isinstance(signature, str) and signature:
                part["thoughtSignature"] = signature
            parts.append(part)

    response = raw.get("function_response")
    if isinstance(response, dict):
        name = str(response.get("name") or "").strip()
        if name:
            fr: dict[str, Any] = {
                "name": name,
                # response è un oggetto JSON: il modello legge `result` parlante.
                "response": {"result": str(response.get("result") or "")},
            }
            call_id = response.get("id")
            if isinstance(call_id, str) and call_id.strip():
                fr["id"] = call_id.strip()
            parts.append({"functionResponse": fr})

    content = raw.get("content")
    if isinstance(content, str) and content:
        parts.append({"text": content})
    return parts


def parse_gemini_turn(data: dict[str, Any]) -> LlmTurn:
    """Legge testo e functionCall da candidates[0].content.parts.

    Accetta sia camelCase (`functionCall`) sia snake_case. Copia
    `thoughtSignature` dalla part (o, in difesa, dalla call) senza alterare
    il blob. Turno senza text né call → GeminiError (payload rotto, non
    “l'utente ha taciuto”).
    """
    candidates = data.get("candidates") or []
    if not candidates:
        raise GeminiError(f"candidates assente o vuoto: {data!r}")
    first = candidates[0] if isinstance(candidates[0], dict) else {}
    content = first.get("content") or {}
    parts = content.get("parts") or []
    texts: list[str] = []
    calls: list[FunctionCall] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        text = part.get("text")
        if isinstance(text, str):
            texts.append(text)
        raw_fc = part.get("functionCall") or part.get("function_call")
        if isinstance(raw_fc, dict):
            name = str(raw_fc.get("name") or "").strip()
            if not name:
                continue
            args_raw = raw_fc.get("args")
            args = args_raw if isinstance(args_raw, dict) else {}
            call_id = raw_fc.get("id")
            # Firma da riecheggiare sul prossimo generateContent; blob intatto.
            thought_signature = _copy_thought_signature(part, raw_fc)
            calls.append(
                FunctionCall(
                    name=name,
                    args=args,
                    call_id=str(call_id).strip() if call_id else None,
                    thought_signature=thought_signature,
                )
            )
    if not texts and not calls:
        raise GeminiError(f"parts senza text né functionCall: {data!r}")
    return LlmTurn(text="".join(texts), function_calls=tuple(calls))


class GeminiChat:
    """Wrapper su `POST /v1beta/models/{model}:generateContent` via httpx.

    `chat` restituisce `LlmTurn` (testo TTS e/o functionCall). Side-effect:
    `last_usage` dopo ogni chiamata (campi assenti → 0).
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
        # Telemetria: ultimo conteggio token; 0 finché non arriva un payload.
        self.last_usage = TokenUsage()

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
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        options: dict[str, Any] | None = None,
    ) -> LlmTurn:
        """Chat non-stream; legge text e/o functionCall da candidates[0].

        `tools` è l'output di `to_gemini_tools` (functionDeclarations).
        `options["temperature"]` → generationConfig. Niente JSON mime se
        ci sono tool: altrimenti Gemini non emette functionCall.
        """
        body = _messages_to_gemini_body(
            messages, tools=tools, options=options
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
        # Side-effect telemetria: last_usage senza cambiare il return LlmTurn.
        self.last_usage = parse_gemini_usage(data)
        return parse_gemini_turn(data)


# Alias retrocompatibile: import storici `CloudLLM` puntano al client reale.
CloudLLM = GeminiChat
