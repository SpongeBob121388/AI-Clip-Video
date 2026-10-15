"""AvatarProvider Protocol — 数字人合成接口定义。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.models.enums import SynthesisStatus


class AvatarProviderNotImplementedError(RuntimeError):
    """数字人合成未接入真实 API。

    宁可显式失败，也不要返回一个永远 PENDING 的假任务：调用方会把它当成
    "任务已提交"，用户则看到进度条永远不动。
    """

    pass


class VoiceConfig(BaseModel):
    speed: float = Field(default=1.0, ge=0.5, le=2.0)
    pitch: float = Field(default=0.0, ge=-12.0, le=12.0)


class SynthesisJob(BaseModel):
    job_id: str = Field(...)
    status: SynthesisStatus = Field(default=SynthesisStatus.PENDING)
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    output_video_path: str | None = Field(default=None)
    error_message: str | None = Field(default=None)


class AvatarCapabilities(BaseModel):
    provider_id: str = Field(...)
    max_duration_seconds: int = Field(default=300)
    supports_custom_avatar: bool = Field(default=True)
    supported_languages: list[str] = Field(default_factory=lambda: ["zh", "en"])


class ProfileResult(BaseModel):
    profile_id: str = Field(...)
    status: str = Field(default="pending_approval")
    test_video_path: str | None = Field(default=None)


@runtime_checkable
class AvatarProviderProtocol(Protocol):
    async def synthesize(
        self,
        text: str,
        avatar_id: str,
        voice_config: VoiceConfig | None = None,
    ) -> SynthesisJob: ...

    async def get_job(self, job_id: str) -> SynthesisJob: ...

    async def cancel_job(self, job_id: str) -> bool: ...

    def capabilities(self) -> AvatarCapabilities: ...

    async def create_profile(
        self, reference_video_path: str, provider_id: str
    ) -> ProfileResult: ...
