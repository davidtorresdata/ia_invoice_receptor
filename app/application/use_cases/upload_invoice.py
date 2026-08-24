"""Use case: upload a document and schedule its asynchronous processing."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from app.application.services.task_dispatcher import TaskDispatcher
from app.application.services.unit_of_work import UnitOfWork
from app.domain.entities.document import Document
from app.domain.entities.job import ProcessingJob
from app.domain.exceptions import FileTooLargeError
from app.domain.services.document_storage import DocumentStorage
from app.domain.value_objects.file_type import FileType

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UploadCommand:
    """Transport-agnostic upload input."""

    filename: str
    content: bytes
    declared_mime: str | None = None


@dataclass(frozen=True, slots=True)
class UploadResult:
    document_id: UUID
    job_id: UUID
    filename: str
    status: str


class UploadInvoiceUseCase:
    """
    Validates, stores, registers and queues an invoice document.

    Flow: security checks -> blob storage -> PostgreSQL (document + PENDING
    job) -> broker enqueue. The HTTP/UI caller gets identifiers immediately;
    heavy processing happens in workers.
    """

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        storage: DocumentStorage,
        dispatcher: TaskDispatcher,
        *,
        max_file_size_bytes: int,
    ) -> None:
        self._uow_factory = uow_factory
        self._storage = storage
        self._dispatcher = dispatcher
        self._max_file_size_bytes = max_file_size_bytes

    def execute(self, command: UploadCommand) -> UploadResult:
        file_type = self._validate(command)

        safe_name = FileType.sanitize_filename(command.filename)
        document = Document(
            filename=safe_name,
            content_type=file_type.mime_type,
            size_bytes=len(command.content),
            storage_path="",  # filled right after save
            document_type=file_type.document_type,
        )
        storage_key = self._store(document.id, safe_name, command.content)
        document.storage_path = storage_key

        job = ProcessingJob(document_id=document.id)

        with self._uow_factory() as uow:
            uow.documents.add(document)
            uow.jobs.add(job)
            uow.commit()

        # Dispatch only after durable persistence (no phantom jobs).
        self._dispatcher.dispatch_invoice_processing(job.id)

        logger.info(
            "Document uploaded and queued",
            extra={"document_id": str(document.id), "job_id": str(job.id),
                   "file_size": document.size_bytes, "file_type": file_type.document_type},
        )
        return UploadResult(
            document_id=document.id, job_id=job.id,
            filename=document.filename, status=job.status.value,
        )

    # ------------------------------------------------------------------ steps
    def _validate(self, command: UploadCommand) -> FileType:
        if len(command.content) > self._max_file_size_bytes:
            raise FileTooLargeError(
                f"File exceeds the maximum allowed size "
                f"({self._max_file_size_bytes // (1024 * 1024)} MB)"
            )
        return FileType.validate(
            content=command.content,
            filename=command.filename,
            declared_mime=command.declared_mime,
        )

    def _store(self, document_id: UUID, safe_name: str, content: bytes) -> str:
        return self._storage.save(document_id, safe_name, content)
