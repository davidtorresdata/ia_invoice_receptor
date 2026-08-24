"""Root pytest fixtures."""

import pytest

from tests.fakes import FakeLLM, FakeOCR, FakeStorage, RecordingDispatcher, make_valid_extraction
from tests.fakes_uow import FakeStore, FakeUnitOfWork


@pytest.fixture
def sample_pdf_bytes() -> bytes:
    """Minimal PDF-shaped payload (header is what matters for sniffing)."""
    return (
        b"%PDF-1.4\n"
        b"INVOICE INV-2026-777\nTechSupply Europe S.L. B12345678\n"
        b"Consulting 4 x 150.00\nTotal 1210.00 EUR\n"
        b"%%EOF\n"
    )


@pytest.fixture
def sample_png_bytes() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"fake-image-payload" * 8


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def fake_ocr() -> FakeOCR:
    return FakeOCR(text="INVOICE #TEST-001 supplier Acme Supplies Ltd.")


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM(data=make_valid_extraction())


@pytest.fixture
def make_uow():
    """Factory producing UoW sessions over ONE shared store (DB-like)."""
    store = FakeStore()

    def factory() -> FakeUnitOfWork:
        return FakeUnitOfWork(store)

    factory.store = store  # type: ignore[attr-defined]
    return factory


@pytest.fixture
def recording_dispatcher() -> RecordingDispatcher:
    return RecordingDispatcher()
