"""Rule-based invoice extractor for real documents (offline, no API).

`LLM_PROVIDER=rules`: parses the OCR text with ordered, format-tolerant
regular expressions targeting Latin-American e-invoices (DIAN/Colombia
layouts such as Claro/COMCEL, DIVERS TEAM, Sporty City) plus common
generic patterns. It identifies:

- supplier entity (Emisor / first company-like line) and its NIT/tax id
- invoice number (REFERENCIA DE PAGO > "Factura Electrónica de Venta No."
  > generic "FACTURA ... No."), matching each layout seen in practice
- issue/due dates (dd/mm/yyyy and dd-Mon-yyyy with EN/ES month abbrevs)
- currency (explicit ISO code, else COP when Colombian markers present)
- subtotal / tax / total with a consistency check (subtotal + tax = total)

Every field lookup records its outcome in an extraction report
(field -> matched pattern name or "no_encontrado"). The report is:

- logged as structured JSON by the worker (grep it to tune patterns),
- summarized IN SPANISH inside the raised `LLMExtractionError` message,
  which the pipeline persists as `processing_jobs.error_message`, so the
  Streamlit upload page shows exactly which field/pattern needs work.

Line items: structured rows are parsed when the layout allows; otherwise a
single aggregate line keeps the shared math convention intact
(item.total = qty x unit_price; total = subtotal + tax).
"""

import logging
import re
from collections.abc import Sequence
from datetime import date
from decimal import Decimal, InvalidOperation

from app.domain.exceptions import LLMExtractionError, PartialExtractionError
from app.domain.services.invoice_extractor import InvoiceExtractor
from app.domain.value_objects.extracted_invoice import (
    ExtractedInvoiceData,
    ExtractedItem,
    ExtractedSupplier,
)
from app.infrastructure.llm.text_normalizer import normalize_invoice_text

logger = logging.getLogger(__name__)

_TWO_PLACES = Decimal("0.01")

_MONTHS = {
    "jan": 1, "ene": 1, "feb": 2, "mar": 3, "apr": 4, "abr": 4,
    "may": 5, "jun": 6, "jul": 7, "aug": 8, "ago": 8, "sep": 9, "set": 9,
    "oct": 10, "nov": 11, "dec": 12, "dic": 12,
}

# Human-readable field labels used both in the user-facing error message
# and in the extraction report.
_FIELD_LABELS_ES = {
    "number": "el número de factura",
    "supplier": "la entidad (proveedor)",
    "supplier_tax_id": "el NIT / identificación tributaria",
    "issue_date": "la fecha de emisión",
    "due_date": "la fecha de vencimiento",
    "total": "el total",
    "subtotal": "el subtotal",
    "tax": "el impuesto (IVA)",
    "items": "las líneas de la factura",
}

_REQUIRED_FIELDS = ("number", "supplier", "issue_date", "total")

_NO_MATCH = "no_encontrado"

# Ordered by specificity; first match wins. Names surface in the report.
_NUMBER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("referencia_de_pago", re.compile(r"REFERENCIA\s+DE\s+PAGO\s*:?\s*\n?\s*(\d{5,25})", re.I)),
    ("factura_electronica_venta_no", re.compile(
        r"Factura\s+Electr[oó]nica\s+(?:de\s+)?Venta\s*[:\n]*\s*"
        r"(?:No\.?|N[°º])?\s*\.?\s*([A-Z]{0,6}-?\d{2,}[\w-]*)",
        re.I,
    )),
    ("generico_factura_no", re.compile(
        r"(?:FACTURA|INVOICE)\b[^\n]{0,80}?No\.?\s*[:.]?\s*([A-Z]{0,6}-?\d{3,})", re.I)),
)

_TAX_ID_PATTERN_NAME = "nit_generico"
_TAX_ID_PATTERN = re.compile(
    r"N\.?I\.?T\.?\s*(?:del\s+Emisor\s*)?[:.\-\s]*([\d]{1,12}(?:[.\-][\d]{1,6}){0,3}(?:-[\d]{1,2})?)",
    re.I,
)

_EMITOR_PATTERN_NAME = "etiqueta_emisor"
_EMITOR_PATTERN = re.compile(r"Emisor\s*:\s*([^\n]{2,120})", re.I)
_COMPANY_LINE_PATTERN_NAME = "primera_linea_empresa"
_COMPANY_LINE_PATTERN = re.compile(
    r"^([A-ZÁÉÍÓÚÑ][\w.,&\- ]{1,80}?(?:S\.?\s?A\.?(?:\s?S\.?)?|LTDA\.?|LTD\.?|INC\.?|SAS))\s*$",
    re.I | re.M,
)

_DATE_DDMMYYYY = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})")
_DATE_DDMMMYYYY = re.compile(r"\b(\d{1,2})-([A-Za-z]{3,4})-(\d{4})")

# Each entry: (pattern_name, label regex fragment with accent tolerance).
# From every fragment we build two matchers: inline ("Label: value")
# and standalone (label alone on its line, value on a following line).
_ISSUE_DATE_LABELS = (
    ("fecha_de_emision", r"Fechas?\s+de\s+[Ee]misi[óo]n"),
    ("fecha_de_expedicion", r"FECHA\s+DE\s+EXPEDICI[OÓ]N"),
    ("fecha_de_validacion", r"Fechas?\s+de\s+Validaci[óo]n"),
    ("periodo_de_facturacion", r"PER[ÍI]ODO\s+DE\s+FACTURACI[OÓ]N"),
)
_DUE_DATE_LABELS = (
    ("fecha_limite_de_pago", r"FECHA\s+L[ÍI]MITE\s+DE\s+PAGO"),
    ("fecha_de_vencimiento", r"Fechas?\s+de\s+Vencimiento"),
    ("fecha_de_pago", r"FECHA\s+DE\s+PAGO"),
)


def _build_date_matchers(
    labels: tuple[tuple[str, str], ...],
) -> list[tuple[str, re.Pattern[str], re.Pattern[str]]]:
    return [
        (
            name,
            re.compile(rf"^\s*{frag}\s*:\s*([^\n]+)$", re.I | re.M),
            re.compile(rf"^\s*{frag}\s*:?\s*$", re.I),
        )
        for name, frag in labels
    ]


_ISSUE_DATE_MATCHERS = _build_date_matchers(_ISSUE_DATE_LABELS)
_DUE_DATE_MATCHERS = _build_date_matchers(_DUE_DATE_LABELS)
# Union of every standalone-label matcher: when scanning below a label we
# skip these lines instead of stopping at them (e.g. Smart prints
# "FECHA DE EXPEDICION" immediately followed by "FECHA DE PAGO" and only
# then the dates).
_ALL_DATE_LABEL_MATCHERS = tuple(
    [rx for _, _, rx in _ISSUE_DATE_MATCHERS] + [rx for _, _, rx in _DUE_DATE_MATCHERS]
)

_MONEY = r"(?:COP|USD|EUR|\$)?\s*-?\s*([\d][\d.,]*)"
_TOTAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("total_a_pagar", re.compile(rf"Total(?:es)?\s+a\s+[Pp]agar\s*:?\s*{_MONEY}", re.I)),
    ("total_venta", re.compile(rf"Total\s+Venta\s*:?\s*{_MONEY}", re.I)),
    ("total_valor", re.compile(rf"^\s*TOTAL\s+VALOR\s*:?\s*{_MONEY}", re.I | re.M)),
    ("total_mayusculas", re.compile(rf"TOTAL\s+A\s+PAGAR\s*:?\s*{_MONEY}")),
    # Lowest priority: bare "Total $X" (PaddleOCR-VL markdown style).
    ("total_simple", re.compile(rf"(?<!\w)Total\s*:?\s*{_MONEY}", re.I)),
)
_SUBTOTAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("subtotal", re.compile(rf"Subtotal\s*:?\s*{_MONEY}", re.I)),
)
_TAX_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("monto_iva", re.compile(rf"Monto\s+IVA\s*:?\s*{_MONEY}", re.I)),
    ("impuestos_iva_inline", re.compile(rf"Impuestos\s+IVA\s*:?\s*{_MONEY}", re.I)),
    # "(19.00%)" style annotations are skipped so the percent digits are
    # never mistaken for the amount; a bare "IVA 19%" with no amount is
    # rejected by the negative lookahead.
    ("iva_etiqueta", re.compile(
        rf"\bIVA\b\s*(?:\([^)]*\))?\s*:?\s*(?![\d.,]+\s*%){_MONEY}", re.I,
    )),
)

_ITEM_ROW_PATTERN = re.compile(
    r"^\s*\d{1,4}\s*\n([A-ZÁÉÍÓÚÑ][^\n]{2,90})\n(\d+\.\d{1,3})\n([\d.,]+)\n([\d.,]+)\n([\d.,]+)\s*$",
    re.M,
)

# Accent-free uppercase: the text arrives normalized by text_normalizer.
_COLOMBIAN_MARKERS = ("NIT", "DIAN", "COP", "BOGOT", "MEDELL", "CUFE")


def _to_decimal(raw: str) -> Decimal:
    """Parse money strings like '$47,799.77', 'COP 770,000.00', '99,900.00'."""
    cleaned = raw.strip().replace("$", "").replace(" ", "").replace("COP", "").replace("USD", "")
    if "," in cleaned and "." in cleaned:
        # The rightmost separator is the decimal one.
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        # Comma-only: decimal comma when followed by exactly 2 digits.
        head, _, tail = cleaned.rpartition(",")
        cleaned = f"{head.replace(',', '')}.{tail}" if len(tail) == 2 else cleaned.replace(",", "")
    elif "." in cleaned:
        # Dot-only: keep as decimal for 1-2 digits ('47799.77', '50.5');
        # 3+ digits mean es-CO thousands ('50.000' = fifty thousand).
        head, _, tail = cleaned.rpartition(".")
        if len(tail) >= 3:
            cleaned = cleaned.replace(".", "")
    try:
        return Decimal(cleaned).quantize(_TWO_PLACES)
    except InvalidOperation as exc:
        raise ValueError(f"unparseable amount {raw!r}") from exc


class RulesInvoiceExtractor(InvoiceExtractor):
    """Offline heuristic extractor for real-world invoice text layouts."""

    def extract(
        self, document_text: str, images: Sequence[bytes] | None = None
    ) -> ExtractedInvoiceData:
        # Vision adapters use `images`; rules parsing is text-only and
        # ignores them.
        # Canonical form first: uppercase, no accents, single spaces,
        # 3+ repeated letters collapsed (see text_normalizer docstring).
        document_text = normalize_invoice_text(document_text or "")
        # Per-call report: never keep state on self (the extractor is a
        # singleton shared across concurrent worker threads).
        report: dict[str, dict[str, str]] = {}

        number = self._find_number(text=document_text or "", report=report)
        supplier = self._find_supplier(text=document_text or "", report=report)
        issue_date = self._find_issue_date(document_text, report=report)
        total = self._find_amount(_TOTAL_PATTERNS, document_text, report=report, field="total")
        due_date = self._find_due_date(document_text, report=report)
        if due_date is None:
            logger.warning(
                "No se encontró %s en el documento (campo opcional)",
                _FIELD_LABELS_ES["due_date"],
                extra={"extraction_field": "due_date", "extraction_status": _NO_MATCH},
            )
        currency, currency_confident = self._find_currency(document_text)

        tax = self._find_amount(_TAX_PATTERNS, document_text, report=report, field="tax",
                                default=Decimal("0"))
        subtotal = self._find_amount(_SUBTOTAL_PATTERNS, document_text, report=report,
                                     field="subtotal")
        if total is not None and (subtotal is None or subtotal + tax != total):
            report["subtotal"] = {
                "status": "derivado",
                "pattern": "calculado_como_total_menos_impuesto",
            }
            subtotal = total - tax

        items, items_from_rows = self._parse_items(
            document_text, subtotal=subtotal or Decimal("0"), tax=tax, report=report
        )

        missing = [field for field in _REQUIRED_FIELDS if report.get(field, {}).get("status") == _NO_MATCH]
        if missing:
            self._log_report(report, level=logging.WARNING)
            detail = "; ".join(f"No se encontró {_FIELD_LABELS_ES[field]}" for field in missing)
            # Pattern-miss is a deterministic content problem: fail fast
            # (permanent) instead of burning Celery retries. When some
            # fields WERE found, raise Partial so the hybrid orchestrator
            # can merge them with the vision model's full extraction.
            if not (number or supplier or issue_date):
                raise LLMExtractionError(
                    f"{detail}. Revise los patrones del extractor o use otro formato.",
                    retryable=False,
                )
            partial: dict = {"supplier": supplier.model_dump()} if supplier else {}
            if number:
                partial["number"] = number
            if issue_date:
                partial["issue_date"] = issue_date.isoformat()
            if due_date:
                partial["due_date"] = due_date.isoformat()
            if currency_confident:
                partial["currency"] = currency
            if items_from_rows:
                partial["items"] = [item.model_dump() for item in items]
            if total is not None:
                # The monetary trio travels as ONE coherent block so the
                # merged invoice keeps internal arithmetic: when the rules
                # found the total they also own subtotal (derived if needed)
                # and tax; otherwise all three come from the vision model.
                partial["subtotal"] = str(subtotal)
                partial["tax"] = str(tax)
                partial["total"] = str(total)
            raise PartialExtractionError(
                f"{detail}. Campos recuperables entregados para fusión híbrida.",
                partial_data=partial,
                missing_fields=missing,
            )

        assert supplier is not None and total is not None and issue_date is not None

        if not items:
            # Pathological doc (e.g. tax == total): no valid line can be built.
            raise LLMExtractionError(
                "No se encontró las líneas de la factura. Revise los patrones "
                "del extractor o use otro formato.",
                retryable=False,
            )

        data = ExtractedInvoiceData(
            number=number,
            issue_date=issue_date,
            due_date=due_date,
            currency=currency,
            subtotal=subtotal or Decimal("0"),
            tax=tax,
            total=total,
            supplier=supplier,
            items=items,
        )
        self._log_report(report, level=logging.INFO)
        return data

    # ------------------------------------------------------------------ report
    @staticmethod
    def _log_report(report: dict[str, dict[str, str]], *, level: int) -> None:
        logger.log(
            level,
            "Reporte de extracción por reglas: %s",
            ", ".join(
                f"{field}={info['status']}({info['pattern']})" for field, info in report.items()
            ),
            extra={"extraction_report": report},
        )

    @staticmethod
    def _record(report: dict[str, dict[str, str]], field: str, pattern: str | None) -> None:
        report[field] = {
            "status": _NO_MATCH if pattern is None else "ok",
            "pattern": pattern or "-",
        }

    # ------------------------------------------------------------------ fields
    @classmethod
    def _find_number(cls, text: str, report: dict[str, dict[str, str]]) -> str | None:
        for name, pattern in _NUMBER_PATTERNS:
            match = pattern.search(text)
            if match:
                cls._record(report, "number", name)
                return match.group(1).strip().strip(":").upper()
        cls._record(report, "number", None)
        return None

    @classmethod
    def _find_supplier(cls, text: str, report: dict[str, dict[str, str]]) -> ExtractedSupplier | None:
        name: str | None = None
        emitor_match = _EMITOR_PATTERN.search(text)
        if emitor_match:
            name = emitor_match.group(1).strip()
            cls._record(report, "supplier", _EMITOR_PATTERN_NAME)
        else:
            for line in text.splitlines()[:20]:
                # Drop an inline "- Nit. ..." suffix before matching.
                cleaned = re.sub(r"\s*-\s*N\.?I\.?T\.?\.?\s*[\d.\-]+\s*$", "", line.strip(), flags=re.I)
                company = _COMPANY_LINE_PATTERN.match(cleaned)
                if company:
                    name = company.group(1).strip().rstrip(",")
                    cls._record(report, "supplier", _COMPANY_LINE_PATTERN_NAME)
                    break
        if not name:
            cls._record(report, "supplier", None)
            return None

        tax_id_match = _TAX_ID_PATTERN.search(text)
        if tax_id_match:
            cls._record(report, "supplier_tax_id", _TAX_ID_PATTERN_NAME)
            tax_id = tax_id_match.group(1)
        else:
            cls._record(report, "supplier_tax_id", None)
            # Valid placeholder (>=6 chars) and deterministic per entity name,
            # so supplier dedup stays consistent when no NIT is printed.
            slug = re.sub(r"[^A-Z0-9]", "", name.upper())[:6] or "UNKNOWN"
            tax_id = f"SIN-NIT-{slug}"
        return ExtractedSupplier(name=name, tax_id=tax_id)

    @staticmethod
    def _parse_date(raw: str) -> date | None:
        raw = raw.strip()
        ddmmyyyy = _DATE_DDMMYYYY.search(raw)
        if ddmmyyyy:
            day, month, year = (int(part) for part in ddmmyyyy.groups())
            try:
                return date(year, month, day)
            except ValueError:
                return None
        ddmonyyyy = _DATE_DDMMMYYYY.search(raw)
        if ddmonyyyy:
            day, mon, year = ddmonyyyy.groups()
            month = _MONTHS.get(mon.lower()[:3])
            if month:
                try:
                    return date(int(year), month, int(day))
                except ValueError:
                    return None
        return None

    @classmethod
    def _labelled_date(
        cls,
        text: str,
        matchers: list[tuple[str, re.Pattern[str], re.Pattern[str]]],
        report: dict[str, dict[str, str]],
        field: str,
    ) -> date | None:
        """Date after `Label:` inline, or on a nearby line below the label."""
        for name, inline, _ in matchers:
            for match in inline.finditer(text):
                parsed = cls._parse_date(match.group(1))
                if parsed:
                    cls._record(report, field, name)
                    return parsed

        lines = [line.strip() for line in text.splitlines()]
        standalone = [(name, rx) for name, _, rx in matchers]
        for index, line in enumerate(lines):
            matched_name = next((n for n, rx in standalone if rx.match(line)), None)
            if matched_name is None:
                continue
            for candidate in (x for x in lines[index + 1 : index + 6] if x):
                if any(rx.match(candidate) for rx in _ALL_DATE_LABEL_MATCHERS):
                    continue
                parsed = cls._parse_date(candidate)
                if parsed:
                    cls._record(report, field, f"{matched_name}_linea_siguiente")
                    return parsed
        cls._record(report, field, None)
        return None

    @classmethod
    def _find_issue_date(cls, text: str, report: dict[str, dict[str, str]]) -> date | None:
        labelled = cls._labelled_date(text, _ISSUE_DATE_MATCHERS, report, "issue_date")
        if labelled:
            return labelled
        any_date = cls._parse_date(text[:4000])
        if any_date:
            report["issue_date"] = {"status": "ok", "pattern": "primera_fecha_del_documento"}
            return any_date
        cls._record(report, "issue_date", None)
        return None

    @classmethod
    def _find_due_date(cls, text: str, report: dict[str, dict[str, str]]) -> date | None:
        return cls._labelled_date(text, _DUE_DATE_MATCHERS, report, "due_date")

    @classmethod
    def _find_amount(cls, patterns: tuple[tuple[str, re.Pattern[str]], ...], text: str, *,
                     report: dict[str, dict[str, str]], field: str,
                     default: Decimal | None = None) -> Decimal | None:
        for name, pattern in patterns:
            match = pattern.search(text)
            if match:
                try:
                    value = _to_decimal(match.group(1))
                except ValueError:
                    continue
                cls._record(report, field, name)
                return value
        if default is None:
            cls._record(report, field, None)
        else:
            report.setdefault(field, {"status": "por_defecto", "pattern": "-"})
        return default

    @staticmethod
    def _find_currency(text: str) -> tuple[str, bool]:
        """Returns (code, confident): confident=False only for the USD guess."""
        iso = re.search(r"\b(COP|USD|EUR|GBP)\b", text)
        if iso:
            return iso.group(1), True
        if any(marker in text for marker in _COLOMBIAN_MARKERS):
            return "COP", True
        return "USD", False

    @staticmethod
    def _parse_items(
        text: str, *, subtotal: Decimal, tax: Decimal,
        report: dict[str, dict[str, str]] | None = None,
    ) -> tuple[list[ExtractedItem], bool]:
        items: list[ExtractedItem] = []
        for match in _ITEM_ROW_PATTERN.finditer(text):
            description, quantity, unit_price, line_tax, line_total = match.groups()
            items.append(
                ExtractedItem(
                    description=description.strip(),
                    quantity=_to_decimal(quantity),
                    unit_price=_to_decimal(unit_price),
                    tax=_to_decimal(line_tax),
                    total=_to_decimal(line_total),
                )
            )
        from_rows = bool(items)
        if report is not None:
            report["items"] = {"status": "ok" if from_rows else _NO_MATCH,
                               "pattern": "fila_estructurada" if from_rows else "-"}
        if not items and subtotal > 0:
            items.append(
                ExtractedItem(
                    description="Conceptos facturados (agregado del documento)",
                    quantity=Decimal("1"),
                    unit_price=subtotal,
                    tax=tax,
                    total=subtotal,
                )
            )
        return items, from_rows


def ensure_items_payload(payload: dict) -> None:
    """Synthesize one aggregate item when none were parsed.

    Summary-only documents (vision models and OCR text alike) may omit
    line items; the domain contract requires >= 1, so derive a single
    aggregate row from the subtotal instead of rejecting the extraction.
    """
    if payload.get("items"):
        return
    subtotal = payload.get("subtotal")
    if subtotal in (None, "", 0):
        return  # nothing to derive from; let validation raise a clear error
    payload["items"] = [{
        "description": "Conceptos facturados (agregado del documento)",
        "quantity": "1",
        "unit_price": str(subtotal),
        "tax": str(payload.get("tax") or 0),
        "total": str(subtotal),
    }]


__all__ = ["RulesInvoiceExtractor", "ensure_items_payload"]
