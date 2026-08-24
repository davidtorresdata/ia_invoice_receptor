"""Celery application: broker/backend wiring and worker defaults."""

from celery import Celery

from app.config.settings import get_settings

settings = get_settings()

celery_app = Celery(
    "invoice_processor",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.infrastructure.celery_app.tasks"],
)

celery_app.conf.update(
    # Single dedicated queue keeps scaling/monitoring simple.
    task_default_queue="invoices",
    task_acks_late=True,              # redeliver if a worker dies mid-task
    worker_prefetch_multiplier=1,     # fair distribution across workers
    task_track_started=True,
    task_time_limit=settings.celery_task_timeout_seconds + 60,
    task_soft_time_limit=settings.celery_task_timeout_seconds,
    broker_connection_retry_on_startup=True,
    result_expires=3600,
    timezone="UTC",
    enable_utc=True,
    # Our `setup_logging` signal installs the traceable app formatter.
    worker_hijack_root_logger=False,
)

# Registers setup_logging / task_failure signal handlers (side effects).
from app.infrastructure.celery_app import signals as _celery_signals  # noqa: E402,F401
