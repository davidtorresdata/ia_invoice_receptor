"""Central, environment-driven configuration.

Every environment-specific value is read from process environment / `.env`.
Secrets are modeled with `SecretStr` so they never leak into logs or reprs.
This module lives outside the domain: only the composition root and
adapters import it.
"""

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All runtime settings. Names map 1:1 to `.env` variables."""

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application ---------------------------------------------------------
    app_name: str = "Invoice Processing System"
    environment: str = "development"
    log_level: str = "INFO"
    log_format: str = "json"  # json | text
    # CORS origins (comma-separated). Default "*" only in development.
    cors_origins: str = "*"

    # --- Database ------------------------------------------------------------
    database_url: str = "postgresql+psycopg://invoice:invoice@localhost:5432/invoices"

    # --- Redis ---------------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"

    # --- Storage -------------------------------------------------------------
    storage_path: Path = Path("./data/uploads")

    # --- Upload limits -------------------------------------------------------
    max_file_size_mb: int = 10

    # --- Celery --------------------------------------------------------------
    celery_task_timeout_seconds: int = 600
    celery_max_retries: int = 3
    celery_retry_backoff_seconds: int = 30

    # --- OCR -----------------------------------------------------------------
    ocr_provider: str = "local"
    ocr_min_text_chars_per_page: int = 40
    ocr_language: str = "eng+spa"

    # --- LLM -----------------------------------------------------------------
    # hybrid = reglas primero (offline); si no hallan patron, escala segun el
    # switch LLM_EXECUTION:
    #   api   -> modelo de vision remoto (LLM_BASE_URL + LLM_API_KEY + LLM_MODEL)
    #   local -> PP-OCR (PaddleOCR) sobre las paginas + extractor de reglas,
    #            100% local, sin llamadas a LLM.
    llm_provider: str = "hybrid"  # hybrid | rules | openai | mock
    llm_execution: str = "api"  # api | local
    llm_api_key: SecretStr = SecretStr("")
    llm_model: str = "gemini-3.5-flash-lite"
    llm_base_url: str | None = None
    llm_timeout_seconds: int = 60
    llm_temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    llm_max_attempts: int = 3
    vision_max_pages: int = Field(default=4, ge=1, le=20)

    @field_validator("llm_base_url", mode="before")
    @classmethod
    def _validate_llm_base_url(cls, value: str | None) -> str | None:
        if not value:
            return None
        import ipaddress
        from urllib.parse import urlparse

        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("LLM_BASE_URL must use http or https scheme")
        host = parsed.hostname or ""
        # Block private/internal IPs and hostnames
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            # Not an IP address - check hostname
            if host.lower() in {"localhost", "localhost.localdomain", "host.docker.internal"}:
                raise ValueError("LLM_BASE_URL must not point to localhost/internal hosts") from None
        else:
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                raise ValueError("LLM_BASE_URL must not point to private/internal addresses")
        return value

    # --- OCR local (modo LLM_EXECUTION=local) ---------------------------------
    # vl       -> PaddleOCR-VL 1.5: PP-DocLayout(PP-Structure) + PP-OCR + VL
    # paddle   -> PP-OCRv5 puro (mas rapido, sin VL)
    # tesseract-> respaldo clasico
    local_ocr_engine: str = "vl"
    local_ocr_lang: str = "es"

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR"}
        normalized = value.strip().upper()
        if normalized not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return normalized

    @field_validator("llm_execution")
    @classmethod
    def _validate_llm_execution(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"api", "local"}:
            raise ValueError(f"llm_execution must be one of ['api', 'local'], got {value!r}")
        return normalized

    @field_validator("local_ocr_engine")
    @classmethod
    def _validate_local_ocr_engine(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"vl", "paddle", "tesseract"}:
            raise ValueError(
                f"local_ocr_engine must be one of ['vl', 'paddle', 'tesseract'], got {value!r}"
            )
        return normalized

    @field_validator("log_format", "llm_provider", "ocr_provider", "environment", mode="after")
    @classmethod
    def _lowercase(cls, value: str) -> str:
        return value.strip().lower()

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    @property
    def is_production(self) -> bool:
        return self.environment.lower() == "production"


@lru_cache
def get_settings() -> Settings:
    """Process-wide cached settings accessor (composition root entry point).

    The dotenv file is selected by the `ENVIRONMENT` variable (process env,
    injected by docker-compose): `production` loads `.env`; anything else
    (development/pruebas) loads `.env.example`. Real environment variables
    always take precedence over either file.
    """
    environment = os.environ.get("ENVIRONMENT", "development").strip().lower()
    env_file = ".env" if environment == "production" else ".env.example"
    return Settings(_env_file=env_file)
