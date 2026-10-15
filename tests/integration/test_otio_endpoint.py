"""OTIO 导出端点：从项目 EDL 到磁盘上的 .otio。

产物结构本身由 tests/e2e/test_otio_reader.py 用官方读取器把关，这里只验证接口链路
（鉴权后的项目 → 落盘路径 → 内容对应当前修订），以及"没有 EDL 时不能假装成功"。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models.edl import EDL, EDLSource, Suggestion
from app.models.enums import OperationType, SuggestionStatus, SuggestionType
from app.services.project_service import get_project_service

pytestmark = pytest.mark.integration


async def _project_with_edl(client, tmp_path: Path) -> tuple[str, Path]:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    created = await client.post(
        "/api/v1/projects", json={"name": "otio 端点", "source_path": str(source)}
    )
    pid = created.json()["project_id"]
    edl = EDL(
        source=EDLSource(uri=str(source), duration_us=10_000_000),
        suggestions=[
            Suggestion(
                id="cut-1",
                source_start_us=2_000_000,
                source_end_us=4_000_000,
                suggestion_type=SuggestionType.SILENCE,
                status=SuggestionStatus.ACCEPTED,
            )
        ],
    )
    await get_project_service().save_edl(pid, edl, OperationType.MANUAL_EDIT, {})
    return pid, source


class TestOtioEndpoint:
    async def test_export_writes_file_for_current_revision(self, async_client, tmp_path) -> None:
        pid, source = await _project_with_edl(async_client, tmp_path)

        response = await async_client.get(f"/api/v1/projects/{pid}/otio")
        assert response.status_code == 200, response.text

        target = Path(response.json()["output_path"])
        assert target.exists()
        data = json.loads(target.read_text(encoding="utf-8"))
        clips = data["tracks"]["children"][0]["children"]
        # 删掉 [2s,4s) 之后源被切成两段
        assert [(c["source_range"]["start_time"]["value"], c["source_range"]["duration"]["value"])
                for c in clips] == [(0, 2_000_000), (4_000_000, 6_000_000)]
        assert clips[0]["media_references"]["DEFAULT_MEDIA"]["target_url"] == source.as_uri()

    async def test_project_without_edl_does_not_claim_success(self, async_client, tmp_path) -> None:
        source = tmp_path / "clip.mp4"
        source.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
        created = await async_client.post(
            "/api/v1/projects", json={"name": "没有剪辑记录", "source_path": str(source)}
        )
        pid = created.json()["project_id"]

        response = await async_client.get(f"/api/v1/projects/{pid}/otio")
        assert response.status_code == 409

    async def test_capabilities_report_otio_available(self, async_client) -> None:
        body = (await async_client.get("/api/v1/capabilities")).json()
        assert body["otio_available"] is True
