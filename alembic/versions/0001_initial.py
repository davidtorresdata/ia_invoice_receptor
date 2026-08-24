"""initial schema: documents, suppliers, invoices, invoice_items, processing_jobs

Revision ID: 0001
Revises:
Create Date: 2026-01-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op
from app.domain.value_objects.enums import (
    DocumentStatus,
    DocumentType,
    JobStatus,
)
from app.infrastructure.database.base import str_enum

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ---------------------------------------------------------------- documents
    op.create_table(
        "documents",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("storage_path", sa.String(length=512), nullable=False),
        sa.Column("document_type", str_enum(DocumentType), nullable=False),
        sa.Column("status", str_enum(DocumentStatus), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("storage_path", name=op.f("uq_documents_storage_path")),
    )
    op.create_index(
        "ix_documents_status_created_at",
        "documents",
        [sa.text("status"), sa.text("created_at DESC")],
    )

    # ---------------------------------------------------------------- suppliers
    op.create_table(
        "suppliers",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("tax_id", sa.String(length=64), nullable=False),
        sa.Column("address", sa.String(length=500)),
        sa.Column("phone", sa.String(length=50)),
        sa.Column("email", sa.String(length=255)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("tax_id", name=op.f("uq_suppliers_tax_id")),
    )
    op.create_index(op.f("ix_suppliers_tax_id"), "suppliers", ["tax_id"], unique=True)

    # ----------------------------------------------------------------- invoices
    op.create_table(
        "invoices",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        # FKs are declared as NAMED table-level constraints below (single
        # source of truth matching the ORM naming convention).
        sa.Column("document_id", UUID(as_uuid=True), nullable=False),
        sa.Column("supplier_id", UUID(as_uuid=True), nullable=False),
        sa.Column("number", sa.String(length=100), nullable=False),
        sa.Column("issue_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date()),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("subtotal", sa.Numeric(14, 2), nullable=False),
        sa.Column("tax_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("total", sa.Numeric(14, 2), nullable=False),
        sa.Column("validation_status", sa.String(length=16), nullable=False,
                  server_default="PENDING"),
        sa.Column("validation_report", JSONB()),
        sa.Column("raw_extraction", JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"],
                                name=op.f("fk_invoices_document_id_documents"),
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"],
                                name=op.f("fk_invoices_supplier_id_suppliers"),
                                ondelete="RESTRICT"),
        sa.UniqueConstraint("supplier_id", "number",
                            name=op.f("uq_invoices_supplier_number")),
        sa.UniqueConstraint("document_id", name=op.f("uq_invoices_document_id")),
    )
    op.create_index(op.f("ix_invoices_document_id"), "invoices", ["document_id"], unique=True)
    op.create_index(op.f("ix_invoices_supplier_id"), "invoices", ["supplier_id"])
    op.create_index(op.f("ix_invoices_issue_date"), "invoices", ["issue_date"])
    op.create_index(op.f("ix_invoices_validation_status"), "invoices", ["validation_status"])

    # ------------------------------------------------------------ invoice_items
    op.create_table(
        "invoice_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("invoice_id", UUID(as_uuid=True), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("quantity", sa.Numeric(12, 3), nullable=False),
        sa.Column("unit_price", sa.Numeric(14, 2), nullable=False),
        sa.Column("tax_amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("total", sa.Numeric(14, 2), nullable=False),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"],
                                name=op.f("fk_invoice_items_invoice_id_invoices"),
                                ondelete="CASCADE"),
    )
    op.create_index(op.f("ix_invoice_items_invoice_id"), "invoice_items", ["invoice_id"])

    # ----------------------------------------------------------- processing_jobs
    op.create_table(
        "processing_jobs",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", UUID(as_uuid=True), nullable=False),
        sa.Column("invoice_id", UUID(as_uuid=True)),
        sa.Column("status", str_enum(JobStatus), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("celery_task_id", sa.String(length=64)),
        sa.Column("error_message", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"],
                                name=op.f("fk_processing_jobs_document_id_documents"),
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invoice_id"], ["invoices.id"],
                                name=op.f("fk_processing_jobs_invoice_id_invoices"),
                                ondelete="SET NULL"),
    )
    op.create_index(op.f("ix_processing_jobs_document_id"), "processing_jobs", ["document_id"])
    op.create_index(op.f("ix_processing_jobs_invoice_id"), "processing_jobs", ["invoice_id"])
    op.create_index(
        "ix_processing_jobs_status_created_at",
        "processing_jobs",
        [sa.text("status"), sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_table("processing_jobs")
    op.drop_table("invoice_items")
    op.drop_table("invoices")
    op.drop_table("suppliers")
    op.drop_table("documents")
