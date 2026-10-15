"""设置端点：读取（脱敏）、保存、LLM 连通性测试。

安全要点：
- base_url 必经 SSRF 校验（协议/私网/链路本地/userinfo），否则该端点等于内网代理
- API Key 只走请求头，绝不进 URL；密钥值永不出现在 GET 响应里
- 不回显上游响应体（上游错误页常回带请求元数据）
- 保存是原子的：先全量校验，再写机密，失败则回滚已写项
"""

from __future__ import annotations

import time
from typing import Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import require_auth
from app.config import get_settings
from app.persistence.preferences import PreferenceError, get_preferences, set_preferences
from app.providers.credential.win_credential_store import (
    CredentialStoreError,
    WinCredentialStore,
    get_credential_store,
)
from app.utils.logging import get_logger
from app.utils.net_guard import UnsafeURLError, validate_endpoint_url_async

logger = get_logger("api.settings")

router = APIRouter(prefix="/settings", tags=["settings"], dependencies=[Depends(require_auth)])

_TEST_TIMEOUT_SECONDS = 15.0
_MAX_RESPONSE_BYTES = 1024 * 1024
# 只接受真能跑的引擎：whisperx / qwen_asr 的 transcribe 仍会抛 NotImplementedError，
# 存进偏好就等于让 UI 显示"识别成功"而结果始终为空
_ASR_ENGINES = ("faster_whisper",)
_MODEL_SIZES = ("tiny", "base", "small", "medium", "large")


class LLMTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str = Field(..., min_length=1, max_length=512, description="LLM API Key")
    base_url: str = Field(
        default="https://api.openai.com/v1", max_length=2048, description="LLM 端点 URL"
    )
    model: str = Field(default="gpt-4o-mini", max_length=128, description="模型名称")


class LLMTestResponse(BaseModel):
    success: bool = Field(..., description="是否连通")
    message: str = Field(..., description="结果描述")
    latency_ms: int = Field(..., description="延迟（毫秒）")


class SaveSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm_api_key: str | None = Field(default=None, max_length=512)
    avatar_api_key: str | None = Field(default=None, max_length=512)
    llm_base_url: str | None = Field(default=None, max_length=2048)
    asr_engine: Literal["faster_whisper"] | None = None
    asr_model_size: Literal["tiny", "base", "small", "medium", "large"] | None = None


class SaveSettingsResponse(BaseModel):
    success: bool
    message: str
    saved_keys: list[str]
    credential_persistent: bool


class SettingsResponse(BaseModel):
    llm_base_url: str | None
    has_llm_api_key: bool
    has_avatar_api_key: bool
    asr_engine: str
    asr_model_size: str
    credential_persistent: bool
    environment: str


@router.post("/llm/test", response_model=LLMTestResponse)
async def test_llm_connection(req: LLMTestRequest) -> LLMTestResponse:
    """向 LLM 端点发送一个最小请求，验证 API Key 和端点是否可用。"""
    start = time.perf_counter()

    def elapsed_ms() -> int:
        return int((time.perf_counter() - start) * 1000)

    try:
        base_url = await validate_endpoint_url_async(req.base_url)
    except UnsafeURLError as exc:
        logger.warning("llm_test_rejected", reason="unsafe_url")
        return LLMTestResponse(success=False, message=f"端点不被允许：{exc}", latency_ms=elapsed_ms())

    headers = {"Authorization": f"Bearer {req.api_key}", "Accept": "application/json"}
    models_url = f"{base_url}/models"

    try:
        async with httpx.AsyncClient(
            timeout=_TEST_TIMEOUT_SECONDS,
            follow_redirects=False,
            max_redirects=0,
        ) as client:
            resp = await client.get(models_url, headers=headers)
    except httpx.TimeoutException:
        return LLMTestResponse(success=False, message="连接超时，请检查网络或端点 URL", latency_ms=elapsed_ms())
    except httpx.HTTPError:
        # 不回显异常文本：其中可能含 URL 与请求头
        return LLMTestResponse(success=False, message="无法连接，请检查端点 URL 是否正确", latency_ms=elapsed_ms())

    if resp.status_code == 200:
        if len(resp.content) > _MAX_RESPONSE_BYTES:
            return LLMTestResponse(success=False, message="端点响应过大", latency_ms=elapsed_ms())
        try:
            data = resp.json()
        except ValueError:
            return LLMTestResponse(success=False, message="端点未返回 JSON", latency_ms=elapsed_ms())
        models = data.get("data") if isinstance(data, dict) else None
        model_count = len(models) if isinstance(models, list) else 0
        logger.info("llm_test_success", latency_ms=elapsed_ms(), model_count=model_count)
        return LLMTestResponse(
            success=True,
            message=f"连接成功，可用模型 {model_count} 个",
            latency_ms=elapsed_ms(),
        )

    if resp.status_code == 401:
        return LLMTestResponse(success=False, message="API Key 无效（401 Unauthorized）", latency_ms=elapsed_ms())
    if resp.status_code == 403:
        return LLMTestResponse(success=False, message="端点拒绝访问（403）", latency_ms=elapsed_ms())
    if resp.status_code == 404:
        return LLMTestResponse(success=False, message="端点不存在（404），请确认 base_url 是否含 /v1", latency_ms=elapsed_ms())

    logger.warning("llm_test_upstream_status", status=resp.status_code)
    return LLMTestResponse(
        success=False, message=f"上游返回 HTTP {resp.status_code}", latency_ms=elapsed_ms()
    )


@router.get("", response_model=SettingsResponse)
async def read_settings() -> SettingsResponse:
    """读取当前设置。机密只返回是否存在，不返回值。"""
    store = get_credential_store()
    prefs = await get_preferences()

    return SettingsResponse(
        llm_base_url=_read_optional(store, "llm_base_url"),
        has_llm_api_key=bool(_read_optional(store, "llm_api_key")),
        has_avatar_api_key=bool(_read_optional(store, "avatar_api_key")),
        asr_engine=prefs.get("asr_engine", "faster_whisper"),
        asr_model_size=prefs.get("asr_model_size", "small"),
        credential_persistent=store.is_persistent,
        environment=get_settings().environment,
    )


@router.post("/save", response_model=SaveSettingsResponse)
async def save_settings(req: SaveSettingsRequest) -> SaveSettingsResponse:
    """保存设置：机密进 CredentialStore，偏好进 app_settings 表。"""
    store = get_credential_store()
    payload = req.model_dump(exclude_unset=True)
    saved: list[str] = []
    written: list[tuple[str, str | None]] = []  # (key, 原值) 供回滚

    # 先做完整校验，避免"写了一半才失败"
    if req.llm_base_url not in (None, ""):
        try:
            await validate_endpoint_url_async(req.llm_base_url)
        except UnsafeURLError as exc:
            raise HTTPException(status_code=422, detail=f"LLM 端点不被允许：{exc}") from exc

    if req.asr_engine is not None and req.asr_engine not in _ASR_ENGINES:
        raise HTTPException(status_code=422, detail=f"未知 ASR 引擎: {req.asr_engine}")
    if req.asr_model_size is not None and req.asr_model_size not in _MODEL_SIZES:
        raise HTTPException(status_code=422, detail=f"未知模型尺寸: {req.asr_model_size}")

    secret_fields = {"llm_api_key": req.llm_api_key, "avatar_api_key": req.avatar_api_key}
    if req.llm_base_url is not None:
        secret_fields["llm_base_url"] = req.llm_base_url

    try:
        for key, value in secret_fields.items():
            if key not in payload or value is None:
                continue
            previous = store.get_secret(key)
            store.set_secret(key, value)  # 空串表示清除
            written.append((key, previous))
            saved.append(key)
    except CredentialStoreError as exc:
        _rollback(store, written)
        logger.error("settings_save_failed", key_count=len(saved))
        raise HTTPException(status_code=500, detail="凭据保存失败") from exc

    prefs = {
        key: value
        for key, value in (
            ("asr_engine", req.asr_engine),
            ("asr_model_size", req.asr_model_size),
        )
        if key in payload and value is not None
    }
    if prefs:
        try:
            saved.extend(await set_preferences(prefs))
        except (PreferenceError, OSError) as exc:
            _rollback(store, written)
            logger.error("preferences_save_failed")
            raise HTTPException(status_code=500, detail="偏好保存失败") from exc

    logger.info("settings_saved", keys=saved, persistent=store.is_persistent)
    message = f"已保存: {', '.join(saved)}" if saved else "没有需要保存的项"
    if not store.is_persistent:
        message += "（警告：当前凭据后端不可用，机密仅在本进程内存中，重启即失效）"

    return SaveSettingsResponse(
        success=True,
        message=message,
        saved_keys=saved,
        credential_persistent=store.is_persistent,
    )


def _rollback(store: WinCredentialStore, written: list[tuple[str, str | None]]) -> None:
    for key, previous in reversed(written):
        try:
            store.set_secret(key, previous or "")
        except CredentialStoreError:  # pragma: no cover - 回滚失败只能记录
            logger.error("settings_rollback_failed", key=key)


def _read_optional(store: WinCredentialStore, key: str) -> str | None:
    try:
        return store.get_secret(key)
    except CredentialStoreError:
        return None
