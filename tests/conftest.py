"""pytest 公共 fixture。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import reset_settings
from app.main import create_app
from app.providers.credential.win_credential_store import reset_credential_store
from app.providers.registry import reset_providers
from app.services.job_runner import reset_job_runner
from app.services.pipeline_service import reset_pipeline_service
from app.services.project_service import reset_project_service

TEST_TOKEN = "test-token"


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把数据目录、令牌与凭据后端隔离到 tmp，避免测试写入仓库或真实凭据库。"""
    data = tmp_path / "data"
    monkeypatch.setenv("ACV_DATA_DIR", str(data))
    monkeypatch.setenv("ACV_AUTH_TOKEN", TEST_TOKEN)
    monkeypatch.setenv("ACV_ENVIRONMENT", "development")
    monkeypatch.setenv("ACV_CREDENTIAL_BACKEND", "in_memory")
    reset_settings()
    reset_credential_store()
    _reset_singletons()
    yield data
    reset_settings()
    reset_credential_store()
    _reset_singletons()


def _reset_singletons() -> None:
    """服务/提供器都是进程级单例：不清理会让上一个测试的引擎与令牌泄漏到下一个。"""
    reset_providers()
    reset_pipeline_service()
    reset_project_service()
    reset_job_runner()


@pytest.fixture
def tmp_data_dir(isolated_settings: Path) -> Path:
    return isolated_settings


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


@asynccontextmanager
async def _authed_client(*, with_auth: bool) -> AsyncIterator[AsyncClient]:
    app = create_app()
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"} if with_auth else {}
    # 进入 lifespan，覆盖启动期的目录创建与日志配置
    async with (
        app.router.lifespan_context(app),
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
            headers=headers,
        ) as client,
    ):
        yield client


@pytest_asyncio.fixture
async def async_client() -> AsyncIterator[AsyncClient]:
    async with _authed_client(with_auth=True) as client:
        yield client


@pytest_asyncio.fixture
async def bare_client() -> AsyncIterator[AsyncClient]:
    """不带任何默认请求头的客户端，用来验证未鉴权时的真实响应。"""
    async with _authed_client(with_auth=False) as client:
        yield client
