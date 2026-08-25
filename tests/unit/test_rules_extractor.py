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


BUCE0_TEXT = """Elaborado en Soluciones Alegra S.A.S NIT 900.559.088-2 / www.alegra.com
Moneda: COP
LINA RICO MULLER
NIT 1020723830-3
Tel: +574227362
www.atlantidabucea.com
Fecha y hora de expedición: 2025-05-16T14:51:15
Subtotal
$3.180.000
No. 11052
Total
$3.180.000
"""

PARAPENTE_TEXT = """PLATAFORMA: Facturatech Nit. 901.143.311-8
GLORIA YADIRA BELTRAN GUTIERREZ
NIT: 37900991-0
Autorización factura electrónica de venta No. 18764092313021 válida desde 2025-04-24
FACTURA ELECTRÓNICA DE VENTA :
FV - 137
FECHA FIRMADO:
26/06/2025 12:31:24
MONEDA:
COP Colombia, Pesos
Subtotal:
$1.656.000,00
Total:
$1.656.000,00
"""

TRADUCTORES_TEXT = """Cliente: Cristian David Torres Amado
Moneda: COP
Traductores.co
Traductores.co -  Traductores.co S.A.S. - NIT 900755117-8
Software: Alegra  -  NIT 900.559.088-2
Proveedor tecnológico: Soluciones Alegra S.A.S
FACTURA ELECTRÓNICA DE VENTA
 FE364
Fecha de creación: 15/06/2026
Subtotal
$136.135
Iva (19.00%)
$25.866
Total
$162.000
"""

ATRAPALO_TEXT = """ATRAPALO COLOMBIA S.A.S.
NIT. 900413476-1
Autorización facturación electrónica No. 18764112289708
Habilta del FE2638431 al: FE3000000 del: 2026-07-08 al 2027-07-08.
Página 1 de 1
Factura Electrónica De Venta FE2675608
Fecha de Factura:
2026-08-23
TOTAL
92.166
CUFE: e8f8e93ca65563aa6dbf7801a778b52d702d95515a4f75101d98a1dc2942bf0eb1c4b049c72c00567debff31156f45e2
Fecha de Generación: 2026-08-23
"""


class TestRealWorldLayouts:
    def test_alegra_receipt_bare_number_and_name_above_nit(self, extractor):
        data = extractor.extract(BUCE0_TEXT)
        assert data.number == "11052"
        assert data.supplier.name == "LINA RICO MULLER"
        assert data.supplier.tax_id == "1020723830-3"
        assert data.issue_date.isoformat() == "2025-05-16"
        assert str(data.total) == "3180000.00"

    def test_platform_nit_is_never_the_supplier(self, extractor):
        data = extractor.extract(PARAPENTE_TEXT)
        assert data.supplier.name == "GLORIA YADIRA BELTRAN GUTIERREZ"
        assert data.supplier.tax_id == "37900991-0"
        assert data.supplier.tax_id != "9011433118"

    def test_authorization_number_is_not_the_invoice_number(self, extractor):
        assert extractor.extract(PARAPENTE_TEXT).number == "FV-137"
        atrapalo = extractor.extract(ATRAPALO_TEXT)
        assert atrapalo.number == "FE2675608"
        assert atrapalo.number != "18764112289708"

    def test_same_line_brand_dash_nit_with_trailing_dot(self, extractor):
        data = extractor.extract(TRADUCTORES_TEXT)
        assert data.supplier.name == "TRADUCTORES.CO S.A.S"
        assert data.supplier.tax_id == "900755117-8"
        assert data.issue_date.isoformat() == "2026-06-15"

    def test_issue_date_on_line_after_label_and_iso_inline(self, extractor):
        data = extractor.extract(ATRAPALO_TEXT)
        assert data.issue_date.isoformat() == "2026-08-23"
