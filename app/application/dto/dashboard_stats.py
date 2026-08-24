"""Application-layer view models returned by use cases."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class DashboardStats:
    """Aggregated counters powering the Streamlit dashboard."""

    pending_jobs: int
    processing_jobs: int
    completed_jobs: int
    failed_jobs: int
    total_invoices: int
    total_invoiced: Decimal
