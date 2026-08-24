"""Unit tests for the business-rules validator (validation level 3)."""

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from app.domain.entities.invoice import Invoice, InvoiceItem
from app.domain.exceptions import EntityValidationError
from app.domain.services.invoice_validator import InvoiceBusinessValidator
from app.domain.value_objects.money import Money
from app.domain.value_objects.validation import Severity


def make_item(description="Line", qty=Decimal("1"), price=Decimal("100"),
              tax=Decimal("21"), total=Decimal("100")) -> InvoiceItem:
    return InvoiceItem(
        description=description, quantity=qty, unit_price=price,
        tax_amount=tax, total=Money(total),
    )


def make_invoice(items=None, subtotal="200.00", tax="42.00", total="242.00",
                 issue=date(2026, 1, 10), due=None) -> Invoice:
    items = items if items is not None else [make_item(), make_item()]
    return Invoice(
        document_id=uuid4(),
        supplier_id=uuid4(),
        number="INV-1",
        issue_date=issue,
        due_date=due,
        currency="EUR",
        subtotal=Money.parse(subtotal),
        tax_amount=Money.parse(tax),
        total=Money.parse(total),
        items=items,
    )


@pytest.fixture
def validator() -> InvoiceBusinessValidator:
    return InvoiceBusinessValidator()


class TestValidInvoice:
    def test_consistent_invoice_is_valid(self, validator):
        report = validator.validate(make_invoice(due=date(2026, 2, 9)))
        assert report.is_valid
        codes = {i.code for i in report.issues}
        assert "math.total_mismatch" not in codes

    def test_apply_validation_attaches_report(self, validator):
        invoice = make_invoice()
        invoice.apply_validation(validator.validate(invoice))
        assert invoice.validation_report["is_valid"] is True


class TestMathRules:
    def test_total_mismatch_is_error(self, validator):
        invoice = make_invoice(total="999.00")
        report = validator.validate(invoice)
        assert not report.is_valid
        assert any(i.code == "math.total_mismatch" and i.severity == Severity.ERROR
                   for i in report.issues)

    def test_items_sum_mismatch_flags_subtotal(self, validator):
        # Items sum to 200 but declared subtotal is 500.
        invoice = make_invoice(subtotal="500.00", tax="105.00", total="605.00")
        report = validator.validate(invoice)
        assert any(i.code == "math.items_subtotal_mismatch" for i in report.issues)

    def test_small_rounding_gap_is_warning_not_error(self, validator):
        # One item declared 0.01 above its net: tolerated as WARNING.
        items = [make_item(qty=Decimal("2"), price=Decimal("50.00"),
                           tax=Decimal("21.00"), total=Decimal("100.01"))]
        invoice = make_invoice(items=items, subtotal="100.00", tax="21.00",
                               total="121.00")
        report = validator.validate(invoice)
        warnings = list(report.warnings)
        assert warnings and all(i.severity != Severity.ERROR for i in warnings)

    def test_line_net_mismatch_warns(self, validator):
        items = [make_item(qty=Decimal("3"), price=Decimal("50.00"), total=Decimal("100.00"))]
        invoice = make_invoice(items=items, subtotal="100.00", tax="21.00",
                               total="121.00")
        report = validator.validate(invoice)
        assert any(i.code == "math.item_line_mismatch" for i in report.issues)


class TestFieldAndDateRules:
    def test_due_before_issue_is_error(self, validator):
        invoice = make_invoice(issue=date(2026, 3, 1), due=date(2026, 2, 1))
        report = validator.validate(invoice)
        assert not report.is_valid

    def test_zero_total_rejected_by_entity(self):
        with pytest.raises(EntityValidationError):
            make_invoice(total="0.00")

    def test_far_future_issue_date_warns(self, validator):
        invoice = make_invoice(issue=date.today().replace(year=date.today().year + 2))
        report = validator.validate(invoice)
        assert any(i.code == "date.issue_far_future"
                   and i.severity == Severity.WARNING for i in report.issues)

    def test_report_serializes_for_persistence(self, validator):
        invoice = make_invoice()
        invoice.apply_validation(validator.validate(invoice))
        assert isinstance(invoice.validation_report, dict)
        assert "is_valid" in invoice.validation_report
        assert all("code" in issue for issue in invoice.validation_report["issues"])
