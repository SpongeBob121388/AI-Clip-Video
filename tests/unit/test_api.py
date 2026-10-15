"""REST API 集成测试。"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest


@pytest.fixture
def media_file(tmp_path: Path) -> str:
    """真实存在的媒体文件：路径校验会拒绝不存在的文件。"""
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    return str(path)


@pytest.mark.unit
class TestHealthEndpoint:
    async def test_health(self, async_client) -> None:
        response = await async_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"


@pytest.mark.unit
class TestErrorSerialization:
    """坏输入必须是 4xx，不能因为错误详情无法编码而变成 500。"""

    async def test_surrogate_in_body_returns_422_not_500(self, async_client) -> None:
        response = await async_client.post(
            "/api/v1/projects",
            content=b'{"name": "\xed\xba\xa7"}',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 422, response.text
        assert "detail" in response.json()

    async def test_invalid_utf8_body_returns_400(self, async_client) -> None:
        response = await async_client.post(
            "/api/v1/projects",
            content=b'{"name": "\xc1\xf7"}',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code in (400, 422)

    async def test_chinese_name_round_trips_exactly(self, async_client) -> None:
        create = await async_client.post("/api/v1/projects", json={"name": "浏览器验证项目"})
        assert create.status_code == 200
        detail = await async_client.get(f"/api/v1/projects/{create.json()['project_id']}")
        assert detail.json()["name"] == "浏览器验证项目"


@pytest.mark.unit
class TestProjectsAPI:
    async def test_create_project(self, async_client, media_file: str) -> None:
        response = await async_client.post(
            "/api/v1/projects",
            json={"name": "测试项目", "source_path": media_file},
        )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["name"] == "测试项目"
        assert Path(data["source_path"]).name == "clip.mp4"

    async def test_create_project_rejects_missing_file(self, async_client, tmp_path: Path) -> None:
        response = await async_client.post(
            "/api/v1/projects",
            json={"name": "坏路径", "source_path": str(tmp_path / "missing.mp4")},
        )
        assert response.status_code == 400

    async def test_create_project_rejects_traversal(self, async_client) -> None:
        response = await async_client.post(
            "/api/v1/projects",
            json={"name": "遍历", "source_path": "C:\\Users\\..\\Windows\\x.mp4"},
        )
        assert response.status_code == 400

    async def test_list_projects(self, async_client) -> None:
        await async_client.post("/api/v1/projects", json={"name": "项目1"})
        await async_client.post("/api/v1/projects", json={"name": "项目2"})
        response = await async_client.get("/api/v1/projects")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] >= 2

    async def test_project_persists_across_service_calls(self, async_client) -> None:
        create = await async_client.post("/api/v1/projects", json={"name": "持久化"})
        project_id = create.json()["project_id"]
        listing = await async_client.get("/api/v1/projects")
        assert project_id in {p["project_id"] for p in listing.json()["projects"]}
        detail = await async_client.get(f"/api/v1/projects/{project_id}")
        assert detail.json()["name"] == "持久化"

    async def test_get_project(self, async_client) -> None:
        create_resp = await async_client.post(
            "/api/v1/projects", json={"name": "单项目"}
        )
        project_id = create_resp.json()["project_id"]
        response = await async_client.get(f"/api/v1/projects/{project_id}")
        assert response.status_code == 200
        assert response.json()["name"] == "单项目"

    async def test_get_unknown_uuid_returns_404(self, async_client) -> None:
        response = await async_client.get(f"/api/v1/projects/{uuid4()}")
        assert response.status_code == 404

    async def test_malformed_project_id_returns_400(self, async_client) -> None:
        response = await async_client.get("/api/v1/projects/nonexistent")
        assert response.status_code == 400

    async def test_delete_project(self, async_client) -> None:
        create_resp = await async_client.post(
            "/api/v1/projects", json={"name": "待删除"}
        )
        project_id = create_resp.json()["project_id"]
        response = await async_client.delete(f"/api/v1/projects/{project_id}")
        assert response.status_code == 200
        get_resp = await async_client.get(f"/api/v1/projects/{project_id}")
        assert get_resp.status_code == 404

    async def test_revisions_of_new_project_are_empty(self, async_client) -> None:
        create = await async_client.post("/api/v1/projects", json={"name": "版本"})
        project_id = create.json()["project_id"]
        response = await async_client.get(f"/api/v1/projects/{project_id}/revisions")
        assert response.status_code == 200
        assert response.json()["revisions"] == []
        assert response.json()["current_revision_id"] is None


@pytest.mark.unit
class TestJobsAPI:
    async def test_list_empty_jobs(self, async_client) -> None:
        response = await async_client.get("/api/v1/jobs")
        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 0

    async def test_get_nonexistent_job(self, async_client) -> None:
        response = await async_client.get("/api/v1/jobs/00000000-0000-0000-0000-000000000000")
        assert response.status_code == 404

    async def test_malformed_job_id(self, async_client) -> None:
        response = await async_client.get("/api/v1/jobs/not-a-uuid")
        assert response.status_code == 400
