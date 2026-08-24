"""Port for document persistence."""

from abc import ABC, abstractmethod
from uuid import UUID

from app.domain.entities.document import Document


class DocumentRepository(ABC):
    @abstractmethod
    def add(self, document: Document) -> None:
        """Register a new document."""

    @abstractmethod
    def get(self, document_id: UUID) -> Document | None:
        """Fetch by id or None."""

    @abstractmethod
    def update(self, document: Document) -> None:
        """Persist state changes of an existing aggregate."""
