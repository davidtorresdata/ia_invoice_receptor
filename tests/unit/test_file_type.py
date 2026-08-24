"""Unit tests for FileType validation and filename sanitization."""

import pytest

from app.domain.exceptions import EmptyFileError, InvalidFileError
from app.domain.value_objects.enums import DocumentType
from app.domain.value_objects.file_type import FileType

PDF_MAGIC = b"%PDF-1.7\n..."
PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"data"
JPG_MAGIC = b"\xff\xd8\xff\xe0" + b"data"


class TestValidate:
    def test_accepts_pdf(self):
        ft = FileType.validate(content=PDF_MAGIC, filename="invoice.pdf",
                               declared_mime="application/pdf")
        assert ft.document_type == DocumentType.PDF
        assert ft.extension == "pdf"

    def test_accepts_png_without_declared_mime(self):
        ft = FileType.validate(content=PNG_MAGIC, filename="scan.png")
        assert ft.document_type == DocumentType.IMAGE
        assert ft.mime_type == "image/png"

    def test_accepts_jpeg_extension_variant(self):
        ft = FileType.validate(content=JPG_MAGIC, filename="photo.jpeg",
                               declared_mime="image/jpg")
        assert ft.document_type == DocumentType.IMAGE

    @pytest.mark.parametrize("filename", ["invoice.txt", "invoice", "invoice.exe"])
    def test_rejects_bad_extensions(self, filename):
        with pytest.raises(InvalidFileError):
            FileType.validate(content=PDF_MAGIC, filename=filename)

    def test_rejects_disallowed_mime(self):
        with pytest.raises(InvalidFileError):
            FileType.validate(content=PDF_MAGIC, filename="a.pdf",
                              declared_mime="text/html")

    def test_rejects_content_not_matching_signature(self):
        with pytest.raises(InvalidFileError):
            FileType.validate(content=b"just text, not a document",
                              filename="fake.pdf")

    def test_rejects_extension_signature_mismatch(self):
        with pytest.raises(InvalidFileError):
            FileType.validate(content=PDF_MAGIC, filename="renamed.png")

    def test_rejects_empty_content(self):
        with pytest.raises(EmptyFileError):
            FileType.validate(content=b"", filename="empty.pdf")


class TestSanitizeFilename:
    def test_strips_path_traversal(self):
        safe = FileType.sanitize_filename("../../etc/passwd")
        assert "/" not in safe and ".." not in safe

    def test_keeps_readable_name(self):
        assert FileType.sanitize_filename("Mi Factura-2026.PDF") == "Mi_Factura-2026.PDF"

    def test_falls_back_when_empty(self):
        assert FileType.sanitize_filename("///") == "document"
