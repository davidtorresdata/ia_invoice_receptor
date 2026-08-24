"""Unit tests for the Pydantic extraction contract (LLM output validation)."""

import pytest
from pydantic import ValidationError

from app.domain.value_objects.extracted_invoice import ExtractedInvoiceData

VALID = {
    "number": "INV-99",
    "date": "2026-01-31",
    "due_date": None,
    "currency": "eur",
    "subtotal": "500.00",
    "tax": "105.00",
    "total": "605.00",
    "supplier": {"name": "Nordic AB", "tax_id": "SE556789", "email": "a@b.se"},
    "items": [{"description": "Widget", "quantity": 2, "unit_price": "250",
               "tax": "105.00", "total": 500}],
}


def payload(**overrides):
    data = {**VALID, **overrides}
    return data


class TestAccepts:
    def test_valid_payload(self):
        model = ExtractedInvoiceData.model_validate(payload())
        assert model.currency == "EUR"          # normalized uppercase
        assert str(model.issue_date) == "2026-01-31"
        assert model.items[0].quantity == 2

    def test_extra_keys_ignored(self):
        noisy = payload(unexpected_field="whatever")
        model = ExtractedInvoiceData.model_validate(noisy)
        assert model.number == "INV-99"

    def test_string_amounts_coerced(self):
        model = ExtractedInvoiceData.model_validate(payload(tax="1050,00",
                                                            subtotal="5000",
                                                            total="6050,00"))
        assert float(model.subtotal) == 5000.00
        assert float(model.tax) == 1050.00
        assert float(model.total) == 6050.00


class TestRejects:
    def test_missing_supplier(self):
        bad = payload(supplier=None)
        with pytest.raises(ValidationError):
            ExtractedInvoiceData.model_validate(bad)

    def test_bad_currency(self):
        with pytest.raises(ValidationError):
            ExtractedInvoiceData.model_validate(payload(currency="EURO"))

    def test_due_date_before_issue_date(self):
        with pytest.raises(ValidationError):
            ExtractedInvoiceData.model_validate(payload(due_date="2026-01-01"))

    def test_negative_quantity(self):
        items = [dict(VALID["items"][0], quantity=-1)]
        with pytest.raises(ValidationError):
            ExtractedInvoiceData.model_validate(payload(items=items))

    def test_empty_items(self):
        with pytest.raises(ValidationError):
            ExtractedInvoiceData.model_validate(payload(items=[]))

    def test_invalid_email(self):
        supplier = dict(VALID["supplier"], email="not-an-email")
        with pytest.raises(ValidationError):
            ExtractedInvoiceData.model_validate(payload(supplier=supplier))

    def test_missing_required_field(self):
        bad = {k: v for k, v in VALID.items() if k != "number"}
        with pytest.raises(ValidationError):
            ExtractedInvoiceData.model_validate(bad)
