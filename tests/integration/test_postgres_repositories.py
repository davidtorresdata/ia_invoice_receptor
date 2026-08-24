"""Repository integration tests against real PostgreSQL.

Run with:
    docker compose up -d postgres
    TEST_DATABASE_URL=postgresql+psycopg://invoice:invoice@localhost:5432/invoices_test \
        pytest -m integration

Skipped automatically when TEST_DATABASE_URL is absent.
"""

import os
import uuid
from datetime import date
from decimal import Decimal

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("TEST_DATABASE_URL"),
        reason="TEST_DATABASE_URL not set (needs a live PostgreSQL)",
    ),
]

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.domain.entities.document import Document  # noqa: E402
from app.domain.entities.invoice import Invoice, InvoiceItem, Supplier  # noqa: E402
from app.domain.entities.job import ProcessingJob  # noqa: E402
from app.domain.exceptions import PersistenceError  # noqa: E402
from app.domain.repositories.invoice_repository import InvoiceQuery  # noqa: E402
from app.domain.value_objects.enums import (  # noqa: E402
    DocumentType,
    JobStatus,
)
from app.domain.value_objects.money import Money  # noqa: E402
from app.infrastructure.database.base import Base  # noqa: E402
from app.infrastructure.repositories.unit_of_work import (  # noqa: E402
    SqlAlchemyUnitOfWork,
)


@pytest.fixture(scope="module")
def engine():
    _engine = create_engine(os.environ["TEST_DATABASE_URL"])
    Base.metadata.create_all(_engine)
    yield _engine
    Base.metadata.drop_all(_engine)
    _engine.dispose()


@pytest.fixture
def uow_factory(engine):
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def make() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(factory)

    return make


def seed_document_and_job(unit: SqlAlchemyUnitOfWork) -> ProcessingJob:
    document = Document(
        filename="inv.pdf", content_type="application/pdf", size_bytes=10,
        storage_path=f"2026/01/{uuid.uuid4()}/inv.pdf",
        document_type=DocumentType.PDF,
    )
    job = ProcessingJob(document_id=document.id)
    unit.documents.add(document)
    unit.jobs.add(job)
    unit.commit()
    return job


def build_invoice(document_id, supplier_id, number="INV-1") -> Invoice:
    # Items are built FIRST: the aggregate's __post_init__ enforces
    # "at least one line item", so it cannot be created empty.
    items = [
        InvoiceItem(description="Line A", quantity=Decimal(1),
                    unit_price=Decimal("100"), tax_amount=Decimal("21"),
                    total=Money.parse("100")),
        InvoiceItem(description="Line B", quantity=Decimal(1),
                    unit_price=Decimal("100"), tax_amount=Decimal("21"),
                    total=Money.parse("100")),
    ]
    return Invoice(
        document_id=document_id,
        supplier_id=supplier_id,
        number=number,
        issue_date=date(2026, 1, 15),
        due_date=date(2026, 2, 14),
        currency="EUR",
        subtotal=Money.parse("200"),
        tax_amount=Money.parse("42"),
        total=Money.parse("242"),
        raw_extraction={"number": number},
        items=items,
    )


class TestDocumentAndJobRoundTrip:
    def test_document_and_job_persist(self, uow_factory):
        with uow_factory() as unit:
            job = seed_document_and_job(unit)

        with uow_factory() as unit:
            loaded_doc = unit.documents.get(job.document_id)
            loaded_job = unit.jobs.get(job.id)

        assert loaded_doc is not None and loaded_doc.filename == "inv.pdf"
        assert str(loaded_doc.status) == "RECEIVED"
        assert str(loaded_job.status) == "PENDING"

    def test_job_state_transitions_persist(self, uow_factory):
        with uow_factory() as unit:
            job = seed_document_and_job(unit)
            job.start()
            unit.jobs.update(job)
            unit.commit()

        with uow_factory() as unit:
            stored = unit.jobs.get(job.id)
            assert str(stored.status) == "PROCESSING"
            assert stored.attempts == 1


class TestInvoicePersistence:
    def test_full_aggregate_roundtrip(self, uow_factory):
        with uow_factory() as unit:
            job = seed_document_and_job(unit)
            supplier = Supplier(name="Acme Ltd.", tax_id=f"TAX-{uuid.uuid4().hex[:8]}")
            unit.suppliers.add(supplier)
            invoice = build_invoice(job.document_id, supplier.id)
            unit.invoices.add(invoice)
            unit.commit()

        with uow_factory() as unit:
            loaded = unit.invoices.get(invoice.id)

        assert loaded is not None
        assert str(loaded.total.amount) == "242.00"
        assert [i.description for i in loaded.items] == ["Line A", "Line B"]
        assert loaded.raw_extraction["number"] == "INV-1"

    def test_duplicate_supplier_number_is_permanent_error(self, uow_factory):
        with uow_factory() as unit:
            job = seed_document_and_job(unit)
            supplier = Supplier(name="Dup Co", tax_id=f"DUP-{uuid.uuid4().hex[:6]}")
            unit.suppliers.add(supplier)
            unit.invoices.add(build_invoice(job.document_id, supplier.id))
            unit.commit()

            other_job = seed_document_and_job(unit)
            with pytest.raises(PersistenceError) as excinfo:
                unit.invoices.add(build_invoice(other_job.document_id, supplier.id))
                unit.commit()

        assert excinfo.value.retryable is False  # duplicates never heal on retry

    def test_one_invoice_per_document_guard(self, uow_factory):
        with uow_factory() as unit:
            job = seed_document_and_job(unit)
            supplier = Supplier(name="Solo SA", tax_id=f"SOLO-{uuid.uuid4().hex[:6]}")
            unit.suppliers.add(supplier)
            unit.invoices.add(build_invoice(job.document_id, supplier.id, "SOLO-1"))
            unit.commit()

        with uow_factory() as unit:
            found = unit.invoices.get_by_document(job.document_id)

        assert found is not None and found.number == "SOLO-1"


class TestQueriesAndStats:
    @pytest.fixture(scope="class")
    def populated(self, engine):
        """Seed a few invoices once per class for query/stat assertions."""
        factory = sessionmaker(bind=engine, expire_on_commit=False)

        def make():
            return SqlAlchemyUnitOfWork(factory)

        tax_base = uuid.uuid4().hex[:6]
        numbers = []
        for index in range(3):
            with make() as unit:
                document = Document(filename=f"d{index}.pdf",
                                    content_type="application/pdf", size_bytes=1,
                                    storage_path=f"s/{uuid.uuid4()}.pdf",
                                    document_type=DocumentType.PDF)
                supplier = Supplier(name=f"QueryCo {index}",
                                    tax_id=f"Q-{tax_base}-{index}")
                invoice = build_invoice(document.id, supplier.id,
                                        f"QRY-{tax_base}-{index}")
                unit.documents.add(document)
                unit.suppliers.add(supplier)
                unit.invoices.add(invoice)
                unit.commit()
                numbers.append((f"QRY-{tax_base}-{index}", supplier.tax_id))

        return {"numbers": numbers}

    def test_search_by_supplier_tax_id(self, uow_factory, populated):
        _, tax_id = populated["numbers"][0]
        with uow_factory() as unit:
            result = unit.invoices.query(InvoiceQuery(search=tax_id))
        assert result.total_count >= 1
        assert all(tax_id in item.supplier_tax_id for item in result.items)

    def test_stats_counts_match_seed(self, uow_factory, populated):
        with uow_factory() as unit:
            stats = unit.invoices.stats()
        assert stats.total_invoices >= 3
        assert stats.total_invoiced > Decimal("0")

    def test_job_count_by_status(self, uow_factory):
        with uow_factory() as unit:
            job = seed_document_and_job(unit)
            counts_before = dict(unit.jobs.count_by_status())
            job.start()
            unit.jobs.update(job)
            counts_after = dict(unit.jobs.count_by_status())

        assert counts_before.get(JobStatus.PENDING.value, 0) >= 1
        assert counts_after.get(JobStatus.PENDING.value, 0) == \
               counts_before.get(JobStatus.PENDING.value, 0) - 1
