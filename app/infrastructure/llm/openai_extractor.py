"""LLM adapter for any OpenAI-compatible chat-completions API.

Works with OpenAI, Azure OpenAI gateways, OpenRouter, vLLM, Ollama's
OpenAI endpoint, etc. — anything speaking POST {base_url}/chat/completions.

Hard guarantees required by the domain port:
  * returns ONLY Pydantic-validated `ExtractedInvoiceData`;
  * raises `LLMExtractionError` otherwise;
  * configurable timeout + bounded retries with exponential backoff;
  * the API key never appears in logs or error messages.
"""

import base64
import json
import logging
import re
import time
from collections.abc import Sequence

import httpx
from pydantic import SecretStr, ValidationError

from app.domain.exceptions import LLMExtractionError
from app.domain.services.invoice_extractor import InvoiceExtractor
from app.domain.value_objects.extracted_invoice import ExtractedInvoiceData
from app.infrastructure.llm.rules_extractor import ensure_items_payload

logger = logging.getLogger(__name__)

_MAX_TEXT_CHARS = 100_000          # guard runaway prompts / token costs
_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}
_BACKOFF_CAP_SECONDS = 8.0
_RATE_LIMIT_MIN_SLEEP = 30.0
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)
_RETRY_IN_RE = re.compile(r"retry in ([\d.]+)\s*s", re.I)

_SYSTEM_PROMPT = """You are an invoice data extraction engine.
From the document text below (and the attached page images, if any),
return ONE JSON object and nothing else.

Required JSON shape:
{
  "number": "<invoice number>",
  "date": "<issue date, YYYY-MM-DD>",
  "due_date": "<due date YYYY-MM-DD or null>",
  "currency": "<ISO-4217 code, e.g. COP, EUR, USD>",
  "subtotal": <sum of all item totals, net of tax>,
  "tax": <total tax amount>,
  "total": <grand total = subtotal + tax>,
  "supplier": {
    "name": "...", "tax_id": "...", "address": "...",
    "phone": "...", "email": "..."
  },
  "items": [
    {"description": "...", "quantity": <num>, "unit_price": <num>,
     "tax": <line tax>, "total": <NET line total: quantity * unit_price>}
  ]
}

Rules:
- Prefer the text layer; use page images to resolve OCR noise, tables or
  labels whose value sits on a separate line.
- Use plain numbers (no currency symbols, no thousands separators).
- IMPORTANT - money formats: Colombian/Latam invoices use '.' as THOUSANDS
  separator and ',' as decimal (e.g. "$162.000" = 162000; "$47.799,77" =
  47799.77). Amounts like "1.234.567" are one million two hundred... Keep
  the magnitude EXACTLY as printed: never divide or rescale amounts.
- Dates must be ISO-style YYYY-MM-DD or null when absent.
- item.total is NET (quantity x unit_price); tax is separate.
- subtotal must equal the sum of every item.total; total = subtotal + tax.
- Omit fields you truly cannot read rather than inventing values.
"""


class OpenAICompatibleInvoiceExtractor(InvoiceExtractor):
    def __init__(
        self,
        *,
        api_key: str | SecretStr,
        model: str,
        base_url: str | None = None,
        timeout_seconds: float = 60.0,
        temperature: float = 0.0,
        max_attempts: int = 3,
        transport: httpx.BaseTransport | None = None,  # test seam
    ) -> None:
        raw_key = api_key.get_secret_value() if isinstance(api_key, SecretStr) else api_key
        self._model = model
        self._temperature = temperature
        self._max_attempts = max(1, max_attempts)
        # Self-hosted gateways legitimately run without an API key;
        # an empty Bearer value makes httpx raise IllegalHeaderError.
        headers = {"Authorization": f"Bearer {raw_key}"} if raw_key.strip() else {}
        self._client = httpx.Client(
            base_url=(base_url or "https://api.openai.com/v1").rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            transport=transport,
        )

    # ------------------------------------------------------------------ public
    def extract(
        self,
        document_text: str,
        images: Sequence[bytes] | None = None,
    ) -> ExtractedInvoiceData:
        prompt = document_text[:_MAX_TEXT_CHARS]
        page_images = [img for img in (images or []) if img]
        last_error: Exception | None = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                content = self._request_chat(prompt, page_images)
                payload = self._parse_json(content)
                self._ensure_items_payload(payload)
                return ExtractedInvoiceData.model_validate(payload)
            except _RetryableError as exc:
                last_error = exc
                rate_limited = isinstance(exc, _RateLimitedError)
                if attempt < self._max_attempts:
                    sleep_for = self._backoff(attempt)
                    if rate_limited:
                        # 429 windows are ~1 min: short bursts only burn quota.
                        sleep_for = max(
                            sleep_for,
                            getattr(exc, "retry_after", None) or _RATE_LIMIT_MIN_SLEEP,
                        )
                        logger.warning(
                            "Límite de cuota (429); esperando %.1fs antes del "
                            "intento %s/%s", sleep_for, attempt + 1,
                            self._max_attempts,
                        )
                    else:
                        logger.warning(
                            "LLM call failed (attempt %s/%s): %s",
                            attempt, self._max_attempts, exc,
                        )
                    time.sleep(sleep_for)
            except (json.JSONDecodeError, ValidationError) as exc:
                # Malformed/invalid output may improve on retry with same input.
                last_error = exc
                logger.warning(
                    "LLM returned invalid structured output "
                    "(attempt %s/%s): %s: %s",
                    attempt, self._max_attempts, type(exc).__name__,
                    str(exc)[:300],
                )
                if attempt < self._max_attempts:
                    time.sleep(self._backoff(attempt))

        raise LLMExtractionError(
            f"LLM extraction failed after {self._max_attempts} attempts: "
            f"{type(last_error).__name__}: {str(last_error)[:200]}"
        )

    # ------------------------------------------------------------------ internals
    @staticmethod
    def _ensure_items_payload(payload: dict) -> None:
        """Vision models sometimes omit line items on summary-only documents."""
        ensure_items_payload(payload)

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        header = response.headers.get("retry-after", "").strip()
        if header.replace(".", "", 1).isdigit():
            return float(header)
        match = _RETRY_IN_RE.search(response.text[:800])
        return float(match.group(1)) + 2.0 if match else None

    def _request_chat(self, prompt: str, images: Sequence[bytes]) -> str:
        body = self._build_body(prompt, images, use_json_mode=True)
        try:
            response = self._client.post("/chat/completions", json=body)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise _RetryableError(f"network error: {type(exc).__name__}", cause=exc) from exc

        # Some OpenAI-compatible backends reject JSON mode outright; degrade
        # gracefully instead of failing the whole extraction.
        if response.status_code == 400 and "response_format" in response.text.lower():
            logger.info("Backend rechazo response_format json_object; reintentando sin el")
            response = self._client.post(
                "/chat/completions",
                json=self._build_body(prompt, images, use_json_mode=False),
            )

        if response.status_code == 429:
            raise _RateLimitedError(self._retry_after_seconds(response))
        if response.status_code in _RETRYABLE_STATUS:
            raise _RetryableError(f"HTTP {response.status_code} from LLM API")
        if response.status_code >= 400:
            # Auth/quota/request errors are permanent; retrying won't help.
            raise LLMExtractionError(
                f"LLM API rejected request (HTTP {response.status_code}): "
                f"{response.text[:200]}"
            )

        try:
            message = response.json()["choices"][0]["message"]
        except (KeyError, IndexError, ValueError) as exc:
            raise _RetryableError("malformed response envelope", cause=exc) from exc
        choice = message.get("content") or ""
        if not choice.strip():
            # Algunos servidores (plantillas thinking) enrutan la respuesta
            # completa a un campo "reasoning" y dejan content vacio; el JSON
            # final vive alli.
            choice = message.get("reasoning") or ""
        return choice

    def _build_body(
        self, prompt: str, images: Sequence[bytes], *, use_json_mode: bool
    ) -> dict:
        user_content: list[dict] = [
            {"type": "text", "text": f"Invoice document text:\n\n{prompt}"}
        ]
        for image in images:
            user_content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{base64.b64encode(image).decode('ascii')}",
                    "detail": "high",
                },
            })
        body: dict = {
            "model": self._model,
            "temperature": self._temperature,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        }
        if use_json_mode:
            body["response_format"] = {"type": "json_object"}
        if not images:
            # Text-only call: plain string content maximizes compatibility
            # with minimal OpenAI-compatible servers.
            body["messages"][1]["content"] = user_content[0]["text"]
        return body

    @staticmethod
    def _parse_json(content: str) -> dict:
        cleaned = content.strip()
        fenced = _FENCE_RE.match(cleaned)
        if fenced:
            cleaned = fenced.group(1)
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            # Reasoning models may wrap the JSON in prose; recover the
            # outermost object before giving up.
            start, end = cleaned.find("{"), cleaned.rfind("}")
            if start < 0 or end <= start:
                raise
            payload = json.loads(cleaned[start : end + 1])
        if not isinstance(payload, dict):
            raise ValueError("LLM JSON root must be an object")
        return payload

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(0.5 * (2 ** (attempt - 1)), _BACKOFF_CAP_SECONDS)


class _RetryableError(Exception):
    """Internal marker for transport/server-side failures worth retrying."""

    def __init__(self, message: str, *, cause: Exception | None = None) -> None:
        super().__init__(message)
        self.cause = cause or self


class _RateLimitedError(_RetryableError):
    """HTTP 429 with an optional server-advised wait (seconds)."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__(f"HTTP 429 from LLM API (retry after {retry_after}s)")
        self.retry_after = retry_after


__all__ = ["OpenAICompatibleInvoiceExtractor"]
