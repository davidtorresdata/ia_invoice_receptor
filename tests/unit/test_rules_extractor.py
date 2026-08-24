"""Tests for the rule-based offline invoice extractor."""

import pytest

from app.domain.exceptions import LLMExtractionError
from app.infrastructure.llm.rules_extractor import RulesInvoiceExtractor, _to_decimal

CLARO_TEXT = """COMCEL S.A.
NIT 800.153.993-7
FACTURA ELECTRÓNICA DE VENTA:
FACTURA ELECTRÓNICA DE VENTA:
FACTURA ELECTRÓNICA DE VENTA: E 6096510400
FECHA LÍMITE DE PAGO:
FECHA LÍMITE DE PAGO:
FECHA LÍMITE DE PAGO:
04-Sep-2026
REFERENCIA DE PAGO:
REFERENCIA DE PAGO:
1166547846
TOTAL A PAGAR:
$47,799.77
PERÍODO DE FACTURACIÓN:
18-Jul-2026 a 17-Ago-2026
Impuestos IVA:7,619.64 Consumo:77.01
"""

DIVERS_TEXT = """Factura Electrónica De Venta No. FE3077
Moneda: COP
Fecha de Emisión: 18/07/2026 10:43 AM
Emisor: DIVERS TEAM S.A.S
Nit del Emisor: 901717791
Subtotal:
COP 770,000.00
Monto IVA:
COP 0.00
Total a pagar:
COP 770,000.00
"""

SMART_TEXT = """Factura Electrónica de Venta
No.FESS34120852
FECHA DE EXPEDICION
13/08/2026 10:02:00 AM
SPORTY CITY S.A.S. - Nit. 900777063-3
Total Valor:
99,900.00
0.00
99,900.00
Total a Pagar:
99,900.00
"""


@pytest.fixture
def extractor() -> RulesInvoiceExtractor:
    return RulesInvoiceExtractor()


class TestInvoiceNumber:
    def test_claro_payment_reference_wins(self, extractor):
        assert extractor.extract(CLARO_TEXT).number == "1166547846"

    def test_divers_prefixed_number(self, extractor):
        assert extractor.extract(DIVERS_TEXT).number == "FE3077"

    def test_smart_number_on_next_line(self, extractor):
        assert extractor.extract(SMART_TEXT).number == "FESS34120852"


class TestSupplierEntity:
    def test_first_company_line_with_nit(self, extractor):
        data = extractor.extract(CLARO_TEXT)
        assert data.supplier.name == "COMCEL S.A."
        assert data.supplier.tax_id == "800.153.993-7"

    def test_emisor_label(self, extractor):
        data = extractor.extract(DIVERS_TEXT)
        assert data.supplier.name == "DIVERS TEAM S.A.S"
        assert data.supplier.tax_id == "901717791"

    def test_company_line_with_inline_nit(self, extractor):
        data = extractor.extract(SMART_TEXT)
        assert data.supplier.name == "SPORTY CITY S.A.S."
        assert data.supplier.tax_id == "900777063-3"


class TestDatesAndAmounts:
    def test_claro_dates_and_totals(self, extractor):
        data = extractor.extract(CLARO_TEXT)
        assert data.issue_date.isoformat() == "2026-07-18"
        assert data.due_date is not None and data.due_date.isoformat() == "2026-09-04"
        assert str(data.total) == "47799.77"
        assert str(data.tax) == "7619.64"
        assert data.subtotal + data.tax == data.total

    def test_divers_cop_totals(self, extractor):
        data = extractor.extract(DIVERS_TEXT)
        assert data.currency == "COP"
        assert str(data.total) == "770000.00"
        assert str(data.tax) == "0.00"

    def test_smart_ddmmyyyy_issue_date(self, extractor):
        data = extractor.extract(SMART_TEXT)
        assert data.issue_date.isoformat() == "2026-08-13"
        assert str(data.total) == "99900.00"

    def test_items_keep_math_convention(self, extractor):
        for text in (CLARO_TEXT, DIVERS_TEXT, SMART_TEXT):
            data = extractor.extract(text)
            assert sum(item.total for item in data.items) == data.subtotal


class TestFailures:
    def test_supplier_without_nit_gets_valid_deterministic_placeholder(self, extractor):
        text = "Factura Electrónica De Venta No. X-777\nEmisor: ACME S.A.S\n" \
               "Fecha de Emisión: 01/08/2026\nTotal a Pagar: COP 123,456.78\n"
        data = extractor.extract(text)
        assert data.supplier.tax_id == "SIN-NIT-ACMESA"

    def test_unparseable_text_raises_llm_error(self, extractor):
        with pytest.raises(LLMExtractionError):
            extractor.extract("hello world this is not an invoice")

    def test_error_message_lists_every_missing_field(self, extractor):
        with pytest.raises(LLMExtractionError) as excinfo:
            extractor.extract("hello world this is not an invoice")
        message = str(excinfo.value)
        for expected in (
            "No se encontró el número de factura",
            "No se encontró la entidad (proveedor)",
            "No se encontró la fecha de emisión",
            "No se encontró el total",
        ):
            assert expected in message

    def test_missing_field_names_the_pattern_to_tune(
        self, extractor, caplog
    ):
        with pytest.raises(LLMExtractionError), caplog.at_level("WARNING"):
            extractor.extract("hello world this is not an invoice")
        reports = [
            record.extraction_report
            for record in caplog.records
            if hasattr(record, "extraction_report")
        ]
        assert reports and "number" in reports[-1]
        assert reports[-1]["number"]["status"] == "no_encontrado"

    def test_missing_due_date_logs_warning_not_error(self, extractor, caplog):
        with caplog.at_level("WARNING"):
            data = extractor.extract(DIVERS_TEXT)
        assert data.due_date is None
        assert any(
            "fecha de vencimiento" in record.getMessage() for record in caplog.records
        )


class TestMoneyParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("$47,799.77", "47799.77"),
            ("COP 770,000.00", "770000.00"),
            ("99,900.00", "99900.00"),
            ("1234", "1234.00"),
            ("770000.5", "770000.50"),
        ],
    )
    def test_formats(self, raw, expected):
        assert str(_to_decimal(raw)) == expected
