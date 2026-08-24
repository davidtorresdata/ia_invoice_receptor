"""File type validation: extension + MIME declaration + magic-byte sniffing.

Pure logic over bytes — no third-party dependency (no libmagic), fully unit
testable. The use-case layer composes these checks with the size limit.
"""

import unicodedata
from dataclasses import dataclass

from app.domain.exceptions import EmptyFileError, InvalidFileError
from app.domain.value_objects.enums import DocumentType

ALLOWED_EXTENSIONS: frozenset[str] = frozenset({"pdf", "png", "jpg", "jpeg"})

DECLARED_MIME_TYPES: dict[str, DocumentType] = {
    "application/pdf": DocumentType.PDF,
    "image/png": DocumentType.IMAGE,
    "image/jpeg": DocumentType.IMAGE,
    "image/jpg": DocumentType.IMAGE,
}

_MAGIC_SIGNATURES: tuple[tuple[bytes, DocumentType, str], ...] = (
    (b"%PDF-", DocumentType.PDF, "PDF"),
    (b"\x89PNG\r\n\x1a\n", DocumentType.IMAGE, "PNG"),
    (b"\xff\xd8\xff", DocumentType.IMAGE, "JPEG"),
)

_PDF_SEARCH_WINDOW = 1024  # spec allows %PDF- header within first 1024 bytes


@dataclass(frozen=True, slots=True)
class FileType:
    """Result of validating an uploaded file's identity."""

    document_type: DocumentType
    extension: str
    signature_format: str  # e.g. "PDF", "PNG", "JPEG"
    mime_type: str

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """Strip paths/accidents; keep a safe, human-readable basename."""
        basename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        normalized = unicodedata.normalize("NFKD", basename).encode("ascii", "ignore").decode()
        # Keep alphanumerics, dots, underscores and dashes; collapse everything else.
        safe = "".join(ch if ch.isalnum() or ch in {".", "_", "-"} else "_" for ch in normalized)
        return safe.strip("._") or "document"

    @classmethod
    def validate(
        cls,
        *,
        content: bytes,
        filename: str,
        declared_mime: str | None = None,
    ) -> "FileType":
        """
        Enforce the upload contract.

        Rules:
          1. Non-empty content.
          2. Extension must be whitelisted.
          3. Declared MIME type (if any) must be whitelisted.
          4. Magic bytes must identify PDF/PNG/JPEG.
          5. Sniffed format must agree with the extension family.
        """
        if not content:
            raise EmptyFileError("Uploaded file is empty")

        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if extension not in ALLOWED_EXTENSIONS:
            raise InvalidFileError(
                f"Extension '.{extension}' is not allowed. "
                f"Allowed: {sorted(ALLOWED_EXTENSIONS)}"
            )

        mime = (declared_mime or "").split(";")[0].strip().lower()
        if mime and mime not in DECLARED_MIME_TYPES:
            raise InvalidFileError(f"MIME type '{mime}' is not allowed")

        sniffed = cls._sniff(content)
        if sniffed is None:
            raise InvalidFileError("File content does not match PDF/PNG/JPEG signatures")

        expected_family = DocumentType.PDF if extension == "pdf" else DocumentType.IMAGE
        if sniffed.document_type != expected_family:
            raise InvalidFileError(
                f"Content looks like {sniffed.signature_format} but extension is '{extension}'"
            )
        if mime and sniffed.document_type != DECLARED_MIME_TYPES[mime]:
            raise InvalidFileError(f"Declared MIME '{mime}' does not match file content")

        return cls(
            document_type=sniffed.document_type,
            extension=extension,
            signature_format=sniffed.signature_format,
            mime_type=mime or _default_mime(sniffed),
        )

    @classmethod
    def _sniff(cls, content: bytes) -> "FileType | None":
        head = content[:_PDF_SEARCH_WINDOW]
        for magic, doc_type, fmt in _MAGIC_SIGNATURES:
            probe = head if magic == b"%PDF-" else content[: len(magic)]
            if magic in probe:
                ext = {DocumentType.PDF: "pdf"}.get(doc_type, fmt.lower())
                return cls(document_type=doc_type, extension=ext, signature_format=fmt,
                           mime_type=_default_mime_by_format(fmt))
        return None


def _default_mime(sniffed: FileType) -> str:
    return _default_mime_by_format(sniffed.signature_format)


def _default_mime_by_format(fmt: str) -> str:
    return {
        "PDF": "application/pdf",
        "PNG": "image/png",
        "JPEG": "image/jpeg",
    }.get(fmt, "application/octet-stream")
