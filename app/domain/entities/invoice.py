"""Invoice aggregate: Invoice, Supplier and InvoiceItem entities."""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from app.domain.exceptions import EntityValidationError
from app.domain.value_objects.money import Money
from app.domain.value_objects.validation import ValidationReport


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(slots=True)
class Supplier:
    """The party issuing the invoice; deduplicated by tax_id."""

    name: str
    tax_id: str
    address: str | None = None
    phone: str | None = None
    email: str | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise EntityValidationError("Supplier name is required")
        if not self.tax_id or not self.tax_id.strip():
            raise EntityValidationError("Supplier tax_id is required")

    @property
    def identity(self) -> str:
        return f"{self.name} ({self.tax_id})"


@dataclass(slots=True)
class InvoiceItem:
    """One invoice line."""

    description: str
    quantity: Decimal
    unit_price: Decimal
    tax_amount: Decimal
    total: Money
    id: UUID = field(default_factory=uuid4)

    @property
    def line_net(self) -> Money:
        """quantity * unit_price (informational; validator checks consistency)."""
        return Money.parse(self.quantity * self.unit_price)

    def to_dict(self) -> dict:
        return {
            "description": self.description,
            "quantity": str(self.quantity),
            "unit_price": str(self.unit_price),
            "tax": str(self.tax_amount),
            "total": str(self.total.amount),
        }


@dataclass(slots=True)
class Invoice:
    """
    Aggregate root holding extracted invoice data + validation outcome.

    Invariants enforced here (level 2): positive totals, currency present,
    at least one item. Deeper business rules live in the domain service
    `InvoiceBusinessValidator` (level 3).
    """

    document_id: UUID
    supplier_id: UUID
    number: str
    issue_date: date
    currency: str
    subtotal: Money
    tax_amount: Money
    total: Money
    due_date: date | None = None
    validation_report: dict | None = None
    raw_extraction: dict | None = None
    items: list[InvoiceItem] = field(default_factory=list)
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.number or not self.number.strip():
            raise EntityValidationError("Invoice number is required")
        if len(self.currency) != 3 or not self.currency.isupper():
            raise EntityValidationError(f"Currency must be a 3-letter ISO code, got '{self.currency}'")
        if not self.items:
            raise EntityValidationError("Invoice requires at least one line item")
        if self.total.amount <= Decimal("0"):
            raise EntityValidationError("Invoice total must be greater than zero")

    def add_item(self, item: InvoiceItem) -> None:
        self.items.append(item)

    @property
    def items_total(self) -> Money:
        return sum((item.total for item in self.items), start=Money(Decimal("0")))

    def apply_validation(self, report: ValidationReport) -> None:
        """Attach a business-validation outcome to this invoice."""
        self.validation_report = report.to_dict()
        self.updated_at = _utcnow()
