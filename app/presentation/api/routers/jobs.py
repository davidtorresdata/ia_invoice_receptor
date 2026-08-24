"""Processing-job endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends

from app.presentation.api.deps import get_job_status_use_case
from app.presentation.api.mappers import job_to_response
from app.presentation.api.schemas import JobResponse

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=JobResponse, summary="Job processing status")
def get_job(
    job_id: UUID,
    use_case: object = Depends(get_job_status_use_case),
) -> JobResponse:
    job = use_case.execute(job_id)  # type: ignore[attr-defined]
    return job_to_response(job)
