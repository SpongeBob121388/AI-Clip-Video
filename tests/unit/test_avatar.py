"""数字人 Avatar Provider 与 Service 测试。

约定：HeyGen / D-ID 尚未接入真实 API，因此"不可用 + 显式抛错"才是正确行为。
返回 PENDING 假任务会被调用方当成提交成功，用户看到进度条永远不动。
"""

from __future__ import annotations

import pytest

from app.models.enums import SynthesisStatus
from app.providers.avatar.did_adapter import DIDAdapter
from app.providers.avatar.heygen_adapter import HeyGenAdapter
from app.providers.avatar.protocol import (
    AvatarCapabilities,
    AvatarProviderNotImplementedError,
    AvatarProviderProtocol,
    ProfileResult,
    SynthesisJob,
    VoiceConfig,
)
from app.services.avatar_service import AvatarService, AvatarServiceUnavailableError


class FakeCredentialStore:
    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self._secrets = secrets or {}

    def get_secret(self, key: str) -> str | None:
        return self._secrets.get(key)

    def set_secret(self, key: str, value: str) -> None:
        self._secrets[key] = value

    def delete_secret(self, key: str) -> None:
        self._secrets.pop(key, None)

    def list_keys(self) -> list[str]:
        return list(self._secrets.keys())


@pytest.fixture
def cred_store_with_key() -> FakeCredentialStore:
    return FakeCredentialStore({"avatar_api_key": "test-key-123"})


@pytest.fixture
def cred_store_empty() -> FakeCredentialStore:
    return FakeCredentialStore({})


class TestVoiceConfig:
    def test_defaults(self) -> None:
        vc = VoiceConfig()
        assert vc.speed == 1.0
        assert vc.pitch == 0.0

    def test_speed_bounds(self) -> None:
        VoiceConfig(speed=0.5)
        VoiceConfig(speed=2.0)
        with pytest.raises(ValueError):
            VoiceConfig(speed=0.49)
        with pytest.raises(ValueError):
            VoiceConfig(speed=2.01)

    def test_pitch_bounds(self) -> None:
        VoiceConfig(pitch=-12.0)
        VoiceConfig(pitch=12.0)
        with pytest.raises(ValueError):
            VoiceConfig(pitch=-12.01)
        with pytest.raises(ValueError):
            VoiceConfig(pitch=12.01)


class TestSynthesisJob:
    def test_defaults(self) -> None:
        job = SynthesisJob(job_id="job-1")
        assert job.status == SynthesisStatus.PENDING
        assert job.progress == 0.0
        assert job.output_video_path is None
        assert job.error_message is None

    def test_progress_bounds(self) -> None:
        SynthesisJob(job_id="j", progress=0.0)
        SynthesisJob(job_id="j", progress=1.0)
        with pytest.raises(ValueError):
            SynthesisJob(job_id="j", progress=-0.01)
        with pytest.raises(ValueError):
            SynthesisJob(job_id="j", progress=1.01)


class TestAvatarCapabilities:
    def test_defaults(self) -> None:
        caps = AvatarCapabilities(provider_id="heygen")
        assert caps.max_duration_seconds == 300
        assert caps.supports_custom_avatar is True
        assert caps.supported_languages == ["zh", "en"]


class TestProfileResult:
    def test_defaults(self) -> None:
        pr = ProfileResult(profile_id="p1")
        assert pr.status == "pending_approval"
        assert pr.test_video_path is None


@pytest.fixture(params=[HeyGenAdapter, DIDAdapter], ids=["heygen", "did"])
def adapter(request, cred_store_with_key: FakeCredentialStore):
    return request.param(cred_store_with_key)


class TestUnimplementedAdapters:
    """两个适配器共享同一份"未实现即不可用"契约。"""

    def test_is_available_is_false_even_with_key(self, adapter) -> None:
        assert adapter.is_available() is False

    @pytest.mark.asyncio
    async def test_synthesize_raises(self, adapter) -> None:
        with pytest.raises(AvatarProviderNotImplementedError):
            await adapter.synthesize("你好", "avatar-1")

    @pytest.mark.asyncio
    async def test_get_job_raises(self, adapter) -> None:
        with pytest.raises(AvatarProviderNotImplementedError):
            await adapter.get_job("external-job-123")

    @pytest.mark.asyncio
    async def test_cancel_raises(self, adapter) -> None:
        with pytest.raises(AvatarProviderNotImplementedError):
            await adapter.cancel_job("job-1")

    @pytest.mark.asyncio
    async def test_create_profile_raises(self, adapter) -> None:
        with pytest.raises(AvatarProviderNotImplementedError):
            await adapter.create_profile("ref.mp4", "heygen")

    def test_is_protocol_compliant(self, adapter) -> None:
        assert isinstance(adapter, AvatarProviderProtocol)


@pytest.mark.parametrize("adapter_cls", [HeyGenAdapter, DIDAdapter], ids=["heygen", "did"])
def test_empty_credential_store_is_still_unavailable(
    adapter_cls, cred_store_empty: FakeCredentialStore
) -> None:
    """有 key 也不可用（未实现），没 key 更不可用 —— 两个方向都不能回退成 True。"""
    assert adapter_cls(cred_store_empty).is_available() is False


class _StubProvider:
    """只有一个"真的能用"的提供者才允许被委派——用桩件验证委派路径。"""

    def __init__(self, *, available: bool = True) -> None:
        self._available = available
        self.calls: list[str] = []

    def is_available(self) -> bool:
        return self._available

    async def synthesize(self, text: str, avatar_id: str, voice_config=None) -> SynthesisJob:
        self.calls.append("synthesize")
        return SynthesisJob(job_id="job-real", status=SynthesisStatus.SYNTHESIZING)

    async def get_job(self, job_id: str) -> SynthesisJob:
        self.calls.append("get_job")
        return SynthesisJob(job_id=job_id, progress=0.5)

    async def cancel_job(self, job_id: str) -> bool:
        self.calls.append("cancel_job")
        return True

    def capabilities(self) -> AvatarCapabilities:
        return AvatarCapabilities(provider_id="stub")

    async def create_profile(self, reference_video_path: str, provider_id: str) -> ProfileResult:
        self.calls.append("create_profile")
        return ProfileResult(profile_id="profile-real")


class TestAvatarService:
    @pytest.mark.asyncio
    async def test_synthesize_delegates_to_available_provider(self) -> None:
        provider = _StubProvider()
        service = AvatarService(provider)
        job = await service.synthesize("测试文本", "avatar-1")
        assert job.job_id == "job-real"
        assert provider.calls == ["synthesize"]

    @pytest.mark.asyncio
    async def test_synthesize_refuses_unavailable_provider(self) -> None:
        service = AvatarService(_StubProvider(available=False))
        with pytest.raises(AvatarServiceUnavailableError):
            await service.synthesize("测试", "avatar-1")

    @pytest.mark.asyncio
    async def test_synthesize_does_not_swallow_provider_errors(self) -> None:
        class Exploding(_StubProvider):
            async def synthesize(self, text: str, avatar_id: str, voice_config=None):
                raise RuntimeError("上游 500")

        service = AvatarService(Exploding())
        with pytest.raises(RuntimeError, match="上游 500"):
            await service.synthesize("test", "avatar-1")

    @pytest.mark.asyncio
    async def test_get_job_status_and_cancel_delegate(self) -> None:
        provider = _StubProvider()
        service = AvatarService(provider)
        job = await service.get_job_status("job-99")
        cancelled = await service.cancel("job-99")
        assert (job.job_id, cancelled) == ("job-99", True)
        assert provider.calls == ["get_job", "cancel_job"]

    @pytest.mark.asyncio
    async def test_create_profile_delegates(self) -> None:
        provider = _StubProvider()
        service = AvatarService(provider)
        profile = await service.create_profile("/ref.mp4", "stub")
        assert profile.profile_id == "profile-real"

    @pytest.mark.asyncio
    async def test_real_adapters_are_unavailable(self, cred_store_with_key) -> None:
        for adapter in (HeyGenAdapter(cred_store_with_key), DIDAdapter(cred_store_with_key)):
            with pytest.raises(AvatarServiceUnavailableError):
                await AvatarService(adapter).synthesize("文本", "avatar-1")
