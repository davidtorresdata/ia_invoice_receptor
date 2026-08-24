"""Money value object: immutable, quantized, comparison-safe decimals."""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

TWO_PLACES = Decimal("0.01")


class MoneyError(ValueError):
    """Raised when a monetary amount is invalid."""


@dataclass(frozen=True, slots=True, order=True)
class Money:
    """
    A non-negative decimal amount with 2-place precision.

    The domain never manipulates bare floats: every monetary value flows
    through this VO, guaranteeing deterministic rounding and arithmetic.
    """

    amount: Decimal

    def __post_init__(self) -> None:
        if isinstance(self.amount, bool) or not isinstance(self.amount, Decimal):
            raise MoneyError(f"Money requires a Decimal, got {type(self.amount).__name__}")
        if not self.amount.is_finite():
            raise MoneyError("Money amount must be finite")
        if self.amount < Decimal("0"):
            raise MoneyError(f"Money cannot be negative, got {self.amount}")
        # Bypass frozen setter via object.__setattr__ (quantization on build).
        object.__setattr__(self, "amount", self.amount.quantize(TWO_PLACES, rounding=ROUND_HALF_UP))

    @classmethod
    def parse(cls, raw: Decimal | int | str | float | None, *, default: str = "0.00") -> "Money":
        """Parse untrusted external input (LLM JSON, forms) defensively."""
        if raw is None:
            raw = default
        text = str(raw)
        try:
            return cls(Decimal(cls._normalize(text)))
        except (InvalidOperation, ValueError, ArithmeticError) as exc:
            raise MoneyError(f"Invalid monetary amount: {raw!r}") from exc

    @staticmethod
    def _normalize(text: str) -> str:
        """
        Accept European/mixed formats: '12,5' -> '12.5', '1.234,56' ->
        '1234.56', '1,234.56' -> '1234.56'. Whitespace is ignored.
        """
        cleaned = text.strip().replace(" ", "").replace("\u00a0", "")
        if not cleaned or cleaned in {"-", "+", ".", ","}:
            raise ValueError(f"empty or sign-only amount: {text!r}")
        if "," in cleaned and "." in cleaned:
            decimal_sep = "," if cleaned.rfind(",") > cleaned.rfind(".") else "."
            thousands_sep = "." if decimal_sep == "," else ","
            cleaned = cleaned.replace(thousands_sep, "").replace(decimal_sep, ".")
        else:
            cleaned = cleaned.replace(",", ".")
        return cleaned

    @property
    def currency_free_amount(self) -> Decimal:
        return self.amount

    def add(self, other: "Money") -> "Money":
        return Money(self.amount + other.amount)

    def __add__(self, other: "Money") -> "Money":
        if not isinstance(other, Money):
            raise MoneyError(f"Cannot add {type(other).__name__} to Money")
        return Money(self.amount + other.amount)

    def __radd__(self, other: object) -> "Money":
        # Enables built-in sum(...) starting from 0.
        if other == 0:
            return self
        raise MoneyError(f"Cannot add Money to {type(other).__name__}")

    def multiply(self, factor: Decimal | int) -> "Money":
        return Money(self.amount * Decimal(factor))

    def is_close(self, other: "Money", tolerance: Decimal = TWO_PLACES) -> bool:
        """True when the absolute difference is within `tolerance`."""
        return abs(self.amount - other.amount) <= tolerance

    def __str__(self) -> str:
        return f"{self.amount:.2f}"
