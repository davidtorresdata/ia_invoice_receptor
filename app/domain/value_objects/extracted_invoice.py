"""Pydantic contract for LLM extraction output.

This is the *only* shape accepted from an LLM. Adapters must parse their
raw response into `ExtractedInvoiceData` (or raise `LLMExtractionError`);
nothing unvalidated ever crosses into the application/domain layers.

Level-1 validation lives here: syntax, types, required fields, basic date
and numeric sanity. Level-2 (entity invariants) and level-3 (business math)
run later on domain objects.
"""

import re
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_TAX_ID_MIN = 6


class ExtractedSupplier(BaseModel):
    """Supplier block of the extraction schema."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    name: str = Field(min_length=2, max_length=255)
    tax_id: str = Field(min_length=_TAX_ID_MIN, max_length=64)
    address: str | None = Field(default=None, max_length=500)
    phone: str | None = Field(default=None, max_length=50)
    email: EmailStr | None = None


class ExtractedItem(BaseModel):
    """Line item of the extraction schema."""

    model_config = ConfigDict(extra="ignore")

    description: str = Field(min_length=1, max_length=1000)
    quantity: Decimal = Field(ge=0)
    unit_price: Decimal = Field(ge=0)
    tax: Decimal = Field(default=Decimal("0"), ge=0)
    total: Decimal = Field(gt=0)

    @field_validator("quantity", "unit_price", "tax", "total", mode="before")
    @classmethod
    def _coerce_amount(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().replace(" ", "").replace(",", ".") or "0"
        return value


class ExtractedInvoiceData(BaseModel):
    """Root of the structured extraction contract."""

    model_config = ConfigDict(
        extra="ignore",
        str_strip_whitespace=True,
        populate_by_name=True,  # accept "date" alias and "issue_date" alike
    )

    number: str = Field(min_length=1, max_length=100)
    issue_date: date = Field(alias="date")
    due_date: date | None = None
    currency: str
    subtotal: Decimal = Field(ge=0)
    tax: Decimal = Field(default=Decimal("0"), ge=0)
    total: Decimal = Field(gt=0)
    supplier: ExtractedSupplier
    items: list[ExtractedItem] = Field(min_length=1)

    @field_validator("currency")
    @classmethod
    def _currency_iso(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not _CURRENCY_RE.match(normalized):
            raise ValueError(f"currency '{value}' is not a 3-letter ISO code")
        return normalized

    @field_validator("subtotal", "tax", "total", mode="before")
    @classmethod
    def _coerce_amounts(cls, value: object) -> object:
        if isinstance(value, str):
            cleaned = value.strip().replace(" ", "").replace(",", ".")
            return cleaned or "0"
        return value

    @model_validator(mode="after")
    def _check_dates(self) -> "ExtractedInvoiceData":
        if self.due_date is not None and self.due_date < self.issue_date:
            raise ValueError(f"due_date ({self.due_date}) precedes issue_date ({self.issue_date})")
        return self

    @property
    def items_total(self) -> Decimal:
        return sum((item.total for item in self.items), start=Decimal("0"))
