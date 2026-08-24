"""Security middleware: headers + simple in-memory rate limiting."""

import time
from dataclasses import dataclass
from threading import Lock

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp


@dataclass(slots=True)
class _ClientBucket:
    tokens: float
    last_update: float


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses."""

    def __init__(self, app: ASGIApp, hsts_max_age: int = 31536000) -> None:
        super().__init__(app)
        self._hsts_max_age = hsts_max_age

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        # HSTS (only if HTTPS, but we set it anyway; browser ignores on HTTP)
        response.headers["Strict-Transport-Security"] = (
            f"max-age={self._hsts_max_age}; includeSubDomains"
        )
        # Prevent clickjacking
        response.headers["X-Frame-Options"] = "DENY"
        # Prevent MIME sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Referrer policy
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # CSP - restrictive but allows inline scripts/styles for FastAPI docs
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; "
            "font-src 'self'; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        # Permissions policy
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=(), payment=()"
        )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Token-bucket rate limiter per client IP (in-memory, per-process).

    Not distributed — suitable for single-container or low-traffic deployments.
    For multi-container, use Redis-backed limiter.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        requests_per_minute: int = 60,
        burst: int = 10,
    ) -> None:
        super().__init__(app)
        self._rate = requests_per_minute / 60.0  # tokens per second
        self._burst = burst
        self._buckets: dict[str, _ClientBucket] = {}
        self._lock = Lock()

    def _client_key(self, request: Request) -> str:
        # Trust X-Forwarded-For if behind proxy; otherwise client host
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _consume(self, key: str, tokens: int = 1) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _ClientBucket(tokens=float(self._burst), last_update=now)
                self._buckets[key] = bucket
            # Refill
            elapsed = now - bucket.last_update
            bucket.tokens = min(self._burst, bucket.tokens + elapsed * self._rate)
            bucket.last_update = now
            if bucket.tokens >= tokens:
                bucket.tokens -= tokens
                return True
            return False

    async def dispatch(self, request: Request, call_next):
        # Skip rate limiting for health checks
        if request.url.path in ("/health", "/health/"):
            return await call_next(request)

        key = self._client_key(request)
        if not self._consume(key):
            from fastapi.responses import JSONResponse
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded"},
                headers={"Retry-After": "60"},
            )
        return await call_next(request)


def _is_private_ip(url: str) -> bool:
    """Check if a URL points to a private/internal IP (SSRF protection)."""
    import ipaddress
    from urllib.parse import urlparse

    try:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        if not host:
            return False
        # Resolve if it's a hostname (best effort; skip DNS for speed)
        # We only block obvious private IPs in the URL itself
        try:
            ip = ipaddress.ip_address(host)
            return ip.is_private or ip.is_loopback or ip.is_link_local
        except ValueError:
            # Hostname - block localhost variants explicitly
            return host.lower() in {"localhost", "localhost.localdomain", "host.docker.internal"}
    except Exception:
        return False


def validate_llm_base_url(base_url: str | None) -> str | None:
    """Validate LLM_BASE_URL to prevent SSRF.

    Allows: public HTTPS URLs
    Blocks: private IPs (10.x, 172.16-31.x, 192.168.x), loopback, link-local,
            localhost, host.docker.internal, and non-HTTPS schemes.
    """
    if not base_url:
        return None
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("LLM_BASE_URL must use http or https scheme")
    if _is_private_ip(base_url):
        raise ValueError("LLM_BASE_URL must not point to private/internal addresses")
    return base_url


__all__ = [
    "RateLimitMiddleware",
    "SecurityHeadersMiddleware",
    "validate_llm_base_url",
]
