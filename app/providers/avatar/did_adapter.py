"""D-ID Avatar Adapter —— 未实现分支。

与 HeyGen 适配器同样拒绝伪装成功：未接入真实 API 时一律显式失败。
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

logger = get_logger("providers.avatar.did")

_MESSAGE = "D-ID 数字人合成尚未接入真实 API"


class DIDAdapter:
    def __init__(self, credential_store: CredentialStoreProtocol) -> None:
        self._credential_store = credential_store

    def is_available(self) -> bool:
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
        return AvatarCapabilities(provider_id="did")

    async def create_profile(
        self, reference_video_path: str, provider_id: str
    ) -> ProfileResult:
        raise self._unavailable()
