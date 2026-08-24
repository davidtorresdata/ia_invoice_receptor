"""ORM model for asynchronous processing jobs."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.value_objects.enums import JobStatus
from app.infrastructure.database.base import Base, TimestampMixin, str_enum
from app.infrastructure.database.models.document_model import DocumentModel
from app.infrastructure.database.models.invoice_model import InvoiceModel


class ProcessingJobModel(Base, TimestampMixin):
    __tablename__ = "processing_jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    invoice_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("invoices.id", ondelete="SET NULL"), index=True
    )

    # Real relationships (NOT viewonly): explicit unit-of-work dependency
    # edges so documents/invoices are always INSERTed before processing_jobs.
    document: Mapped["DocumentModel"] = relationship()
    invoice_ref: Mapped["InvoiceModel | None"] = relationship()

    status: Mapped[JobStatus] = mapped_column(
        str_enum(JobStatus), nullable=False, default=JobStatus.PENDING
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    celery_task_id: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


Index("ix_processing_jobs_status_created_at",
      ProcessingJobModel.status, ProcessingJobModel.created_at.desc())
