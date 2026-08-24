"""SQLAlchemy implementation of the DocumentRepository port."""

import uuid

from sqlalchemy.orm import Session

from app.domain.entities.document import Document
from app.domain.exceptions import PersistenceError
from app.domain.repositories.document_repository import DocumentRepository
from app.domain.value_objects.enums import DocumentStatus, DocumentType
from app.infrastructure.database.models import DocumentModel
from app.infrastructure.repositories.mappers import apply_document, document_to_domain


class SqlAlchemyDocumentRepository(DocumentRepository):
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, document: Document) -> None:
        self._session.add(
            DocumentModel(
                id=document.id,
                filename=document.filename,
                content_type=document.content_type,
                size_bytes=document.size_bytes,
                storage_path=document.storage_path,
                document_type=DocumentType(document.document_type),
                status=DocumentStatus(document.status),
            )
        )

    def get(self, document_id: uuid.UUID) -> Document | None:
        model = self._session.get(DocumentModel, document_id)
        return document_to_domain(model) if model else None

    def update(self, document: Document) -> None:
        model = self._session.get(DocumentModel, document.id)
        if model is None:
            raise PersistenceError(f"Document {document.id} not found for update")
        apply_document(document, model)
