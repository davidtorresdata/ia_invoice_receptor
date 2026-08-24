"""Centralized exception handling: domain errors -> consistent HTTP envelope."""

import logging

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.domain.exceptions import (
    AppError,
    BusinessValidationError,
    DocumentNotFoundError,
    DocumentProcessingError,
    EmptyFileError,
    ExternalServiceError,
    FileTooLargeError,
    InvalidFileError,
    InvoiceNotFoundError,
    JobNotFoundError,
    NotFoundError,
    PersistenceError,
)

logger = logging.getLogger(__name__)

_STATUS_MAP: list[tuple[type[AppError], int]] = [
    (DocumentNotFoundError, status.HTTP_404_NOT_FOUND),
    (InvoiceNotFoundError, status.HTTP_404_NOT_FOUND),
    (JobNotFoundError, status.HTTP_404_NOT_FOUND),
    (NotFoundError, status.HTTP_404_NOT_FOUND),
    (FileTooLargeError, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE),
    (EmptyFileError, status.HTTP_400_BAD_REQUEST),
    (InvalidFileError, status.HTTP_400_BAD_REQUEST),
    (BusinessValidationError, status.HTTP_422_UNPROCESSABLE_CONTENT),
    (ExternalServiceError, status.HTTP_502_BAD_GATEWAY),
    (PersistenceError, status.HTTP_500_INTERNAL_SERVER_ERROR),
    (DocumentProcessingError, status.HTTP_500_INTERNAL_SERVER_ERROR),
]


def _error_response(exc: AppError, http_status: int) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content={"error": {"code": exc.code, "message": str(exc)}},
    )


def _request_ctx(request: Request | None) -> dict:
    if request is None:
        return {}
    return {"http_method": request.method, "path": request.url.path}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        for error_type, http_status in _STATUS_MAP:
            if isinstance(exc, error_type):
                level = logging.WARNING if http_status < 500 else logging.ERROR
                logger.log(
                    level, "Application error (%s): %s", exc.code, exc,
                    extra={**_request_ctx(request), "error_code": exc.code},
                    exc_info=True,
                )
                return _error_response(exc, http_status)
        logger.error(
            "Unhandled application error (%s): %s", exc.code, exc,
            extra=_request_ctx(request), exc_info=True,
        )
        return _error_response(exc, status.HTTP_500_INTERNAL_SERVER_ERROR)

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = exc.errors() or [{}]
        first = errors[0]
        logger.warning(
            "Request validation failed at %s: %s %s",
            first.get("loc", "?"), first.get("msg", "validation error"), errors[1:] and f"(+{len(errors) - 1} more)",
            extra=_request_ctx(request),
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {
                    "code": "request_validation_failed",
                    "message": f"Invalid request: {first.get('msg', 'validation error')}",
                }
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # Sanitized response; details only in logs.
        logger.critical(
            "Unexpected server error: %s", exc,
            extra=_request_ctx(request), exc_info=True,
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {"code": "internal_error", "message": "Internal server error"}
            },
        )
