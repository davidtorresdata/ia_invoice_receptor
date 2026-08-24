"""Tests for the OCR-text normalization applied before rules extraction."""

import pytest

from app.infrastructure.llm.text_normalizer import normalize_invoice_text


class TestAccentsAndCase:
    def test_accents_removed(self):
        assert normalize_invoice_text("Electrónica FACTURACIÓN") == "ELECTRONICA FACTURACION"

    def test_mixed_case_unified_to_upper(self):
        assert normalize_invoice_text("Fecha de EmIsIóN") == "FECHA DE EMISION"


class TestSpacing:
    def test_inner_whitespace_collapsed_per_line(self):
        text = "FECHA\tLIMITE   DE  PAGO:\n  04-Sep-2026  \n"
        assert normalize_invoice_text(text) == "FECHA LIMITE DE PAGO:\n04-SEP-2026"

    def test_blank_lines_dropped(self):
        assert normalize_invoice_text("A\n\n\nB\n") == "A\nB"

    def test_line_structure_preserved(self):
        """Labels on their own line keep their line: value sits below."""
        text = "FECHA LIMITE DE PAGO:\n04/09/2026"
        assert normalize_invoice_text(f"  {text}  \n\n") == text

    @pytest.mark.parametrize("char", ["\u00a0", "\u2013", "\u2019", "\u201c"])
    def test_typographic_chars_translated(self, char):
        normalized = normalize_invoice_text(f"A{char}B")
        assert char not in normalized


class TestRepeatedLetters:
    def test_triple_plus_runs_collapsed(self):
        assert normalize_invoice_text("FFFECHAAA DE PAGOOO") == "FECHA DE PAGO"

    def test_ocr_doubles_collapsed(self):
        assert normalize_invoice_text("FACTURAA ELECTRONICAA") == "FACTURA ELECTRONICA"
        assert normalize_invoice_text("FFECHA") == "FECHA"

    def test_legitimate_spanish_digraphs_kept(self):
        assert normalize_invoice_text("BARRANQUILLA PUERTO") == "BARRANQUILLA PUERTO"
        assert normalize_invoice_text("ACCION PROGRAMA") == "ACCION PROGRAMA"

    def test_digits_never_collapsed(self):
        assert normalize_invoice_text("1166547846") == "1166547846"
        assert normalize_invoice_text("REF 111222333") == "REF 111222333"


class TestExtractorOnNoisyInput:
    """The full extractor must survive OCR noise via normalization."""

    NOISY = (
        "Facturaa Electrónicaa De Venta No. FE3077\n"
        "Moneda:\tCOP\n"
        "FeChA De EMISIÓN:\t18/07/2026 10:43 AM\n"
        "EMISOR:   DIVERS   TEAM S.A.S\n"
        "Nit del Emisor:  NIT  901717791\n"
        "Total a Pagaaaar: COP   770.000,00\n"  # es-CO decimal comma
        "Subtotal: COP 770.000,00\n"
        "Monto IVA: COP 0,00\n"
    )

    def test_noisy_layout_still_extracts(self):
        from app.infrastructure.llm.rules_extractor import RulesInvoiceExtractor

        data = RulesInvoiceExtractor().extract(self.NOISY)
        assert data.number == "FE3077"
        assert data.supplier.name == "DIVERS TEAM S.A.S"
        assert data.issue_date.isoformat() == "2026-07-18"
        assert str(data.total) == "770000.00"

    def test_empty_and_none_safe(self):
        from app.domain.exceptions import LLMExtractionError
        from app.infrastructure.llm.rules_extractor import RulesInvoiceExtractor

        extractor = RulesInvoiceExtractor()
        with pytest.raises(LLMExtractionError):
            extractor.extract("")
