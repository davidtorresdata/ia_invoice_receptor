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
    "JAN": 1, "ENE": 1, "FEB": 2, "MAR": 3, "APR": 4, "ABR": 4,
    "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8, "AGO": 8, "SEP": 9, "SET": 9,
    "OCT": 10, "NOV": 11, "DEC": 12, "DIC": 12,
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
# NOTE: every pattern below works on the CANONICAL text produced by
# normalize_invoice_text() — uppercase, accent-free — so no re.I flag and
# no accent alternations are needed (they would be dead branches).
_NUMBER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("referencia_de_pago", re.compile(r"REFERENCIA\s+DE\s+PAGO\s*:?\s*\n?\s*(\d{5,25})")),
    ("factura_electronica_venta_no", re.compile(
        r"FACTURA\s+ELECTRONICA\s+(?:DE\s+)?VENTA\s*[:\n]*\s*"
        r"(?:NO\.?|N[°º])?\s*\.?\s*([A-Z]{0,6}-?\d{2,}[\w-]*)",
    )),
    ("generico_factura_no", re.compile(
        r"(?:FACTURA|INVOICE)\b[^\n]{0,80}?NO\.?\s*[:.]?\s*([A-Z]{0,6}-?\d{3,})")),
    # Prefixed code on its own line ("FV - 137"): common Facturatech style.
    ("numero_prefijo_guion_linea", re.compile(
        r"^\s*([A-Z]{1,5})\s*-\s*(\d{3,10})\s*$", re.M)),
    # Bare "No. 11052" alone on a line (Alegra POS receipts). Capped at 10
    # digits so DIAN authorization numbers (exactly 14, starting 1876...)
    # can never be mistaken for an invoice number.
    ("numero_solo_prefijo_no", re.compile(r"^\s*NO\.?\s*[:.]?\s*(\d{3,10})\s*$", re.M)),
)

# Matches ending right before a candidate number when that candidate sits in
# an authorization/resolution sentence (DIAN numeración), which must never be
# taken as the invoice number.
_AUTH_CONTEXT_PATTERN = re.compile(
    r"\b(?:AUTORIZACI\w*|RESOLUCI\w*|NUMERACI\w*|HABILITA)\b[^:\n]{0,40}$",
)

_TAX_ID_PATTERN_NAME = "nit_generico"
_TAX_ID_PATTERN = re.compile(
    r"N\.?I\.?T\.?\s*(?:DEL\s+EMISOR\s*)?[:.\-\s]*([\d]{1,12}(?:[.\-][\d]{1,6}){0,3}(?:-[\d]{1,2})?)",
)

_EMITOR_PATTERN_NAME = "etiqueta_emisor"
_EMITOR_PATTERN = re.compile(r"EMISOR\s*:\s*([^\n]{2,120})")
_COMPANY_LINE_PATTERN_NAME = "primera_linea_empresa"
# Trailing "." tolerated: "… S.A.S." (with final dot) is as common as "S.A.S".
_COMPANY_LINE_PATTERN = re.compile(
    r"^([A-Z][\w.,&\- ]{1,80}?(?:S\.?\s?A\.?(?:\s?S\.?)?|LTDA\.?|LTD\.?|INC\.?|SAS))\.?,?\s*$",
    re.M,
)
# Lines whose NIT belongs to the e-invoice PLATFORM (not the supplier):
# billing-platform footers would otherwise win the "first NIT in text" race.
_PLATFORM_LINE_PATTERN = re.compile(
    r"PLATAFORMA\s*:|ELABORAD[OA]S?\s+EN|GENERAD[OA]S?\s+(?:EN|POR)|"
    r"PROVEEDOR\s+TECNOLOGICO|SOFTWARE\s*:|POWERED\s+BY",
)
_NIT_IN_LINE_PATTERN_NAME = "nit_con_nombre_cercano"
_NIT_IN_LINE_PATTERN = re.compile(
    r"^(.*?)\bN\.?I\.?T\.?\s*(?:DEL\s+EMISOR\s*)?[:.\-\s]*"
    r"([\d]{1,12}(?:[.\-][\d]{1,6}){0,3}(?:-[\d]{1,2})?)\s*$",
)

_DATE_DDMMYYYY = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})")
_DATE_ISO_YYYYMMDD = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})(?!\d)")
_DATE_DDMMMYYYY = re.compile(r"\b(\d{1,2})-([A-Z]{3,4})-(\d{4})")

# Each entry: (pattern_name, label regex fragment with accent tolerance).
# From every fragment we build two matchers: inline ("Label: value")
# and standalone (label alone on its line, value on a following line).
_ISSUE_DATE_LABELS = (
    ("fecha_de_emision", r"FECHAS?\s+DE\s+EMISION"),
    ("fecha_de_expedicion", r"FECHA\s+DE\s+EXPEDICION"),
    ("fecha_de_validacion", r"FECHAS?\s+DE\s+VALIDACION"),
    ("periodo_de_facturacion", r"PERIODO\s+DE\s+FACTURACION"),
    # Atrapalo/FACTURE-style headers print the label alone and the ISO date
    # on the following line.
    ("fecha_de_factura", r"FECHAS?\s+DE\s+FACTURA"),
    ("fecha_de_generacion", r"FECHAS?\s+DE\s+GENERACION"),
)
_DUE_DATE_LABELS = (
    ("fecha_limite_de_pago", r"FECHA\s+LIMITE\s+DE\s+PAGO"),
    ("fecha_de_vencimiento", r"FECHAS?\s+DE\s+VENCIMIENTO"),
    ("fecha_de_pago", r"FECHA\s+DE\s+PAGO"),
)


def _build_date_matchers(
    labels: tuple[tuple[str, str], ...],
) -> list[tuple[str, re.Pattern[str], re.Pattern[str]]]:
    return [
        (
            name,
            re.compile(rf"^\s*{frag}\s*:\s*([^\n]+)$", re.M),
            re.compile(rf"^\s*{frag}\s*:?\s*$", re.M),
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
    ("total_a_pagar", re.compile(rf"TOTAL(?:ES)?\s+A\s+PAGAR\s*:?\s*{_MONEY}")),
    ("total_venta", re.compile(rf"TOTAL\s+VENTA\s*:?\s*{_MONEY}")),
    ("total_valor", re.compile(rf"^\s*TOTAL\s+VALOR\s*:?\s*{_MONEY}", re.M)),
    # Lowest priority: bare "Total $X" (PaddleOCR-VL markdown style).
    ("total_simple", re.compile(rf"(?<!\w)TOTAL\s*:?\s*{_MONEY}")),
)
_SUBTOTAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("subtotal", re.compile(rf"SUBTOTAL\s*:?\s*{_MONEY}")),
)
_TAX_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("monto_iva", re.compile(rf"MONTO\s+IVA\s*:?\s*{_MONEY}")),
    ("impuestos_iva_inline", re.compile(rf"IMPUESTOS\s+IVA\s*:?\s*{_MONEY}")),
    # "(19.00%)" style annotations are skipped so the percent digits are
    # never mistaken for the amount; a bare "IVA 19%" with no amount is
    # rejected by the negative lookahead.
    ("iva_etiqueta", re.compile(
        rf"\bIVA\b\s*(?:\([^)]*\))?\s*:?\s*(?![\d.,]+\s*%){_MONEY}",
    )),
)

_ITEM_ROW_PATTERN = re.compile(
    r"^\s*\d{1,4}\s*\n([A-Z][^\n]{2,90})\n(\d+\.\d{1,3})\n([\d.,]+)\n([\d.,]+)\n([\d.,]+)\s*$",
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
            for match in pattern.finditer(text):
                if cls._in_authorization_context(text, match.start()):
                    continue
                value = "-".join(g for g in match.groups() if g) \
                    if name == "numero_prefijo_guion_linea" else match.group(1)
                cls._record(report, "number", name)
                return value.strip().strip(":").upper()
        cls._record(report, "number", None)
        return None

    @staticmethod
    def _in_authorization_context(text: str, position: int) -> bool:
        """True when the candidate number follows DIAN authorization wording."""
        window = text[max(0, position - 60):position]
        return bool(_AUTH_CONTEXT_PATTERN.search(window))

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
                cleaned = re.sub(r"\s*-\s*N\.?I\.?T\.?\.?\s*[\d.\-]+\s*$", "", line.strip())
                # Duplicated brand segments ("X.co - X.co S.A.S."): prefer
                # the dash-separated segment carrying a company suffix.
                segments = [seg.strip(" .,-") for seg in cleaned.split("-")] if "-" in cleaned else []
                suffixed = next((s for s in segments if _COMPANY_LINE_PATTERN.match(s)), None)
                company = _COMPANY_LINE_PATTERN.match(suffixed or cleaned)
                if company:
                    name = company.group(1).strip().rstrip(",")
                    cls._record(report, "supplier", _COMPANY_LINE_PATTERN_NAME)
                    break

        nit_line_tax_id: str | None = None
        if not name:
            # Last resort: anchor on a NIT line (skipping e-invoice PLATFORM
            # footers like Alegra/Facturatech) and take the name from the
            # same line's prefix or from the nearest meaningful line above.
            lines = text.splitlines()
            for index, line in enumerate(lines):
                if _PLATFORM_LINE_PATTERN.search(line):
                    continue
                nit_match = _NIT_IN_LINE_PATTERN.match(line.strip())
                if not nit_match:
                    continue
                candidate = cls._name_from_nit_prefix(nit_match.group(1))
                if candidate is None:
                    candidate = cls._name_above_nit(lines, index)
                if candidate is None:
                    continue
                name = candidate
                nit_line_tax_id = nit_match.group(2)
                cls._record(report, "supplier", _NIT_IN_LINE_PATTERN_NAME)
                break

        if not name:
            cls._record(report, "supplier", None)
            return None

        tax_id_match = _TAX_ID_PATTERN.search(
            "\n".join(
                line for line in text.splitlines() if not _PLATFORM_LINE_PATTERN.search(line)
            )
        )
        if nit_line_tax_id:
            cls._record(report, "supplier_tax_id", _TAX_ID_PATTERN_NAME)
            tax_id = nit_line_tax_id
        elif tax_id_match:
            cls._record(report, "supplier_tax_id", _TAX_ID_PATTERN_NAME)
            tax_id = tax_id_match.group(1)
        else:
            cls._record(report, "supplier_tax_id", None)
            # Valid placeholder (>=6 chars) and deterministic per entity name,
            # so supplier dedup stays consistent when no NIT is printed.
            slug = re.sub(r"[^A-Z0-9]", "", name)[:6] or "UNKNOWN"
            tax_id = f"SIN-NIT-{slug}"
        return ExtractedSupplier(name=name, tax_id=tax_id)

    @staticmethod
    def _name_from_nit_prefix(prefix: str) -> str | None:
        """Supplier name printed on the SAME line, left of the NIT."""
        cleaned = prefix.strip().strip("-:., ").strip()
        if len(cleaned) < 3:
            return None
        if "-" in cleaned:
            segments = [seg.strip() for seg in cleaned.split("-") if seg.strip()]
            suffixed = next((s for s in segments if _COMPANY_LINE_PATTERN.match(s)), None)
            if suffixed:
                return suffixed.rstrip(",")
        return cleaned.rstrip(",") or None

    @staticmethod
    def _name_above_nit(lines: list[str], nit_index: int) -> str | None:
        """Nearest plausible entity name ABOVE a bare `NIT ...` line."""
        for candidate in reversed(lines[max(0, nit_index - 5):nit_index]):
            stripped = candidate.strip()
            if not stripped or len(stripped) < 4 or len(stripped) > 90:
                continue
            if _PLATFORM_LINE_PATTERN.search(stripped):
                continue
            if _NIT_IN_LINE_PATTERN.match(stripped):
                continue
            lowered = stripped
            if any(lowered.startswith(w) for w in ("TEL", "CEL", "EMAIL", "WWW.", "HTTP", "DIR")):
                continue
            if re.fullmatch(r"[\d\s.,:/-]+", stripped):
                continue
            return stripped.rstrip(",;- ")
        return None

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
        iso = _DATE_ISO_YYYYMMDD.search(raw)
        if iso:
            year, month, day = (int(part) for part in iso.groups())
            try:
                return date(year, month, day)
            except ValueError:
                return None
        ddmonyyyy = _DATE_DDMMMYYYY.search(raw)
        if ddmonyyyy:
            day, mon, year = ddmonyyyy.groups()
            month = _MONTHS.get(mon[:3])
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
