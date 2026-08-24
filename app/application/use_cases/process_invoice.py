"""Use case: full invoice processing pipeline (executed by Celery workers).

Stages
------
1. Guard & transition      job -> PROCESSING, document -> PROCESSING
2. Text extraction         blob storage -> embedded text / OCR
3. LLM extraction          text -> Pydantic-validated ExtractedInvoiceData
4. Mapping                 DTO -> domain aggregate (Supplier/Invoice/items)
5. Business validation     math/date/rule checks -> ValidationReport
6. Persistence             supplier dedup + invoice + items + state updates

Retry semantics (consumed by the task layer):
    * retryable errors   -> state stays PROCESSING; error bubbles up so the
                            task can schedule a Celery retry.
    * permanent errors   -> job/document marked FAILED before re-raising.
Long-running calls (OCR/LLM) run OUTSIDE transactions: no DB locks are held
while waiting on external services, which keeps horizontal scaling safe.
"""

import logging
import time
from collections.abc import Callable
from uuid import UUID

from app.application.services.unit_of_work import UnitOfWork
from app.domain.entities.invoice import Invoice, InvoiceItem, Supplier
from app.domain.exceptions import (
    AppError,
    DocumentNotFoundError,
    DocumentProcessingError,
    JobNotFoundError,
)
from app.domain.services.document_storage import DocumentStorage
from app.domain.services.invoice_extractor import InvoiceExtractor
from app.domain.services.invoice_validator import InvoiceBusinessValidator
from app.domain.services.ocr_provider import OCRProvider
from app.domain.value_objects.enums import JobStatus
from app.domain.value_objects.extracted_invoice import ExtractedInvoiceData
from app.domain.value_objects.money import Money

logger = logging.getLogger(__name__)


class ProcessInvoiceUseCase:
    """Orchestrates extraction -> validation -> persistence for one job."""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        storage: DocumentStorage,
        ocr_provider: OCRProvider,
        extractor: InvoiceExtractor,
        validator: InvoiceBusinessValidator,
        page_renderer: Callable[[bytes, object], list[bytes]] | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._storage = storage
        self._ocr = ocr_provider
        self._extractor = extractor
        self._validator = validator
        self._page_renderer = page_renderer

    # ------------------------------------------------------------------ entry
    def execute(self, job_id: UUID) -> None:
        ctx: dict[str, str] = {"job_id": str(job_id)}
        try:
            document = self._begin(ctx)
            if document is None:
                return  # job already completed or resumed from prior attempt

            content = self._storage.get(document.storage_path)
            logger.info("Pipeline: text extraction started", extra=ctx)

            ocr_result = self._ocr.extract_text(content, document.document_type)
            logger.info(
                "Pipeline: text extracted",
                extra={**ctx, "method": ocr_result.method,
                       "pages": ocr_result.page_count, "chars": len(ocr_result.text)},
            )

            images = self._safe_render(content, document.document_type, ctx)
            data = self._run_llm(ocr_result.text, ctx, images)
            invoice = self._persist(document, data, ctx)
            logger.info(
                "Pipeline: completed",
                extra={**ctx, "invoice_id": str(invoice.id)},
            )
        except AppError as exc:
            self._handle_failure(job_id, exc, ctx)
            raise
        except Exception as exc:  # unexpected bug: never retry-loop on logic errors
            wrapped = DocumentProcessingError(f"Unexpected pipeline error: {exc}")
            logger.exception("Pipeline crashed", extra=ctx)
            self._handle_failure(job_id, wrapped, ctx)
            raise wrapped from exc

    # ------------------------------------------------------------------ stage 1
    def _begin(self, ctx: dict[str, str]):
        """Load job/document, apply guards and transition to PROCESSING."""
        with self._uow_factory() as uow:
            job = uow.jobs.get(UUID(ctx["job_id"]))
            if job is None:
                raise JobNotFoundError(f"Job {ctx['job_id']} does not exist")
            ctx["document_id"] = str(job.document_id)

            if job.status == JobStatus.COMPLETED:
                logger.info("Pipeline: skipped (job already completed)", extra=ctx)
                return None  # sentinel handled by caller-free path below

            document = uow.documents.get(job.document_id)
            if document is None:
                raise DocumentNotFoundError(f"Document {job.document_id} is missing")

            existing_invoice = uow.invoices.get_by_document(job.document_id)
            if existing_invoice is not None:
                # Previous attempt persisted the invoice but died before
                # completing the job -> resume instead of duplicating work.
                job.complete(existing_invoice.id)
                uow.jobs.update(job)
                uow.commit()
                logger.info("Pipeline: resumed already-persisted invoice", extra=ctx)
                return None

            job.start()
            document.mark_processing()
            uow.jobs.update(job)
            uow.documents.update(document)
            uow.commit()

        logger.info("Pipeline: started", extra=ctx)
        return document

    # ------------------------------------------------------------- stage 3 (LLM)
    def _safe_render(self, content: bytes, document_type, ctx: dict[str, str]) -> list[bytes]:
        if self._page_renderer is None:
            return []
        try:
            return self._page_renderer(content, document_type) or []
        except Exception as exc:  # rendering must never kill the pipeline
            logger.warning(
                "Pipeline: page rendering failed (%s); continuing text-only",
                type(exc).__name__,
                extra=ctx,
            )
            return []

    def _run_llm(
        self, text: str, ctx: dict[str, str], images: list[bytes] | None = None
    ) -> ExtractedInvoiceData:
        logger.info(
            "Pipeline: LLM extraction started",
            extra={**ctx, "images": len(images or [])},
        )
        started = time.monotonic()
        data = self._extractor.extract(text, images=images)
        logger.info(
            "Pipeline: LLM extraction succeeded",
            extra={**ctx, "elapsed_s": round(time.monotonic() - started, 3)},
        )
        return data

    # ------------------------------------------------------------ stage 5 & 6
    def _persist(self, document, data: ExtractedInvoiceData, ctx: dict[str, str]):
        with self._uow_factory() as uow:
            supplier = uow.suppliers.find_by_tax_id(data.supplier.tax_id)
            if supplier is None:
                supplier = Supplier(
                    name=data.supplier.name,
                    tax_id=data.supplier.tax_id,
                    address=data.supplier.address,
                    phone=data.supplier.phone,
                    email=str(data.supplier.email) if data.supplier.email else None,
                )
                supplier = uow.suppliers.add(supplier)
                logger.info("Pipeline: new supplier created",
                            extra={**ctx, "supplier_tax_id": supplier.tax_id})

            invoice = self._build_invoice(document.id, supplier.id, data)
            report = self._validator.validate(invoice)
            invoice.apply_validation(report)

            duplicate = uow.invoices.get_by_document(document.id)
            if duplicate is not None:
                job = uow.jobs.get(UUID(ctx["job_id"]))
                assert job is not None
                job.complete(duplicate.id)
                uow.jobs.update(job)
                uow.commit()
                logger.warning("Pipeline: concurrent duplicate detected", extra=ctx)
                return duplicate

            uow.invoices.add(invoice)

            refreshed_document = uow.documents.get(document.id)
            assert refreshed_document is not None
            refreshed_document.mark_processed()
            uow.documents.update(refreshed_document)

            job = uow.jobs.get(UUID(ctx["job_id"]))
            assert job is not None
            job.complete(invoice.id)
            uow.jobs.update(job)
            uow.commit()

        logger.info(
            "Pipeline: persisted invoice",
            extra={**ctx, "validation_issues": len(report.issues)},
        )
        return invoice

    # ---------------------------------------------------------------- helpers
    def _build_invoice(
        self, document_id: UUID, supplier_id: UUID, data: ExtractedInvoiceData
    ) -> Invoice:
        # Build items first: the aggregate's constructor enforces "at least
        # one line item", so the entity cannot be created empty.
        items = [
            InvoiceItem(
                description=item.description,
                quantity=item.quantity,
                unit_price=item.unit_price,
                tax_amount=item.tax,
                total=Money.parse(item.total),
            )
            for item in data.items
        ]
        return Invoice(
            document_id=document_id,
            supplier_id=supplier_id,
            number=data.number,
            issue_date=data.issue_date,
            due_date=data.due_date,
            currency=data.currency,
            subtotal=Money.parse(data.subtotal),
            tax_amount=Money.parse(data.tax),
            total=Money.parse(data.total),
            raw_extraction=data.model_dump(mode="json"),
            items=items,
        )

    def _handle_failure(self, job_id: UUID, exc: AppError, ctx: dict[str, str]) -> None:
        """
        Central failure strategy.

        Retryable  -> keep PROCESSING (Celery will re-enter); attempts already
                      incremented by `start()` on each attempt.
        Permanent  -> close the job as FAILED and flag the document.
        """
        if exc.retryable:
            logger.warning(
                f"Pipeline: transient failure ({exc.code}): {exc}",
                extra={**ctx, "error_code": exc.code},
            )
            return

        logger.error(
            f"Pipeline: permanent failure ({exc.code}): {exc}",
            extra={**ctx, "error_code": exc.code},
        )
        try:
            with self._uow_factory() as uow:
                job = uow.jobs.get(job_id)
                if job is not None and not job.is_terminal:
                    job.fail(str(exc))
                    uow.jobs.update(job)
                    document = uow.documents.get(job.document_id)
                    if document is not None and not document.is_processed:
                        document.mark_failed()
                        uow.documents.update(document)
                uow.commit()
        except Exception:  # pragma: no cover - last-resort logging path
            logger.exception("Pipeline: could not persist FAILED state", extra=ctx)
