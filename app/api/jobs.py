"""任务管理端点 —— 必须与链路端点共用同一个调度器实例。

早先这里自建 `JobScheduler()`：提交任务的是 job_runner 里的调度器，查询用的是
另一个空调度器，于是前端永远拿不到任务状态。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.api.deps import require_auth
from app.models.enums import JobStatus
from app.services.job_runner import get_job_runner
from app.services.scheduler import JobInfo, JobStateError
from app.utils.logging import get_logger

logger = get_logger("api.jobs")

router = APIRouter(tags=["jobs"], dependencies=[Depends(require_auth)])


class JobResponse(BaseModel):
    job_id: str
    project_id: str
    stage: str
    status: str
    progress: float
    message: str
    cancel_requested: bool
    result: dict[str, Any] | None = None


class JobListResponse(BaseModel):
    jobs: list[JobResponse]
    total: int


def _to_response(job: JobInfo) -> JobResponse:
    return JobResponse(
        job_id=str(job.job_id),
        project_id=job.project_id,
        stage=job.stage,
        status=job.status.value,
        progress=job.progress,
        message=job.message,
        cancel_requested=job.cancel_requested,
        result=job.result,
    )


def _parse_job_id(job_id: str) -> UUID:
    try:
        return UUID(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="job_id 必须为 UUID") from exc


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    status: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=50, ge=1, le=500),
) -> JobListResponse:
    job_status: JobStatus | None = None
    if status is not None:
        try:
            job_status = JobStatus(status)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"未知任务状态: {status}") from exc

    jobs = get_job_runner().scheduler.list_jobs(job_status)[:limit]
    return JobListResponse(
        jobs=[_to_response(j) for j in jobs],
        total=len(jobs),
    )


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(job_id: str) -> JobResponse:
    job = get_job_runner().scheduler.get_job(_parse_job_id(job_id))
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _to_response(job)


@router.post("/jobs/{job_id}/cancel", response_model=JobResponse)
async def cancel_job(job_id: str) -> JobResponse:
    scheduler = get_job_runner().scheduler
    try:
        job = scheduler.request_cancel(_parse_job_id(job_id))
    except JobStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _to_response(job)
