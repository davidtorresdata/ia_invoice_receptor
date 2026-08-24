"""Business-rule validation service (validation level 3).

Operates purely on domain entities — no Pydantic, no infrastructure.
Checks required fields, date sanity, numeric ranges and the arithmetic
consistency of subtotal/tax/total/items. Produces a traceable
`ValidationReport`; the caller decides how to persist the outcome.
"""

from decimal import Decimal

from app.domain.entities.invoice import Invoice
from app.domain.value_objects.validation import Severity, ValidationIssue, ValidationReport

_DEFAULT_TOLERANCE = Decimal("5")  # invoice-level: absorbs +/- $1-5 rounding gaps
_MAX_LINE_TOLERANCE = Decimal("0.02")
_MAX_FUTURE_DAYS = 1  # allow small clock skew on issue dates


class InvoiceBusinessValidator:
    """Stateless validator; tolerance is configurable for rounding regimes."""

    def __init__(self, tolerance: Decimal = _DEFAULT_TOLERANCE) -> None:
        self._tolerance = tolerance

    @property
    def tolerance(self) -> Decimal:
        return self._tolerance

    def validate(self, invoice: Invoice) -> ValidationReport:
        report = ValidationReport()
        self._check_required_fields(invoice, report)
        self._check_dates(invoice, report)
        self._check_numeric_ranges(invoice, report)
        self._check_math(invoice, report)
        self._check_items(invoice, report)
        return report

    # ------------------------------------------------------------------ fields
    def _check_required_fields(self, invoice: Invoice, report: ValidationReport) -> None:
        if not invoice.number.strip():
            report.add(ValidationIssue("required.number", Severity.ERROR,
                                       "Invoice number is missing", field="number"))
        if invoice.supplier_id is None:  # pragma: no cover - guarded by entity
            report.add(ValidationIssue("required.supplier", Severity.ERROR,
                                       "Supplier reference is missing", field="supplier"))

    # ------------------------------------------------------------------- dates
    def _check_dates(self, invoice: Invoice, report: ValidationReport) -> None:
        today = invoice.issue_date.today()
        days_ahead = (invoice.issue_date - today).days
        if days_ahead > 365:
            report.add(ValidationIssue(
                "date.issue_far_future", Severity.WARNING,
                f"Issue date {invoice.issue_date} is more than one year ahead",
                field="issue_date",
            ))
        elif days_ahead > _MAX_FUTURE_DAYS:
            report.add(ValidationIssue(
                "date.issue_future", Severity.INFO,
                f"Issue date {invoice.issue_date} lies in the future",
                field="issue_date",
            ))
        if invoice.due_date and invoice.due_date < invoice.issue_date:
            report.add(ValidationIssue(
                "date.due_before_issue", Severity.ERROR,
                f"Due date {invoice.due_date} precedes issue date {invoice.issue_date}",
                field="due_date",
            ))

    # ----------------------------------------------------------------- numbers
    def _check_numeric_ranges(self, invoice: Invoice, report: ValidationReport) -> None:
        if invoice.subtotal.amount < 0:
            report.add(ValidationIssue("range.subtotal_negative", Severity.ERROR,
                                       "Subtotal cannot be negative", field="subtotal"))
        if invoice.tax_amount.amount < 0:
            report.add(ValidationIssue("range.tax_negative", Severity.ERROR,
                                       "Tax cannot be negative", field="tax"))
        if invoice.total.amount <= 0:
            report.add(ValidationIssue("range.total_positive", Severity.ERROR,
                                       "Total must be greater than zero", field="total"))
        for index, item in enumerate(invoice.items):
            if item.quantity <= 0:
                report.add(ValidationIssue(
                    "range.item_quantity", Severity.ERROR,
                    f"Item #{index + 1} quantity must be positive", field=f"items[{index}].quantity",
                ))

    # -------------------------------------------------------------------- math
    def _check_math(self, invoice: Invoice, report: ValidationReport) -> None:
        expected_total = invoice.subtotal.add(invoice.tax_amount)
        if not expected_total.is_close(invoice.total, self._tolerance):
            report.add(ValidationIssue(
                "math.total_mismatch", Severity.ERROR,
                f"subtotal ({invoice.subtotal}) + tax ({invoice.tax_amount}) "
                f"!= total ({invoice.total}) [diff {expected_total.amount - invoice.total.amount}]",
                field="total",
            ))

    # ------------------------------------------------------------------- items
    def _check_items(self, invoice: Invoice, report: ValidationReport) -> None:
        if not invoice.items:
            report.add(ValidationIssue(
                "items.empty", Severity.ERROR, "Invoice has no line items", field="items",
            ))
            return

        items_sum = invoice.items_total
        if not items_sum.is_close(invoice.subtotal, self._tolerance):
            severity = (
                Severity.ERROR
                if abs(items_sum.amount - invoice.subtotal.amount) > Decimal("0.01") * len(invoice.items)
                else Severity.WARNING
            )
            report.add(ValidationIssue(
                "math.items_subtotal_mismatch", severity,
                f"Sum of line totals ({items_sum}) differs from subtotal ({invoice.subtotal})",
                field="subtotal",
            ))

        for index, item in enumerate(invoice.items):
            expected_net = item.line_net
            if not item.total.is_close(expected_net, _MAX_LINE_TOLERANCE):
                report.add(ValidationIssue(
                    "math.item_line_mismatch", Severity.ERROR,
                    f"Item #{index + 1}: quantity({item.quantity}) x unit_price"
                    f"({item.unit_price}) != line total ({item.total})",
                    field=f"items[{index}].total",
                ))
            elif item.total.amount != expected_net.amount:
                # Small gap (<= tolerance): classic rounding difference.
                report.add(ValidationIssue(
                    "math.item_line_rounding", Severity.WARNING,
                    f"Item #{index + 1}: line total ({item.total}) differs from "
                    f"quantity x unit_price ({expected_net}) by rounding",
                    field=f"items[{index}].total",
                ))


__all__ = ["InvoiceBusinessValidator"]
