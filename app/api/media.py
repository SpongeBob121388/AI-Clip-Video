"""媒体探测、上传与本地播放流端点。"""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import check_host, require_auth
from app.config import get_settings
from app.models.media import MediaProbe
from app.services.project_service import ProjectServiceError, get_project_service
from app.utils.log_redaction import redact_value
from app.utils.logging import get_logger
from app.utils.path_validator import (
    PathValidationError,
    is_within,
    sanitize_filename,
    validate_media_path,
)

logger = get_logger("api.media")

router = APIRouter(tags=["media"], dependencies=[Depends(require_auth)])

#: 取流端点单独成路由：<video> 标签无法携带自定义头，凭票据本身授权，
#: 因此它不要求 Authorization，但 Host 校验与票据校验仍在函数内部执行。
stream_router = APIRouter(tags=["media"])

_CHUNK = 1024 * 1024
#: 播放票据只用于本机 <video> 取流：浏览器无法给媒体请求加自定义头，
#: 而把 API 令牌放进 URL 会被访问日志记录，所以换成短期、单次路径绑定的随机票据。
_TICKET_TTL_SECONDS = 900.0
_TICKET_LIMIT = 64
_TICKETS: dict[str, tuple[Path, float]] = {}

_MEDIA_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".webm": "video/webm",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
}


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(..., min_length=1, max_length=4096, description="媒体文件路径")


class UploadProbeResponse(BaseModel):
    file_path: str = Field(..., description="服务器端保存的文件路径")
    probe: MediaProbe


def _bad_request(exc: Exception) -> HTTPException:
    logger.warning("media_request_rejected", error=redact_value(str(exc)))
    return HTTPException(status_code=400, detail="路径或文件不被允许")


def _internal_error(op: str, exc: Exception) -> HTTPException:
    # 不把上游异常原样回显给客户端：其中常含绝对路径与用户名
    logger.error(
        f"{op}_failed",
        error_type=type(exc).__name__,
        error=redact_value(str(exc)),
        exc_info=True,
    )
    return HTTPException(status_code=500, detail=f"{op}失败")


@router.post("/media/probe", response_model=MediaProbe)
async def probe_media(request: ProbeRequest) -> MediaProbe:
    from app.providers.ffmpeg.runner import FFmpegRunner

    try:
        file_path = validate_media_path(request.file_path)
    except PathValidationError as exc:
        raise _bad_request(exc) from exc

    logger.info("probe_requested", file_name=file_path.name)

    try:
        runner = FFmpegRunner()
        result = await runner.probe(str(file_path))
        logger.info("probe_success", duration_us=result.duration_us)
        return result
    except Exception as exc:
        raise _internal_error("探测", exc) from exc


@router.post("/media/upload", response_model=UploadProbeResponse)
async def upload_media(
    file: Annotated[UploadFile, File(description="媒体文件")],
) -> UploadProbeResponse:
    """上传媒体文件并自动探测。用于浏览器环境（无法获取本地文件路径）。"""
    from app.providers.ffmpeg.runner import FFmpegRunner

    settings = get_settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)

    try:
        safe_name = sanitize_filename(file.filename)
    except PathValidationError as exc:
        raise _bad_request(exc) from exc

    tmp_path = settings.upload_dir / f"{uuid4().hex[:8]}_{safe_name}"
    max_bytes = settings.max_upload_bytes

    oversized = False
    try:
        written = 0
        with open(tmp_path, "wb") as f:
            while True:
                chunk = await file.read(_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    oversized = True
                    break
                f.write(chunk)
    except OSError as exc:
        _silent_unlink(tmp_path)
        raise _internal_error("上传", exc) from exc
    finally:
        await file.close()

    if oversized:
        _silent_unlink(tmp_path)
        logger.warning("upload_rejected", reason="too_large", limit_bytes=max_bytes)
        raise HTTPException(
            status_code=413,
            detail=f"文件超过上限 {max_bytes // (1024 ** 2)} MB",
        )

    logger.info("file_uploaded", file_name=safe_name, size=written)

    try:
        runner = FFmpegRunner()
        probe = await runner.probe(str(tmp_path))
    except Exception as exc:
        _silent_unlink(tmp_path)
        raise _internal_error("探测", exc) from exc

    return UploadProbeResponse(file_path=str(tmp_path), probe=probe)


def _silent_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - 清理失败不应改变响应
        logger.warning("upload_cleanup_failed", file_name=path.name)


class TicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file_path: str = Field(..., min_length=1, max_length=4096, description="服务器端产物路径")


class ProjectTicketRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(..., min_length=36, max_length=36, description="项目 ID")


class TicketResponse(BaseModel):
    ticket: str
    expires_in: int
    url: str


def _allowed_roots() -> list[Path]:
    settings = get_settings()
    return [
        settings.upload_dir,
        settings.render_work_dir,
        settings.preview_cache_dir,
        settings.export_dir,
        settings.projects_dir,
        settings.data_dir / "audio_tmp",
    ]


def _issue(path: Path) -> TicketResponse:
    if not path.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    _prune_tickets()
    existing = next((key for key, (p, _e) in _TICKETS.items() if p == path), None)
    if existing is not None:
        # 同一个文件重复取证时复用票据，避免长时间会话把票据表填满
        _TICKETS[existing] = (path, time.monotonic() + _TICKET_TTL_SECONDS)
        return TicketResponse(
            ticket=existing,
            expires_in=int(_TICKET_TTL_SECONDS),
            url=f"/api/v1/media/stream?ticket={existing}",
        )
    while len(_TICKETS) >= _TICKET_LIMIT:
        # 满了就淘汰最旧的（dict 保持插入顺序），而不是回 429 让播放器失灵
        _TICKETS.pop(next(iter(_TICKETS)), None)
    ticket = secrets.token_urlsafe(16)
    _TICKETS[ticket] = (path, time.monotonic() + _TICKET_TTL_SECONDS)
    return TicketResponse(
        ticket=ticket,
        expires_in=int(_TICKET_TTL_SECONDS),
        url=f"/api/v1/media/stream?ticket={ticket}",
    )


def _prune_tickets() -> None:
    now = time.monotonic()
    for key in [k for k, (_p, exp) in _TICKETS.items() if exp <= now]:
        _TICKETS.pop(key, None)


@router.post("/media/stream-ticket", response_model=TicketResponse)
async def create_stream_ticket(request: TicketRequest) -> TicketResponse:
    """为服务器产物（预览/导出/上传）签发播放票据；票据只绑定一个已存在的路径。"""
    try:
        path = validate_media_path(request.file_path)
    except PathValidationError as exc:
        raise _bad_request(exc) from exc

    if not any(is_within(path, root) for root in _allowed_roots()):
        logger.warning("stream_ticket_rejected", file_name=path.name)
        raise HTTPException(status_code=403, detail="该路径不允许流式播放")
    return _issue(path)


@router.post("/media/source-ticket", response_model=TicketResponse)
async def create_source_ticket(request: ProjectTicketRequest) -> TicketResponse:
    """为项目源文件签发票据：路径由服务端记录提供，不接受客户端传入的任意路径。"""
    try:
        source = await get_project_service().source_path(request.project_id)
    except (ProjectServiceError, ValueError) as exc:
        raise HTTPException(status_code=404, detail="项目或源文件不存在") from exc
    return _issue(validate_media_path(source))


@stream_router.get("/media/stream", response_class=StreamingResponse)
async def stream_media(
    ticket: str = Query(..., min_length=8, max_length=64),
    range_header: Annotated[str | None, Header(alias="Range")] = None,
    host: str | None = Header(default=None),
) -> Response:
    """按票据取流，支持 Range（视频拖动进度条）。

    这里刻意不要求 Authorization：票据本身已是短期、单次路径绑定的能力凭证。
    """
    check_host(host)
    entry = _TICKETS.get(ticket)
    if entry is None:
        raise HTTPException(status_code=401, detail="播放票据无效或已过期")
    path, expires_at = entry
    if expires_at <= time.monotonic():
        _TICKETS.pop(ticket, None)
        raise HTTPException(status_code=401, detail="播放票据已过期")

    size = path.stat().st_size
    start, end = 0, size - 1
    if range_header:
        unit, _, raw = range_header.partition("=")
        if unit.strip().lower() != "bytes" or not raw:
            raise HTTPException(status_code=416, detail="不支持的 Range 单位")
        first, _, last = raw.partition("-")
        try:
            start = int(first) if first else 0
            end = int(last) if last else size - 1
        except ValueError as exc:
            raise HTTPException(status_code=416, detail="非法 Range") from exc
        if start >= size or end >= size or end < start:
            raise HTTPException(
                status_code=416,
                headers={"Content-Range": f"bytes */{size}"},
                detail="Range 超出文件长度",
            )

    length = end - start + 1

    def _reader():
        with open(path, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                block = handle.read(min(_CHUNK, remaining))
                if not block:
                    break
                remaining -= len(block)
                yield block

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Content-Range": f"bytes {start}-{end}/{size}",
    }
    status_code = 206 if range_header else 200
    if not range_header:
        headers.pop("Content-Range")
    media_type = _MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return StreamingResponse(
        _reader(), media_type=media_type, status_code=status_code, headers=headers
    )
