"""HTTP client wrapper: the ONLY way Streamlit talks to the backend.

Architecture rule enforced here: the UI never touches PostgreSQL nor any
domain object — it exchanges JSON with FastAPI exclusively.
"""

import contextlib
import logging
import os
from typing import Any

import httpx
import streamlit as st

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0


class ApiError(Exception):
    """Friendly transport/application error surfaced to the UI."""

    def __init__(self, message: str, status_code: int | None = None,
                 code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@st.cache_resource
def get_api_client() -> "ApiClient":
    return ApiClient(
        base_url=os.environ.get("API_BASE_URL", "http://localhost:8000"),
    )


class ApiClient:
    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout)

    # ------------------------------------------------------------------ calls
    def upload(self, filename: str, content: bytes, content_type: str) -> dict[str, Any]:
        response = self._request(
            "POST",
            "/api/v1/invoices/upload",
            files={"file": (filename, content, content_type)},
            timeout=60.0,
        )
        return response.json()

    def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/invoices/{invoice_id}").json()

    def list_invoices(self, **params: Any) -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v not in (None, "", "ALL")}
        return self._request("GET", "/api/v1/invoices", params=clean).json()

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/v1/jobs/{job_id}").json()

    def stats(self) -> dict[str, Any]:
        return self._request(
            "GET",
            "/api/v1/dashboard/stats",
            timeout=DEFAULT_TIMEOUT,
        ).json()

    # ------------------------------------------------------------------ internals
    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            logger.error("API unreachable: %s", exc)
            raise ApiError(f"Cannot reach the API at '{self._client.base_url}'. "
                           f"Is the backend running?") from exc

        if response.status_code >= 400:
            payload = {}
            with contextlib.suppress(ValueError):
                payload = response.json()
            error = payload.get("error", {})
            raise ApiError(
                error.get("message") or f"HTTP {response.status_code}",
                status_code=response.status_code,
                code=error.get("code"),
            )
        return response


__all__ = ["ApiClient", "ApiError", "get_api_client"]
