"""Storage adapter factory."""

from app.config.settings import Settings
from app.domain.services.document_storage import DocumentStorage
from app.infrastructure.storage.local_storage import LocalDocumentStorage


def build_document_storage(settings: Settings) -> DocumentStorage:
    """
    Composition root for document blobs.

    Migrating to S3/MinIO later = implement S3DocumentStorage + entry here;
    the database keeps pointing at opaque keys either way.
    """
    return LocalDocumentStorage(root=settings.storage_path)


__all__ = ["build_document_storage"]
