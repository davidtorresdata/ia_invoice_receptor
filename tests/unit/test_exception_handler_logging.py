"""Every handled HTTP error must leave a traceable log entry."""

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.domain.exceptions import InvalidFileError, JobNotFoundError
from app.presentation.api.exception_handlers import register_exception_handlers

LOGGER_NAME = "app.presentation.api.exception_handlers"


def _client() -> TestClient:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/domain-error")
    def domain_error() -> None:
        raise InvalidFileError("content does not match signatures")

    @app.get("/not-found")
    def not_found() -> None:
        raise JobNotFoundError("job 123 absent")

    @app.get("/unexpected")
    def unexpected() -> None:
        raise RuntimeError("kaboom")

    @app.get("/validated")
    def validated(q: int) -> dict:  # query param required -> 422 when absent
        return {"q": q}

    return TestClient(app, raise_server_exceptions=False)


class TestAppErrorLogging:
    def test_client_error_logged_as_warning_with_origin(self, caplog):
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            response = _client().get("/domain-error")

        assert response.status_code == 400
        record = caplog.records[-1]
        assert record.levelno == logging.WARNING
        assert record.error_code == "invalid_file"          # type: ignore[attr-defined]
        assert record.http_method == "GET"                   # type: ignore[attr-defined]
        assert record.path == "/domain-error"                # type: ignore[attr-defined]
        assert record.exc_info is not None                   # traceback preserved
        assert record.exc_info[0] is InvalidFileError

    def test_not_found_logged_with_code(self, caplog):
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            response = _client().get("/not-found")

        assert response.status_code == 404
        record = caplog.records[-1]
        assert record.error_code == "job_not_found"          # type: ignore[attr-defined]

    def test_unexpected_error_logged_as_critical(self, caplog):
        with caplog.at_level(logging.CRITICAL, logger=LOGGER_NAME):
            response = _client().get("/unexpected")

        assert response.status_code == 500
        assert response.json()["error"]["code"] == "internal_error"
        record = caplog.records[-1]
        assert record.levelno == logging.CRITICAL
        assert record.exc_info[0] is RuntimeError


class TestRequestValidationLogging:
    def test_missing_param_logged_with_path(self, caplog):
        with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
            response = _client().get("/validated")  # ?q missing

        assert response.status_code == 422
        record = caplog.records[-1]
        assert record.levelno == logging.WARNING
        assert record.path == "/validated"                    # type: ignore[attr-defined]
        assert "q" in record.getMessage()
