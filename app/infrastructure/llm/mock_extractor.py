"""Deterministic offline invoice extractor.

Used when LLM_PROVIDER=mock: no external API, no cost, fully reproducible
output derived from a SHA-256 seed of the document text. Ideal for local
development, demos and end-to-end tests of the whole pipeline.
"""

import hashlib
import random
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from app.domain.services.invoice_extractor import InvoiceExtractor
from app.domain.value_objects.extracted_invoice import (
    ExtractedInvoiceData,
    ExtractedItem,
    ExtractedSupplier,
)

_TWO_PLACES = Decimal("0.01")

_ITEM_CATALOG: tuple[tuple[str, Decimal], ...] = (
    ("Consultoría técnica", Decimal("85.00")),
    ("Licencia software anual", Decimal("240.00")),
    ("Horas de soporte", Decimal("45.50")),
    ("Mantenimiento preventivo", Decimal("120.00")),
    ("Formación especializada", Decimal("300.00")),
    ("Desarrollo de integraciones", Decimal("95.75")),
)

_SUPPLIERS: tuple[tuple[str, str, str, str], ...] = (
    ("TechSupply Europe S.L.", "B12345678", "Calle Mayor 42, 28013 Madrid",
     "billing@techsupply.eu"),
    ("Global Office Solutions Ltd.", "GB987654321", "12 King Street, Leeds LS1 2HH",
     "accounts@globalsolutions.co.uk"),
)


class MockInvoiceExtractor(InvoiceExtractor):
    """Seeded pseudo-random extractor with mathematically consistent totals."""

    def extract(
        self, document_text: str, images=None
    ) -> ExtractedInvoiceData:
        seed = int(hashlib.sha256(document_text.encode("utf-8")).hexdigest()[:16], 16)
        rng = random.Random(seed)

        supplier = _SUPPLIERS[seed % len(_SUPPLIERS)]
        items = self._build_items(rng)
        subtotal = sum((item.total for item in items), start=Decimal("0"))
        tax = (subtotal * Decimal("0.21")).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
        total = subtotal + tax

        issue_date = date.today() - timedelta(days=rng.randint(1, 90))
        return ExtractedInvoiceData(
            number=f"INV-{issue_date.year}-{seed % 100000:05d}",
            issue_date=issue_date,
            due_date=issue_date + timedelta(days=30),
            currency="EUR" if supplier[1].startswith("B") else "GBP",
            subtotal=subtotal,
            tax=tax,
            total=total,
            supplier=ExtractedSupplier(
                name=supplier[0],
                tax_id=supplier[1],
                address=supplier[2],
                email=supplier[3],
                phone=f"+34 91{rng.randint(1000000, 9999999)}",
            ),
            items=items,
        )

    @staticmethod
    def _build_items(rng: random.Random) -> list[ExtractedItem]:
        count = rng.randint(1, min(4, len(_ITEM_CATALOG)))
        chosen = rng.sample(_ITEM_CATALOG, k=count)
        items: list[ExtractedItem] = []
        for description, unit_price in chosen:
            quantity = Decimal(rng.randint(1, 5))
            # Convention shared with the business validator:
            #   item.total = NET line amount (quantity x unit_price)
            #   invoice.subtotal = SUM(item.total); invoice.total = subtotal + tax
            line_net = (quantity * unit_price).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)
            items.append(
                ExtractedItem(
                    description=description,
                    quantity=quantity,
                    unit_price=unit_price,
                    tax=(line_net * Decimal("0.21")).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP),
                    total=line_net,
                )
            )
        return items


__all__ = ["MockInvoiceExtractor"]
