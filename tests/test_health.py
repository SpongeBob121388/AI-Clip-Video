"""健康检查端点冒烟测试。"""

from __future__ import annotations

import pytest


@pytest.mark.unit
async def test_health_returns_ok(async_client) -> None:
    response = await async_client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "app" in body
    assert "version" in body


@pytest.mark.unit
async def test_root_returns_app_info(async_client) -> None:
    response = await async_client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert "name" in body
    assert "version" in body
