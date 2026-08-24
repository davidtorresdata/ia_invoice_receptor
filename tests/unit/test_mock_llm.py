"""Unit tests for the deterministic mock LLM extractor."""

from decimal import Decimal

from app.infrastructure.llm.mock_extractor import MockInvoiceExtractor

TEXT = "INVOICE #12345\nACME CORP\n1 Widget 10.00\n"


class TestDeterminism:
    def test_same_input_same_output(self):
        first = MockInvoiceExtractor().extract(TEXT)
        second = MockInvoiceExtractor().extract(TEXT)
        assert first.number == second.number
        assert first.total == second.total
        assert [i.description for i in first.items] == [
            i.description for i in second.items
        ]

    def test_totals_are_mathematically_consistent(self):
        model = MockInvoiceExtractor().extract(TEXT)
        items_sum = sum((item.total for item in model.items), start=Decimal("0"))
        assert items_sum == model.subtotal                      # net convention
        assert model.subtotal + model.tax == model.total        # grand total
        for item in model.items:
            expected_net = (item.quantity * item.unit_price).quantize(Decimal("0.01"))
            assert item.total == expected_net                   # line math


class TestContractCompliance:
    def test_output_validates_against_schema(self):
        model = MockInvoiceExtractor().extract(TEXT)
        assert len(model.items) >= 1
        assert model.currency in {"EUR", "GBP"}
        assert model.due_date is not None and model.due_date > model.issue_date
