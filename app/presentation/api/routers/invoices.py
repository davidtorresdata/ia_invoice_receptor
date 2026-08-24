"""Invoice endpoints: upload, detail, list."""

import logging
from datetime import date
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, UploadFile, status

from app.application.use_cases.upload_invoice import UploadCommand
from app.config.settings import get_settings
from app.domain.exceptions import FileTooLargeError, InvalidFileError
from app.infrastructure import container
from app.presentation.api.deps import (
    get_get_invoice_use_case,
    get_list_invoices_use_case,
    get_upload_invoice_use_case,
)
from app.presentation.api.mappers import invoice_to_response, summary_to_response
from app.presentation.api.schemas import (
    InvoiceResponse,
    PaginatedInvoicesResponse,
    UploadResponse,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/invoices", tags=["invoices"])

_UPLOAD_CHUNK = 1024 * 1024


@router.post(
    "/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload a PDF/PNG/JPG/JPEG invoice and queue async processing",
)
async def upload_invoice(
    file: Annotated[
        UploadFile,
        File(..., description="Invoice document (PDF, PNG, JPG or JPEG)"),
    ],
    use_case: object = Depends(get_upload_invoice_use_case),
) -> UploadResponse:
    content = await _read_limited(file)
    result = use_case.execute(  # type: ignore[attr-defined]
        UploadCommand(
            filename=file.filename or "document",
            content=content,
            declared_mime=file.content_type,
        )
    )
    return UploadResponse(
        document_id=result.document_id,
        job_id=result.job_id,
        filename=result.filename,
        status=result.status,
        poll_url=f"/api/v1/jobs/{result.job_id}",
    )


@router.get("", response_model=PaginatedInvoicesResponse, summary="List invoices")
def list_invoices(
    search: str | None = Query(None, max_length=120, description="Matches number/supplier"),
    date_from: date | None = None,
    date_to: date | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    use_case: object = Depends(get_list_invoices_use_case),
) -> PaginatedInvoicesResponse:
    page = use_case.execute(  # type: ignore[attr-defined]
        container.build_invoice_query(
            search=search,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset,
        )
    )
    return PaginatedInvoicesResponse(
        items=[summary_to_response(s) for s in page.items],
        total=page.total_count,
        limit=limit,
        offset=offset,
    )


@router.get("/{invoice_id}", response_model=InvoiceResponse, summary="Get one invoice")
def get_invoice(
    invoice_id: UUID,
    use_case: object = Depends(get_get_invoice_use_case),
) -> InvoiceResponse:
    invoice, supplier = use_case.execute(invoice_id)  # type: ignore[attr-defined]
    return invoice_to_response(invoice, supplier)


# ---------------------------------------------------------------------------
async def _read_limited(file: UploadFile) -> bytes:
    """Stream the upload enforcing the configured size cap (no huge buffers)."""
    max_bytes = get_settings().max_file_size_bytes
    buffer = bytearray()
    while chunk := await file.read(_UPLOAD_CHUNK):
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise FileTooLargeError(
                f"File exceeds the maximum allowed size "
                f"({get_settings().max_file_size_mb} MB)"
            )
    if not buffer:
        raise InvalidFileError("Uploaded file is empty")
    return bytes(buffer)
