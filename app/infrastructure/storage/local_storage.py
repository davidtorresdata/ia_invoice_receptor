"""Local filesystem blob storage (S3-compatible swap point)."""

import logging
import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from app.domain.exceptions import StorageError
from app.domain.services.document_storage import DocumentStorage

logger = logging.getLogger(__name__)

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]")


class LocalDocumentStorage(DocumentStorage):
    """Writes documents under a dated directory tree inside `root`."""

    def __init__(self, root: Path) -> None:
        self._root = root.expanduser().resolve()

    # ------------------------------------------------------------------ public
    def save(self, document_id: UUID, filename: str, content: bytes) -> str:
        safe_name = _SAFE_NAME_RE.sub("_", Path(filename).name)[:150] or "document"
        now = datetime.now(UTC)
        key = f"{now:%Y/%m}/{document_id}__{safe_name}"

        target = self._resolve(key)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write via temp file in the same directory.
            with tempfile.NamedTemporaryFile(
                dir=target.parent, prefix=".tmp-", delete=False
            ) as handle:
                handle.write(content)
                temp_path = Path(handle.name)
            os.replace(temp_path, target)
        except OSError as exc:
            raise StorageError(f"Could not persist document: {exc}") from exc

        logger.info("Document stored", extra={"storage_key": key, "bytes": len(content)})
        return key

    def get(self, storage_key: str) -> bytes:
        self._ensure_not_traversal(storage_key)
        target = self._resolve(storage_key)
        if not target.is_file():
            raise StorageError(f"Document not found in storage: {storage_key}", retryable=False)
        try:
            return target.read_bytes()
        except OSError as exc:
            raise StorageError(f"Could not read document: {exc}") from exc

    def delete(self, storage_key: str) -> None:
        self._ensure_not_traversal(storage_key)
        try:
            self._resolve(storage_key).unlink(missing_ok=True)
        except OSError as exc:  # best-effort by contract
            logger.warning("Could not delete document %s: %s", storage_key, exc)

    # ------------------------------------------------------------------ helpers
    def _resolve(self, key: str) -> Path:
        candidate = (self._root / key).resolve()
        if not candidate.is_relative_to(self._root):
            raise StorageError(f"Illegal storage key: {key!r}", retryable=False)
        return candidate
