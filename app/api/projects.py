"""项目管理端点 —— 落库，不再用进程内字典。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import require_auth
from app.models.media import MediaProbe
from app.services.project_service import (
    ProjectNotFoundError,
    ProjectServiceError,
    get_project_service,
)
from app.utils.log_redaction import redact_value
from app.utils.logging import get_logger
from app.utils.path_validator import PathValidationError, validate_media_path

logger = get_logger("api.projects")

router = APIRouter(tags=["projects"], dependencies=[Depends(require_auth)])


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=200, description="项目名称")
    source_path: str | None = Field(default=None, max_length=4096, description="源文件路径")


class AttachSourceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(..., min_length=1, max_length=4096, description="媒体文件路径")


class ProjectResponse(BaseModel):
    project_id: str
    name: str
    source_path: str | None = None
    duration_us: int = 0
    current_revision_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ProjectListResponse(BaseModel):
    projects: list[ProjectResponse]
    total: int


class RevisionResponse(BaseModel):
    revision_id: str
    revision_number: int
    parent_revision_id: str | None = None
    operation_type: str
    created_at: datetime | None = None


class RevisionListResponse(BaseModel):
    revisions: list[RevisionResponse]
    current_revision_id: str | None = None


def _to_response(payload: dict) -> ProjectResponse:
    return ProjectResponse(
        project_id=str(payload["project_id"]),
        name=payload["name"],
        source_path=payload.get("source_path"),
        duration_us=int(payload.get("duration_us") or 0),
        current_revision_id=payload.get("current_revision_id"),
        created_at=payload.get("created_at"),
        updated_at=payload.get("updated_at"),
    )


def _bad_path(exc: Exception) -> HTTPException:
    logger.warning("project_request_rejected", error=redact_value(str(exc)))
    return HTTPException(status_code=400, detail="路径不被允许")


@router.post("/projects", response_model=ProjectResponse)
async def create_project(request: CreateProjectRequest) -> ProjectResponse:
    source_path = None
    if request.source_path:
        try:
            source_path = str(validate_media_path(request.source_path))
        except PathValidationError as exc:
            raise _bad_path(exc) from exc

    try:
        payload = await get_project_service().create_project(request.name, source_path)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "project_create_failed",
            error_type=type(exc).__name__,
            error=redact_value(str(exc)),
        )
        raise HTTPException(status_code=500, detail="创建项目失败") from exc
    return _to_response(payload)


@router.get("/projects", response_model=ProjectListResponse)
async def list_projects(limit: int = Query(default=50, ge=1, le=200)) -> ProjectListResponse:
    items = await get_project_service().list_projects(limit)
    return ProjectListResponse(
        projects=[_to_response(item) for item in items],
        total=len(items),
    )


@router.get("/projects/{project_id}", response_model=ProjectResponse)
async def get_project(project_id: str) -> ProjectResponse:
    try:
        payload = await get_project_service().get_project(_uuid(project_id))
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="项目不存在") from exc
    return _to_response(payload)


@router.put("/projects/{project_id}/source", response_model=ProjectResponse)
async def attach_source(
    project_id: str, request: AttachSourceRequest
) -> ProjectResponse:
    """把媒体文件绑定到已有项目；路径必须真实存在且在允许的形式内。"""
    pid = _uuid(project_id)
    try:
        path = validate_media_path(request.file_path)
    except PathValidationError as exc:
        raise _bad_path(exc) from exc

    probe = await _probe_best_effort(str(path))
    service = get_project_service()
    try:
        await service.attach_source(
            pid,
            str(path),
            duration_us=probe.duration_us if probe else 0,
            rotation=probe.rotation if probe else 0,
            is_vfr=probe.is_vfr if probe else None,
            time_base=probe.time_base if probe else None,
        )
        payload = await service.get_project(pid)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="项目不存在") from exc
    except Exception as exc:
        logger.error(
            "project_source_attach_failed",
            error_type=type(exc).__name__,
            error=redact_value(str(exc)),
        )
        raise HTTPException(status_code=500, detail="绑定媒体文件失败") from exc
    return _to_response(payload)


async def _probe_best_effort(path: str) -> MediaProbe | None:
    """绑定时顺带记录探测结果；探测失败只降级为「未分析」，不阻塞绑定。"""
    from app.providers.ffmpeg.runner import FFmpegRunner

    try:
        return await FFmpegRunner().probe(path)
    except Exception as exc:
        logger.warning(
            "project_source_probe_failed",
            error_type=type(exc).__name__,
            error=redact_value(str(exc)),
        )
        return None


@router.get("/projects/{project_id}/revisions", response_model=RevisionListResponse)
async def list_revisions(
    project_id: str, limit: int = Query(default=50, ge=1, le=200)
) -> RevisionListResponse:
    service = get_project_service()
    pid = _uuid(project_id)
    try:
        detail = await service.get_project(pid)
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="项目不存在") from exc

    rows = await service.list_revisions(pid, limit)
    return RevisionListResponse(
        revisions=[RevisionResponse(**row) for row in rows],
        current_revision_id=detail.get("current_revision_id"),
    )


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str) -> dict[str, str]:
    try:
        await get_project_service().delete_project(_uuid(project_id))
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail="项目不存在") from exc
    except ProjectServiceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "deleted"}


def _uuid(project_id: str) -> str:
    try:
        return str(UUID(project_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="project_id 必须为 UUID") from exc
