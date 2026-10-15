"""数字人端点 —— 未实现分支必须显式不可用，不能返回假任务。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import require_auth
from app.providers.avatar.protocol import AvatarProviderNotImplementedError
from app.providers.registry import get_providers
from app.services.avatar_service import AvatarService, AvatarServiceUnavailableError
from app.utils.logging import get_logger

logger = get_logger("api.avatar")

router = APIRouter(prefix="/avatar", tags=["avatar"], dependencies=[Depends(require_auth)])

_UNAVAILABLE_MESSAGE = "数字人合成尚未接入真实 API（HeyGen / D-ID），该功能当前不可用"


class SynthesizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=20000)
    avatar_id: str = Field(..., min_length=1, max_length=200)
    provider: str = Field(default="heygen", pattern="^(heygen|did)$")


class ProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference_video_path: str = Field(..., min_length=1, max_length=4096)
    provider: str = Field(default="heygen", pattern="^(heygen|did)$")


class AvatarCapabilities(BaseModel):
    available: bool
    message: str
    providers: list[dict[str, object]]


@router.get("/capabilities", response_model=AvatarCapabilities)
async def capabilities() -> AvatarCapabilities:
    providers = get_providers()
    rows = []
    for name in ("heygen", "did"):
        adapter = providers.avatar(name)
        rows.append({"id": name, "available": bool(adapter.is_available())})
    available = any(row["available"] is True for row in rows)
    return AvatarCapabilities(
        available=available,
        message="" if available else _UNAVAILABLE_MESSAGE,
        providers=rows,
    )


@router.post("/synthesize")
async def synthesize(request: SynthesizeRequest) -> dict[str, str]:
    service = AvatarService(get_providers().avatar(request.provider))
    try:
        job = await service.synthesize(request.text, request.avatar_id)
    except (AvatarServiceUnavailableError, AvatarProviderNotImplementedError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"job_id": job.job_id, "status": job.status.value}


@router.post("/profiles")
async def create_profile(request: ProfileRequest) -> dict[str, str]:
    service = AvatarService(get_providers().avatar(request.provider))
    try:
        profile = await service.create_profile(request.reference_video_path, request.provider)
    except (AvatarServiceUnavailableError, AvatarProviderNotImplementedError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"profile_id": profile.profile_id, "status": profile.status}
