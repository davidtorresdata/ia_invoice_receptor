"""Unit tests for the Money value object."""

from decimal import Decimal

import pytest

from app.domain.value_objects.money import Money, MoneyError


class TestParse:
    def test_parses_string(self):
        assert Money.parse("1234.56").amount == Decimal("1234.56")

    def test_handles_comma_decimal(self):
        assert Money.parse("12,5").amount == Decimal("12.50")

    def test_none_defaults_to_zero(self):
        assert Money.parse(None).amount == Decimal("0.00")

    def test_quantizes_half_up(self):
        assert Money.parse(Decimal("10.005")).amount == Decimal("10.01")

    @pytest.mark.parametrize("raw", ["abc", "1.2.3", ""])
    def test_invalid_raises(self, raw):
        with pytest.raises(MoneyError):
            Money.parse(raw)


class TestInvariants:
    def test_negative_rejected(self):
        with pytest.raises(MoneyError):
            Money(Decimal("-0.01"))

    def test_nan_rejected(self):
        with pytest.raises(MoneyError):
            Money(Decimal("NaN"))

    def test_requires_decimal(self):
        with pytest.raises(MoneyError):
            Money(1.23)  # type: ignore[arg-type]


class TestArithmetic:
    def test_add(self):
        assert Money.parse("10.10").add(Money.parse("1.90")).amount == Decimal("12.00")

    def test_builtin_sum(self):
        total = sum([Money.parse("1.50"), Money.parse("2.25")], start=Money.parse("0"))
        assert total.amount == Decimal("3.75")

    def test_is_close_within_tolerance(self):
        assert Money.parse("100.00").is_close(Money.parse("100.01"))

    def test_is_close_outside_tolerance(self):
        assert not Money.parse("100.00").is_close(Money.parse("100.03"))
