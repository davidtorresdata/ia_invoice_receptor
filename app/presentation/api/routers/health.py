"""Health endpoint with component checks (database, redis)."""

import logging

import redis as redis_lib
from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app import __version__
from app.config.settings import get_settings
from app.infrastructure.container import get_engine

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])


@router.get("/health")
def health(response: Response) -> dict:
    """Liveness/readiness probe aggregating dependency checks."""
    components = {
        "database": _check_database(),
        "redis": _check_redis(),
    }
    healthy = all(components.values())
    response.status_code = (
        status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE
    )
    return {
        "status": "ok" if healthy else "degraded",
        "version": __version__,
        "components": components,
    }


def _check_database() -> str:
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return "up"
    except Exception:
        logger.exception("Database health check failed")
        return "down"


def _check_redis() -> str:
    try:
        client = redis_lib.Redis.from_url(
            get_settings().redis_url,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
        )
        return "up" if client.ping() else "down"
    except Exception:
        logger.exception("Redis health check failed")
        return "down"
