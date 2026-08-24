"""FastAPI presentation layer (thin: no business logic, only transport)."""

from app.presentation.api.main import create_app

__all__ = ["create_app"]
