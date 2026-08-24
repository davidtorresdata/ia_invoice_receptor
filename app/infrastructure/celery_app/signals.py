"""Celery signal hooks for full error traceability.

* `setup_logging`  -> workers use the app formatter (module/file/function/line
  + structured exception metadata) instead of Celery's default hijack.
* `task_failure`   -> any task that dies with an unhandled exception is logged
  at ERROR with task name/id, args and full traceback.

Imported by `celery_app.app` so registration happens on every worker start.
"""

import logging

from celery import signals

from app.config.settings import get_settings
from app.infrastructure.logging_setup import configure_logging

logger = logging.getLogger(__name__)


@signals.setup_logging.connect
def _setup_celery_logging(**_) -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, log_format=settings.log_format)


@signals.task_failure.connect
def _log_task_failure(
    sender=None,
    task_id=None,
    args=None,
    kwargs=None,
    einfo=None,
    **_,
) -> None:
    exc_info = False
    if einfo is not None:
        exc_type = getattr(einfo, "type", None)
        exc_value = getattr(einfo, "value", None)
        exc_tb = getattr(einfo, "tb", None)
        exc_info = (
            (exc_type, exc_value, exc_tb) if exc_value is not None else True
        )
    logger.error(
        "Task failed: %s",
        getattr(sender, "name", sender),
        extra={
            "task": getattr(sender, "name", str(sender)),
            "task_id": task_id,
            "args": list(args or []),
            "kwargs": dict(kwargs or {}),
        },
        exc_info=exc_info,
    )
