"""完整链路 HTTP 端到端测试。

覆盖：建项目 → 分析 → 手工增删区间 → 批量确认 → 撤销/重做 → 预览 → 导出 → 取流。
这条用例的意义在于证明"服务被接成了 API"：此前这些服务只有单测在调用自己的 mock，
真实 HTTP 路径一次都没走过。没有 FFmpeg 的环境会 skip。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.conftest import SOURCE_DURATION_US

pytestmark = pytest.mark.e2e

CUT_START_US = 2_000_000
CUT_END_US = 3_000_000
EXPECTED_OUT_US = SOURCE_DURATION_US - (CUT_END_US - CUT_START_US)


async def _await_job(client, job_id: str, timeout: float = 120.0) -> dict[str, Any]:
    """轮询任务直到终态；超时直接失败而不是静默返回运行中。"""

    async def _poll() -> dict[str, Any]:
        while True:
            response = await client.get(f"/api/v1/jobs/{job_id}")
            assert response.status_code == 200, response.text
            payload = response.json()
            if payload["status"] in {"completed", "failed", "cancelled"}:
                return payload
            await asyncio.sleep(0.05)

    return await asyncio.wait_for(_poll(), timeout=timeout)


async def _create_project(client, source_video: Path) -> str:
    response = await client.post(
        "/api/v1/projects",
        json={"name": "端到端项目", "source_path": str(source_video)},
    )
    assert response.status_code == 200, response.text
    return response.json()["project_id"]


@pytest.mark.asyncio
async def test_full_editing_chain_over_http(
    async_client, source_video: Path, tmp_path: Path
) -> None:
    client = async_client
    project_id = await _create_project(client, source_video)

    # ---------------------------------------------------------------- 分析
    submit = await client.post(
        f"/api/v1/projects/{project_id}/analyze",
        json={"use_asr": False, "min_silence_ms": 500, "dbfs_threshold": -60.0},
    )
    assert submit.status_code == 200, submit.text
    analyze = await _await_job(client, submit.json()["job_id"])
    assert analyze["status"] == "completed", analyze
    assert analyze["result"]["duration_us"] == pytest.approx(SOURCE_DURATION_US, abs=400_000)
    # 连续正弦音里没有 0.5s 以上的静音，候选数应为 0 而不是报错
    assert analyze["result"]["candidate_count"] == 0
    assert analyze["result"]["asr_skipped_reason"] == "本次分析未启用语音识别"

    edl_response = await client.get(f"/api/v1/projects/{project_id}/edl")
    assert edl_response.status_code == 200, edl_response.text
    body = edl_response.json()
    assert body["expected_duration_us"] == pytest.approx(SOURCE_DURATION_US, abs=400_000)

    # ---------------------------------------------------------------- 手工编辑
    operation = await client.post(
        f"/api/v1/projects/{project_id}/edl/operations",
        json={
            "operation": {
                "operation_type": "add",
                "source_start_us": CUT_START_US,
                "source_end_us": CUT_END_US,
            }
        },
    )
    assert operation.status_code == 200, operation.text
    edited = operation.json()
    assert len(edited["edl"]["suggestions"]) == 1
    suggestion_id = edited["edl"]["suggestions"][0]["id"]
    # 只是"新增建议"，尚未确认删除，输出时长仍等于源时长
    assert edited["expected_duration_us"] == SOURCE_DURATION_US

    accepted = await client.post(
        f"/api/v1/projects/{project_id}/suggestions/decisions",
        json={"suggestion_ids": [suggestion_id], "status": "accepted"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["edl"]["suggestions"][0]["status"] == "accepted"
    assert accepted.json()["expected_duration_us"] == EXPECTED_OUT_US

    # 批量确认里混入不存在的 ID 必须整条拒绝，而不是"改了一半"
    partial = await client.post(
        f"/api/v1/projects/{project_id}/suggestions/decisions",
        json={"suggestion_ids": [suggestion_id, "cut-does-not-exist"], "status": "rejected"},
    )
    assert partial.status_code == 422

    # ---------------------------------------------------------------- 撤销/重做
    undone = await client.post(f"/api/v1/projects/{project_id}/undo")
    assert undone.status_code == 200, undone.text
    assert undone.json()["edl"]["suggestions"][0]["status"] != "accepted"

    redone = await client.post(f"/api/v1/projects/{project_id}/redo")
    assert redone.status_code == 200, redone.text
    assert redone.json()["edl"]["suggestions"][0]["status"] == "accepted"
    revision_id = redone.json()["revision_id"]

    revisions = await client.get(f"/api/v1/projects/{project_id}/revisions")
    assert revisions.status_code == 200
    assert revision_id in {row["revision_id"] for row in revisions.json()["revisions"]}

    # ---------------------------------------------------------------- 字幕诚实失败
    subtitle = await client.post(
        f"/api/v1/projects/{project_id}/subtitles", json={"format": "ass"}
    )
    assert subtitle.status_code == 409  # 没有 ASR 结果时不能伪造字幕

    # ---------------------------------------------------------------- 预览
    preview_submit = await client.post(
        f"/api/v1/projects/{project_id}/preview",
        json={"preset": {"subtitle_mode": "none"}},
    )
    assert preview_submit.status_code == 200, preview_submit.text
    preview = await _await_job(client, preview_submit.json()["job_id"])
    assert preview["status"] == "completed", preview
    preview_path = Path(preview["result"]["output_path"])
    assert preview_path.is_file()
    assert preview["result"]["duration_us"] == pytest.approx(EXPECTED_OUT_US, abs=500_000)

    # ---------------------------------------------------------------- 导出
    output = tmp_path / "export" / "final.mp4"
    export_submit = await client.post(
        f"/api/v1/projects/{project_id}/export",
        json={
            "preset": {
                "resolution": "720p",
                "subtitle_mode": "none",
                "aspect_ratio": "16:9",
                "layout_mode": "smart_crop",
            },
            "output_path": str(output),
        },
    )
    assert export_submit.status_code == 200, export_submit.text
    export = await _await_job(client, export_submit.json()["job_id"])
    assert export["status"] == "completed", export
    assert output.is_file()
    assert export["result"]["duration_us"] == pytest.approx(EXPECTED_OUT_US, abs=500_000)

    # ---------------------------------------------------------------- 取流
    ticket = await client.post(
        "/api/v1/media/stream-ticket", json={"file_path": str(preview_path)}
    )
    assert ticket.status_code == 200, ticket.text
    url = ticket.json()["url"]

    ranged = await client.get(url, headers={"Range": "bytes=0-1023"})
    assert ranged.status_code == 206, ranged.text
    assert ranged.headers["Content-Length"] == "1024"
    assert len(ranged.content) == 1024

    whole = await client.get(url)
    assert whole.status_code == 200
    assert len(whole.content) == preview_path.stat().st_size

    # 票据只覆盖服务器自己的产物目录；仓库外的任意路径必须被拒绝
    outside = await client.post(
        "/api/v1/media/stream-ticket", json={"file_path": str(output)}
    )
    assert outside.status_code == 403

    # 源文件通过项目记录取证（路径由服务端保存，不接受客户端任意指定）
    source_ticket = await client.post(
        "/api/v1/media/source-ticket", json={"project_id": project_id}
    )
    assert source_ticket.status_code == 200, source_ticket.text
    play = await client.get(source_ticket.json()["url"])
    assert play.status_code == 200
    assert len(play.content) == source_video.stat().st_size

    # 未签名的票据不能取流
    forged = await client.get("/api/v1/media/stream?ticket=000000000000000000000000000000")
    assert forged.status_code == 401

@pytest.mark.asyncio
async def test_attach_source_records_probe_metadata(
    async_client, source_video: Path
) -> None:
    """绑定源文件就要落库真实时长，否则项目详情只能显示 0 直到首次分析。"""
    client = async_client
    create = await client.post("/api/v1/projects", json={"name": "绑定源文件"})
    project_id = create.json()["project_id"]
    assert create.json()["duration_us"] == 0

    attach = await client.put(
        f"/api/v1/projects/{project_id}/source", json={"file_path": str(source_video)}
    )
    assert attach.status_code == 200, attach.text
    assert attach.json()["duration_us"] == pytest.approx(SOURCE_DURATION_US, abs=400_000)

    detail = await client.get(f"/api/v1/projects/{project_id}")
    assert detail.json()["duration_us"] > 0


@pytest.mark.asyncio
async def test_attach_source_survives_unprobeable_file(
    async_client, tmp_path: Path
) -> None:
    """探测失败只降级为「未分析」，绑定本身仍然成功且不写脏数据。"""
    client = async_client
    junk = tmp_path / "junk.mp4"
    junk.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    create = await client.post("/api/v1/projects", json={"name": "坏文件"})
    project_id = create.json()["project_id"]

    attach = await client.put(
        f"/api/v1/projects/{project_id}/source", json={"file_path": str(junk)}
    )
    assert attach.status_code == 200, attach.text
    assert attach.json()["duration_us"] == 0
    assert attach.json()["source_path"] == str(junk)


@pytest.mark.asyncio
async def test_asr_failure_degrades_instead_of_failing_analysis(
    async_client, source_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """识别引擎炸了不能带走整次分析：静音检测用的是同一份 WAV。"""
    from app.models.enums import ASREngine
    from app.providers.asr.faster_whisper_adapter import FasterWhisperAdapter
    from app.providers.registry import Providers

    async def boom(self, *_args, **_kwargs):  # noqa: ANN001
        raise RuntimeError("模型下载失败: connection timeout")

    monkeypatch.setattr(FasterWhisperAdapter, "transcribe", boom)
    monkeypatch.setattr(FasterWhisperAdapter, "is_available", lambda self: True)
    monkeypatch.setattr(
        Providers, "available_asr_engines", lambda self: [ASREngine.FASTER_WHISPER]
    )

    client = async_client
    project_id = await _create_project(client, source_video)
    submit = await client.post(
        f"/api/v1/projects/{project_id}/analyze",
        json={"use_asr": True, "min_silence_ms": 500, "dbfs_threshold": -60.0},
    )
    result = await _await_job(client, submit.json()["job_id"])
    assert result["status"] == "completed", result
    reason = result["result"]["asr_skipped_reason"]
    assert reason and "connection timeout" in reason, reason
    # 检测阶段照常产出，分析结果不是空的
    assert result["result"]["duration_us"] > 0
