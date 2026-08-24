"""Unit tests for UploadInvoiceUseCase."""

import pytest

from app.application.use_cases.upload_invoice import UploadCommand, UploadInvoiceUseCase
from app.domain.exceptions import FileTooLargeError, InvalidFileError

PDF = b"%PDF-1.4 invoice-content"


@pytest.fixture
def use_case(make_uow, fake_storage, recording_dispatcher):
    return UploadInvoiceUseCase(
        uow_factory=make_uow,
        storage=fake_storage,
        dispatcher=recording_dispatcher,
        max_file_size_bytes=1024 * 1024,
    )


class TestHappyPath:
    def test_creates_document_and_pending_job(self, use_case, make_uow):
        result = use_case.execute(UploadCommand("invoice.pdf", PDF, "application/pdf"))

        store = make_uow.store
        assert len(store.documents) == 1
        job = next(iter(store.jobs.values()))
        assert str(job.status) == "PENDING"
        assert result.job_id == job.id
        assert result.document_id == job.document_id
        assert result.status == "PENDING"
        assert result.filename == "invoice.pdf"

    def test_dispatches_once_after_persistence(self, use_case, recording_dispatcher):
        result = use_case.execute(UploadCommand("invoice.pdf", PDF))
        assert recording_dispatcher.dispatched == [result.job_id]

    def test_sanitizes_filename(self, use_case, make_uow, fake_storage):
        result = use_case.execute(UploadCommand("../../weird name%.pdf", PDF))
        assert "/" not in result.filename
        stored_key = next(iter(fake_storage.files))
        assert stored_key.endswith(result.filename)


class TestRejections:
    def test_oversize_rejected(self, make_uow, fake_storage, recording_dispatcher):
        small_case = UploadInvoiceUseCase(
            uow_factory=make_uow, storage=fake_storage,
            dispatcher=recording_dispatcher, max_file_size_bytes=10,
        )
        with pytest.raises(FileTooLargeError):
            small_case.execute(UploadCommand("big.pdf", b"x" * 11))

    def test_bad_extension_rejected(self, use_case):
        with pytest.raises(InvalidFileError):
            use_case.execute(UploadCommand("invoice.txt", PDF))

    def test_nothing_persisted_on_failure(self, use_case, make_uow,
                                          recording_dispatcher):
        with pytest.raises(InvalidFileError):
            use_case.execute(UploadCommand("bad.exe", PDF))
        assert recording_dispatcher.dispatched == []
        assert len(make_uow.store.jobs) == 0
