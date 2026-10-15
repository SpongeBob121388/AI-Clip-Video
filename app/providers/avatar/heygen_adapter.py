"""HeyGen Avatar Adapter —— 未实现分支。

历史实现会返回 job_id="heygen-pending" 且状态恒为 PENDING 的假任务，
调用方（前端"导入至剪辑"）据此显示成功，实际什么都没发生。
现在统一显式失败，等待真实 API 接入。
"""

from __future__ import annotations

from app.providers.avatar.protocol import (
    AvatarCapabilities,
    AvatarProviderNotImplementedError,
    ProfileResult,
    SynthesisJob,
    VoiceConfig,
)
from app.providers.credential.protocol import CredentialStoreProtocol
from app.utils.logging import get_logger

logger = get_logger("providers.avatar.heygen")

_MESSAGE = "HeyGen 数字人合成尚未接入真实 API"


class HeyGenAdapter:
    def __init__(self, credential_store: CredentialStoreProtocol) -> None:
        self._credential_store = credential_store

    def is_available(self) -> bool:
        """未实现 => 不可用。凭据存在与否不改变这个结论。"""
        return False

    def _unavailable(self) -> AvatarProviderNotImplementedError:
        return AvatarProviderNotImplementedError(_MESSAGE)

    async def synthesize(
        self,
        text: str,
        avatar_id: str,
        voice_config: VoiceConfig | None = None,
    ) -> SynthesisJob:
        raise self._unavailable()

    async def get_job(self, job_id: str) -> SynthesisJob:
        raise self._unavailable()

    async def cancel_job(self, job_id: str) -> bool:
        raise self._unavailable()

    def capabilities(self) -> AvatarCapabilities:
        return AvatarCapabilities(provider_id="heygen")

    async def create_profile(
        self, reference_video_path: str, provider_id: str
    ) -> ProfileResult:
        raise self._unavailable()
