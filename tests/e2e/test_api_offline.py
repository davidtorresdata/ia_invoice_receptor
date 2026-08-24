"""End-to-end API tests running FULLY OFFLINE.

The real FastAPI app is exercised, but every outbound dependency is replaced
with an in-memory fake through `app.dependency_overrides` — the single seam
provided by `app/presentation/api/deps.py`. The Celery worker is simulated by
a dispatcher that runs ProcessInvoiceUseCase inline right after enqueueing,
so a single HTTP call drives the whole pipeline: upload -> OCR -> LLM ->
validation -> persistence.
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.application.use_cases.dashboard_stats import DashboardStatsUseCase
from app.application.use_cases.get_invoice import GetInvoiceUseCase
from app.application.use_cases.get_job_status import GetJobStatusUseCase
from app.application.use_cases.list_invoices import ListInvoicesUseCase
from app.application.use_cases.process_invoice import ProcessInvoiceUseCase
from app.application.use_cases.upload_invoice import UploadInvoiceUseCase
from app.domain.services.invoice_validator import InvoiceBusinessValidator
from app.presentation.api.deps import (
    get_dashboard_stats_use_case,
    get_get_invoice_use_case,
    get_job_status_use_case,
    get_list_invoices_use_case,
    get_upload_invoice_use_case,
)
from app.presentation.api.main import create_app
from tests.fakes import FakeLLM, FakeOCR, FakeStorage, RecordingDispatcher, make_valid_extraction
from tests.fakes_uow import FakeStore, FakeUnitOfWork


def build_offline_app(max_file_size_bytes: int = 5 * 1024 * 1024):
    """Real API + fake adapters + inline 'worker'. Returns (client, store)."""
    store = FakeStore()
    storage = FakeStorage()
    ocr = FakeOCR(text="INVOICE #E2E supplier TechSupply Europe S.L.")
    llm = FakeLLM(data=make_valid_extraction())

    def uow_factory() -> FakeUnitOfWork:
        return FakeUnitOfWork(store)

    def run_pipeline_inline(job_id) -> None:
        ProcessInvoiceUseCase(
            uow_factory=uow_factory, storage=storage, ocr_provider=ocr,
            extractor=llm, validator=InvoiceBusinessValidator(),
        ).execute(job_id)

    dispatcher = RecordingDispatcher(inline_handler=run_pipeline_inline)
    upload_uc = UploadInvoiceUseCase(
        uow_factory=uow_factory, storage=storage, dispatcher=dispatcher,
        max_file_size_bytes=max_file_size_bytes,
    )

    app = create_app()
    app.dependency_overrides[get_upload_invoice_use_case] = lambda: upload_uc
    app.dependency_overrides[get_get_invoice_use_case] = lambda: GetInvoiceUseCase(uow_factory)
    app.dependency_overrides[get_list_invoices_use_case] = lambda: ListInvoicesUseCase(uow_factory)
    app.dependency_overrides[get_job_status_use_case] = lambda: GetJobStatusUseCase(uow_factory)
    app.dependency_overrides[get_dashboard_stats_use_case] = lambda: DashboardStatsUseCase(uow_factory)
    return TestClient(app), store


PDF_BYTES = b"%PDF-1.4\nINVOICE E2E TEST\n%%EOF\n"


class TestHappyFlow:
    def test_upload_poll_fetch_list_dashboard(self):
        client, _store = build_offline_app()

        # 1) Upload -> 202 with poll URL; processing runs inline.
        response = client.post(
            "/api/v1/invoices/upload",
            files={"file": ("invoice.pdf", PDF_BYTES, "application/pdf")},
        )
        assert response.status_code == 202
        body = response.json()
        # The inline "worker" may finish before the response is serialized.
        assert body["status"] in {"PENDING", "COMPLETED"}
        assert body["filename"] == "invoice.pdf"

        # 2) Poll the job -> COMPLETED and linked to an invoice.
        job_response = client.get(body["poll_url"])
        assert job_response.status_code == 200
        job_body = job_response.json()
        assert job_body["status"] == "COMPLETED"
        assert job_body["attempts"] >= 1
        invoice_id = job_body["invoice_id"]
        assert invoice_id is not None

        # 3) Fetch the processed invoice.
        detail = client.get(f"/api/v1/invoices/{invoice_id}")
        assert detail.status_code == 200
        invoice = detail.json()
        assert invoice["number"] == "INV-2026-001"
        assert invoice["supplier"]["tax_id"] == "B87654321"
        assert len(invoice["items"]) == 2

        # 4) List & search by supplier tax id.
        listed = client.get("/api/v1/invoices", params={"search": "B87654321"})
        assert listed.status_code == 200
        page = listed.json()
        assert page["total"] == 1
        assert page["items"][0]["id"] == invoice_id

        # 5) Dashboard aggregates reflect exactly one valid invoice.
        stats = client.get("/api/v1/dashboard/stats").json()
        assert stats["jobs"]["completed"] == 1
        assert stats["invoices"]["total"] == 1
        assert stats["total_invoiced"] == invoice["total"]

    def test_upload_is_persisted_even_if_worker_were_async(self):
        """Without inline handler the API still returns 202 and stores PENDING."""
        store = FakeStore()

        def uow_factory() -> FakeUnitOfWork:
            return FakeUnitOfWork(store)

        app = create_app()
        app.dependency_overrides[get_upload_invoice_use_case] = (
            lambda: UploadInvoiceUseCase(
                uow_factory=uow_factory, storage=FakeStorage(),
                dispatcher=RecordingDispatcher(), max_file_size_bytes=1024 * 1024,
            )
        )
        client = TestClient(app)
        response = client.post(
            "/api/v1/invoices/upload",
            files={"file": ("inv.pdf", PDF_BYTES, "application/pdf")},
        )
        assert response.status_code == 202
        assert len(store.jobs) == 1  # queued, not yet processed


class TestErrorContract:
    def test_bad_extension_returns_400_envelope(self):
        client, _ = build_offline_app()
        response = client.post(
            "/api/v1/invoices/upload",
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_file"

    def test_oversize_returns_413(self, monkeypatch):
        class _SmallSettings:
            max_file_size_mb = 1
            max_file_size_bytes = 64

        import app.presentation.api.routers.invoices as invoices_router

        monkeypatch.setattr(invoices_router, "get_settings", lambda: _SmallSettings())
        client, _ = build_offline_app(max_file_size_bytes=64)
        response = client.post(
            "/api/v1/invoices/upload",
            files={"file": ("big.pdf", b"x" * 65, "application/pdf")},
        )
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "file_too_large"

    def test_unknown_job_returns_404_envelope(self):
        client, _ = build_offline_app()
        response = client.get(f"/api/v1/jobs/{uuid4()}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "job_not_found"

    def test_unknown_invoice_returns_404_envelope(self):
        client, _ = build_offline_app()
        response = client.get(f"/api/v1/invoices/{uuid4()}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "invoice_not_found"


class TestListFilters:
    @pytest.fixture
    def seeded_client(self):
        client, _store = build_offline_app()
        for name in ("one.pdf", "two.pdf"):
            client.post(
                "/api/v1/invoices/upload",
                files={"file": (name, PDF_BYTES, "application/pdf")},
            )
        return client

    def test_search_without_match_is_empty_page(self, seeded_client):
        response = seeded_client.get("/api/v1/invoices", params={"search": "no-such-supplier"})
        assert response.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}
