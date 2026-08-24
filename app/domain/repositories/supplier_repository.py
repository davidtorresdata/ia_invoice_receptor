"""Supplier repository port (driven interface, implemented in infrastructure)."""

import uuid
from abc import ABC, abstractmethod

from app.domain.entities.invoice import Supplier


class SupplierRepository(ABC):
    @abstractmethod
    def add(self, supplier: Supplier) -> Supplier:
        """Register a new supplier; returns the same aggregate."""

    @abstractmethod
    def get(self, supplier_id: uuid.UUID) -> Supplier | None:
        """Fetch by identifier, or None when absent."""

    @abstractmethod
    def find_by_tax_id(self, tax_id: str) -> Supplier | None:
        """Lookup used for deduplication across invoices."""
