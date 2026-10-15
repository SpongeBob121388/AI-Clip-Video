"""API 端到端测试 — REST API 完整流程验证。"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient


@pytest_asyncio.fixture
async def client(async_client: AsyncClient) -> AsyncClient:
    """复用 conftest 的客户端（带访问令牌并执行 lifespan），避免重复且弱化断言。"""
    return async_client


class TestAPIHealthE2E:
    @pytest.mark.asyncio
    async def test_health_endpoint(self, client: AsyncClient) -> None:
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    @pytest.mark.asyncio
    async def test_root_endpoint(self, client: AsyncClient) -> None:
        resp = await client.get("/")
        assert resp.status_code == 200
        body = resp.json()
        assert "name" in body
        assert "version" in body


class TestAPIProjectsE2E:
    @pytest.mark.asyncio
    async def test_list_projects(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/projects")
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_create_project(self, client: AsyncClient) -> None:
        resp = await client.post(
            "/api/v1/projects",
            json={"name": "test-project-e2e"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "test-project-e2e"
        assert body["project_id"]


class TestAPIJobsE2E:
    @pytest.mark.asyncio
    async def test_list_jobs(self, client: AsyncClient) -> None:
        resp = await client.get("/api/v1/jobs")
        assert resp.status_code == 200
