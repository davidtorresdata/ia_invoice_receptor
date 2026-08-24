"""Document storage port — blob persistence decoupled from filesystem/S3."""

from abc import ABC, abstractmethod
from uuid import UUID

from app.domain.exceptions import StorageError


class DocumentStorage(ABC):
    """
    Driven port for storing/retrieving raw uploaded documents.

    Keys are opaque strings (local path segments today, S3 object keys later).
    Implementations must be safe against path traversal and concurrent use.
    """

    @abstractmethod
    def save(self, document_id: UUID, filename: str, content: bytes) -> str:
        """
        Persist `content` and return the storage key it was written to.

        Raises:
            StorageError: on write failure.
        """

    @abstractmethod
    def get(self, storage_key: str) -> bytes:
        """
        Read back a previously stored document.

        Raises:
            StorageError: when the key is unknown or unreadable.
        """

    @abstractmethod
    def delete(self, storage_key: str) -> None:
        """Best-effort removal; missing keys must not raise."""

    @staticmethod
    def _ensure_not_traversal(key: str) -> None:
        if ".." in key.replace("\\", "/").split("/"):
            raise StorageError(f"Illegal storage key: {key!r}")
