"""Unit tests for ProcessInvoiceUseCase (the async pipeline)."""

from uuid import uuid4

import pytest

from app.application.use_cases.process_invoice import ProcessInvoiceUseCase
from app.domain.entities.document import Document, DocumentStatus
from app.domain.entities.job import ProcessingJob
from app.domain.exceptions import (
    DocumentNotFoundError,
    LLMExtractionError,
    StorageError,
)
from app.domain.services.invoice_validator import InvoiceBusinessValidator
from app.domain.value_objects.enums import DocumentType
from tests.fakes import FakeLLM, make_valid_extraction


@pytest.fixture
def use_case(make_uow, fake_storage, fake_ocr, fake_llm):
    return ProcessInvoiceUseCase(
        uow_factory=make_uow,
        storage=fake_storage,
        ocr_provider=fake_ocr,
        extractor=fake_llm,
        validator=InvoiceBusinessValidator(),
    )


def seed_document_and_job(store, fake_storage) -> ProcessingJob:
    """Insert a RECEIVED document + PENDING job + stored blob (DB-like setup)."""
    document = Document(
        filename="invoice.pdf", content_type="application/pdf",
        size_bytes=64, storage_path="placeholder",
        document_type=DocumentType.PDF, status=DocumentStatus.RECEIVED,
    )
    storage_key = fake_storage.save(document.id, "invoice.pdf", b"%PDF-1.4 blob")
    document.storage_path = storage_key

    job = ProcessingJob(document_id=document.id)
    store.documents[document.id] = document
    store.jobs[job.id] = job
    return job


class TestHappyPath:
    def test_full_pipeline_completes(self, use_case, make_uow):
        job = seed_document_and_job(make_uow.store, use_case._storage)

        use_case.execute(job.id)

        stored_job = make_uow.store.jobs[job.id]
        assert str(stored_job.status) == "COMPLETED"
        assert stored_job.invoice_id is not None

        invoice = make_uow.store.invoices[stored_job.invoice_id]
        assert len(invoice.items) == 2
        assert invoice.validation_report["is_valid"] is True

        document = make_uow.store.documents[job.document_id]
        assert str(document.status) == "PROCESSED"

    def test_supplier_deduplicated_by_tax_id(self, use_case, make_uow):
        first = seed_document_and_job(make_uow.store, use_case._storage)
        second = seed_document_and_job(make_uow.store, use_case._storage)

        use_case.execute(first.id)
        use_case.execute(second.id)

        tax_ids = {s.tax_id for s in make_uow.store.suppliers.values()}
        assert tax_ids == {"B87654321"}  # reused, never duplicated

    def test_raw_extraction_audited(self, use_case, make_uow):
        job = seed_document_and_job(make_uow.store, use_case._storage)
        use_case.execute(job.id)
        invoice = next(iter(make_uow.store.invoices.values()))
        assert invoice.raw_extraction["number"] == "INV-2026-001"


class TestValidationOutcomes:
    def test_inconsistent_math_persists_invalid_invoice(
        self, make_uow, fake_storage, fake_ocr
    ):
        bad_payload = make_valid_extraction(total="5000.00")  # broken math
        use_case = ProcessInvoiceUseCase(
            uow_factory=make_uow, storage=fake_storage, ocr_provider=fake_ocr,
            extractor=FakeLLM(data=bad_payload),
            validator=InvoiceBusinessValidator(),
        )
        job = seed_document_and_job(make_uow.store, fake_storage)

        use_case.execute(job.id)

        invoice = next(iter(make_uow.store.invoices.values()))
        assert invoice.validation_report["is_valid"] is False
        codes = {issue["code"] for issue in invoice.validation_report["issues"]}
        assert "math.total_mismatch" in codes

        stored_job = make_uow.store.jobs[job.id]
        assert str(stored_job.status) == "COMPLETED"  # pipeline ok; data invalid


class TestIdempotencyAndResume:
    def test_completed_job_is_skipped(self, use_case, make_uow):
        job = seed_document_and_job(make_uow.store, use_case._storage)
        use_case.execute(job.id)

        calls_before = use_case._extractor.calls
        use_case.execute(job.id)  # duplicate delivery / manual requeue

        assert use_case._extractor.calls == calls_before
        assert len(make_uow.store.invoices) == 1

    def test_resume_links_existing_invoice_after_lost_completion(self, use_case, make_uow):
        """
        Crash simulation: the invoice was persisted and the document marked,
        but the job's COMPLETED update was lost -> next run must resume.
        """
        from app.domain.value_objects.enums import JobStatus

        job = seed_document_and_job(make_uow.store, use_case._storage)
        use_case.execute(job.id)
        completed_job = make_uow.store.jobs[job.id]

        completed_job.status = JobStatus.PROCESSING  # simulate lost update
        calls_before = use_case._extractor.calls

        use_case.execute(job.id)

        assert use_case._extractor.calls == calls_before      # no re-extraction
        assert len(make_uow.store.invoices) == 1              # no duplicate
        assert str(completed_job.status) == "COMPLETED"

    def test_retry_after_transient_failure_reenters_processing(self, use_case, make_uow):
        """
        Celery retry simulation: attempt 1 fails transiently (document left
        in PROCESSING); attempt 2 must resume instead of crashing on the
        PROCESSING -> PROCESSING guard.
        """
        from app.domain.exceptions import OCRExtractionError

        job = seed_document_and_job(make_uow.store, use_case._storage)
        use_case._ocr.fail_with = OCRExtractionError("tesseract hiccup", retryable=True)
        with pytest.raises(OCRExtractionError):
            use_case.execute(job.id)

        document = next(iter(make_uow.store.documents.values()))
        assert str(document.status) == "PROCESSING"

        use_case._ocr.fail_with = None  # transient condition cleared
        use_case.execute(job.id)

        stored_job = make_uow.store.jobs[job.id]
        assert str(stored_job.status) == "COMPLETED"
        assert len(make_uow.store.invoices) == 1


class TestFailureSemantics:
    def test_transient_error_keeps_processing_state(self, make_uow, fake_storage,
                                                    fake_ocr):
        flaky = FakeLLM(exc=LLMExtractionError("provider timeout"))
        use_case = ProcessInvoiceUseCase(
            uow_factory=make_uow, storage=fake_storage, ocr_provider=fake_ocr,
            extractor=flaky, validator=InvoiceBusinessValidator(),
        )
        job = seed_document_and_job(make_uow.store, fake_storage)

        with pytest.raises(LLMExtractionError):
            use_case.execute(job.id)

        stored_job = make_uow.store.jobs[job.id]
        assert str(stored_job.status) == "PROCESSING"  # retry will re-enter
        assert stored_job.attempts == 1

    def test_missing_document_marks_job_failed(self, use_case, make_uow):
        from app.domain.entities.job import ProcessingJob as Job  # clarity

        job = Job(document_id=uuid4())
        make_uow.store.jobs[job.id] = job

        with pytest.raises(DocumentNotFoundError):
            use_case.execute(job.id)

        assert str(make_uow.store.jobs[job.id].status) == "FAILED"

    def test_missing_blob_marks_everything_failed(self, make_uow, fake_storage,
                                                  fake_ocr, fake_llm):
        use_case = ProcessInvoiceUseCase(
            uow_factory=make_uow, storage=fake_storage, ocr_provider=fake_ocr,
            extractor=fake_llm, validator=InvoiceBusinessValidator(),
        )
        document = Document(filename="a.pdf", content_type="application/pdf",
                            size_bytes=10, storage_path="ghost.pdf",
                            document_type=DocumentType.PDF)
        job = ProcessingJob(document_id=document.id)
        make_uow.store.documents[document.id] = document
        make_uow.store.jobs[job.id] = job

        with pytest.raises(StorageError):
            use_case.execute(job.id)

        assert str(make_uow.store.jobs[job.id].status) == "FAILED"
        assert make_uow.store.jobs[job.id].error_message
