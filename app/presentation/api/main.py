"""FastAPI application factory."""

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.config.settings import get_settings
from app.infrastructure.logging_setup import configure_logging
from app.infrastructure.security.middleware import RateLimitMiddleware, SecurityHeadersMiddleware
from app.presentation.api.exception_handlers import register_exception_handlers
from app.presentation.api.routers import (
    dashboard,
    health,
    invoices,
    jobs,
)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(level=settings.log_level, log_format=settings.log_format)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Processes invoice documents (PDF/images) asynchronously: OCR -> "
            "LLM structured extraction -> business validation -> PostgreSQL."
        ),
    )

    # Security headers (outermost = applied last = first on response)
    app.add_middleware(SecurityHeadersMiddleware)
    # Rate limiting
    app.add_middleware(RateLimitMiddleware, requests_per_minute=60, burst=10)
    # CORS: restrict in production; allow all in development for convenience.
    raw_origins = settings.cors_origins.strip()
    allow_origins = ["*"] if raw_origins == "*" else [o.strip() for o in raw_origins.split(",") if o.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)

    api_v1 = APIRouter(prefix="/api/v1")
    api_v1.include_router(invoices.router)
    api_v1.include_router(jobs.router)
    api_v1.include_router(dashboard.router)
    app.include_router(api_v1)
    app.include_router(health.router)

    return app


app = create_app()
