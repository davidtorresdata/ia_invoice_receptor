"""Dashboard aggregates endpoint."""

from fastapi import APIRouter, Depends

from app.presentation.api.deps import get_dashboard_stats_use_case
from app.presentation.api.schemas import DashboardStatsResponse

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/stats", response_model=DashboardStatsResponse, summary="Dashboard counters")
def get_stats(use_case: object = Depends(get_dashboard_stats_use_case)) -> DashboardStatsResponse:
    stats = use_case.execute()  # type: ignore[attr-defined]
    return DashboardStatsResponse(
        jobs={
            "pending": stats.pending_jobs,
            "processing": stats.processing_jobs,
            "completed": stats.completed_jobs,
            "failed": stats.failed_jobs,
        },
        invoices={"total": stats.total_invoices},
        total_invoiced=float(stats.total_invoiced),
    )
