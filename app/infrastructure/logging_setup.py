"""Structured logging with full error traceability.

Every record carries its exact origin (module, file, function, line) and, for
exceptions, structured metadata (type, python module and the raise site), so
any error can be traced back to the module that produced it:

    {"level": "ERROR", "module": "app.infrastructure.ocr.local_ocr",
     "file": "app/infrastructure/ocr/local_ocr.py", "function": "extract",
     "line": 88, "exception": {"type": "OCRExtractionError",
     "python_module": "app.domain.exceptions",
     "origin": {"file": "...", "function": "extract", "line": 91}, ...}}

JSON by default, pretty text for local dev. Uncaught exceptions in any thread
are captured via sys/threading excepthooks, so nothing escapes unlogged.
"""

import json
import logging
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

_STD_ATTRS = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message",
})


def _short_path(path: str) -> str:
    """Project-relative path when possible; absolute otherwise."""
    try:
        return str(Path(path).resolve().relative_to(_PROJECT_ROOT))
    except ValueError:
        return path


def _raise_site(exc_info: tuple) -> dict:
    """File/function/line where the exception was raised (deepest frame)."""
    tb: TracebackType | None = exc_info[2]
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    if tb is None:
        return {}
    code = tb.tb_frame.f_code
    return {
        "file": _short_path(code.co_filename),
        "function": code.co_name,
        "line": tb.tb_lineno,
    }


class JsonFormatter(logging.Formatter):
    """Minimal dependency-free JSON log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "module": record.name,
            "file": _short_path(record.pathname),
            "function": record.funcName,
            "line": record.lineno,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STD_ATTRS and not key.startswith("_"):
                try:
                    json.dumps(value)
                    payload[key] = value
                except (TypeError, ValueError):
                    payload[key] = repr(value)

        if record.exc_info:
            exc_type = record.exc_info[0]
            payload["exception"] = {
                "type": exc_type.__name__ if exc_type else None,
                "python_module": getattr(exc_type, "__module__", None),
                "origin": _raise_site(record.exc_info),
                "traceback": self.formatException(record.exc_info),
            }
        return json.dumps(payload, ensure_ascii=False)


_TEXT_FORMAT = (
    "%(asctime)s %(levelname)-8s [%(name)s :: %(funcName)s:%(lineno)d]"
    " %(message)s"
)

_hooks_installed = False


def _install_excepthooks() -> None:
    """Route uncaught exceptions (main + threads) through logging."""
    global _hooks_installed
    if _hooks_installed:
        return
    root = logging.getLogger()

    def _sys_hook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_tb: TracebackType | None,
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        root.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_tb))

    def _thread_hook(args: threading.ExceptHookArgs) -> None:
        thread_name = args.thread.name if args.thread is not None else "<unknown>"
        root.critical(
            "Uncaught exception in thread %s", thread_name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
    _hooks_installed = True


def configure_logging(level: str = "INFO", log_format: str = "json") -> None:
    """Idempotent root-logger setup used by api, worker and streamlit."""
    handler = logging.StreamHandler(sys.stdout)
    if log_format.lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(_TEXT_FORMAT))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    _install_excepthooks()

    # Third-party noise control.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("celery.utils.log").setLevel(logging.INFO)


__all__ = ["JsonFormatter", "configure_logging"]
