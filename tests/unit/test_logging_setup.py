"""Unit tests for traceable logging (module/file/function/line + exceptions)."""

import json
import logging
import sys
import threading
from types import SimpleNamespace

from app.infrastructure.logging_setup import (
    JsonFormatter,
    configure_logging,
)

MODULE_NAME = "app.infrastructure.logging_setup"


def _record(
    *,
    name: str = "app.fake.module",
    pathname: str | None = None,
    lineno: int = 42,
    func: str = "some_function",
    msg: str = "boom %s",
    args: tuple = ("detail",),
    exc_info=None,
) -> logging.LogRecord:
    return logging.LogRecord(
        name=name,
        level=logging.ERROR,
        pathname=pathname or __file__,
        lineno=lineno,
        msg=msg,
        args=args,
        exc_info=exc_info,
        func=func,
    )


class TestJsonOriginFields:
    def test_every_record_carries_module_file_function_line(self):
        payload = json.loads(JsonFormatter().format(_record()))

        assert payload["module"] == "app.fake.module"
        assert payload["file"].endswith("test_logging_setup.py")
        assert payload["function"] == "some_function"
        assert payload["line"] == 42
        assert payload["message"] == "boom detail"

    def test_project_relative_paths(self):
        record = _record(pathname=f"{__file__}")
        payload = json.loads(JsonFormatter().format(record))
        assert not payload["file"].startswith("/")
        assert "tests/unit/" in payload["file"]

    def test_extra_context_is_preserved(self):
        record = _record()
        record.job_id = "abc-123"  # type: ignore[attr-defined]
        payload = json.loads(JsonFormatter().format(record))
        assert payload["job_id"] == "abc-123"


class TestJsonExceptionMetadata:
    def test_exception_type_module_and_raise_site(self):
        expected_line = sys._getframe().f_lineno + 2
        try:
            raise ValueError("kaboom")
        except ValueError:
            info = sys.exc_info()

        payload = json.loads(JsonFormatter().format(_record(exc_info=info)))
        exc = payload["exception"]

        assert exc["type"] == "ValueError"
        assert exc["python_module"] == "builtins"
        assert exc["origin"]["function"] == "test_exception_type_module_and_raise_site"
        assert exc["origin"]["line"] == expected_line
        assert 'raise ValueError("kaboom")' in exc["traceback"]
        # origin fields must NOT leak into the flat extras section
        assert "origin" not in payload

    def test_domain_exception_reports_its_defining_module(self):
        from app.domain.exceptions import InvalidFileError

        try:
            raise InvalidFileError("bad file")
        except InvalidFileError:
            info = sys.exc_info()

        payload = json.loads(JsonFormatter().format(_record(exc_info=info)))
        assert payload["exception"]["type"] == "InvalidFileError"
        assert payload["exception"]["python_module"] == "app.domain.exceptions"


class TestTextFormat:
    def test_includes_module_function_and_line(self, capsys):
        configure_logging(level="DEBUG", log_format="text")
        logging.getLogger("app.t.text").error("hola")
        out = capsys.readouterr().out

        assert "[app.t.text :: test_includes_module_function_and_line:" in out
        assert out.rstrip().endswith("hola")


def _last_json_line(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


class TestUncaughtExceptionHooks:
    def test_sys_hook_logs_critical_with_origin(self, capsys):
        configure_logging(level="DEBUG", log_format="json")
        try:
            raise RuntimeError("never caught")
        except RuntimeError:
            info = sys.exc_info()

        sys.excepthook(info[0], info[1], info[2])
        payload = _last_json_line(capsys)

        assert payload["level"] == "CRITICAL"
        assert payload["message"] == "Uncaught exception"
        assert payload["exception"]["type"] == "RuntimeError"
        assert payload["exception"]["origin"]["function"] == (
            "test_sys_hook_logs_critical_with_origin"
        )

    def test_thread_hook_logs_thread_name(self, capsys):
        configure_logging(level="DEBUG", log_format="json")
        try:
            raise KeyError("thread boom")
        except KeyError:
            info = sys.exc_info()

        hook_args = SimpleNamespace(
            exc_type=info[0],
            exc_value=info[1],
            exc_traceback=info[2],
            thread=threading.current_thread(),
        )
        threading.excepthook(hook_args)  # type: ignore[arg-type]
        payload = _last_json_line(capsys)

        assert payload["level"] == "CRITICAL"
        assert threading.current_thread().name in payload["message"]
        assert payload["exception"]["type"] == "KeyError"

    def test_keyboard_interrupt_falls_back_to_default_hook(self, caplog, monkeypatch):
        configure_logging(level="DEBUG", log_format="json")
        forwarded = []

        monkeypatch.setattr(
            sys, "__excepthook__",
            lambda *args: forwarded.append(args),
        )
        sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)

        assert len(forwarded) == 1
        assert not caplog.records or all(
            "Uncaught exception" not in r.getMessage() for r in caplog.records
        )


class TestConfigureLoggingIdempotent:
    def test_repeated_calls_do_not_duplicate_handlers(self, capsys):
        for _ in range(3):
            configure_logging(level="INFO", log_format="json")

        root = logging.getLogger()
        stream_handlers = [
            h for h in root.handlers if isinstance(h, logging.StreamHandler)
        ]
        assert len(stream_handlers) == 1
