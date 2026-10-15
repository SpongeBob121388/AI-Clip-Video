"""P1 新增链路的守卫：音轨越界、乐观并发、能力探测要说真话。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.api.pipeline import _check_audio_track, _require_current_revision
from app.models.enums import OperationType
from app.models.media import MediaProbe, MediaTrack
from app.models.render_plan import CRF_MAX, CRF_MIN, RenderPreset
from app.providers.ffmpeg.runner import FFmpegError
from app.providers.registry import get_providers


def _probe(track_count: int) -> MediaProbe:
    tracks = [MediaTrack(index=i, codec_type="audio", codec_name="aac") for i in range(track_count)]
    return MediaProbe(
        file_path="src.mp4", duration_us=10_000_000, width=1920, height=1080, tracks=tracks
    )


class _StubPipelineService:
    def __init__(self, probe: MediaProbe) -> None:
        self._probe = probe
        self.probe_calls = 0

    async def probe_source(self, project_id: str) -> MediaProbe:
        self.probe_calls += 1
        return self._probe


@pytest.mark.unit
class TestAudioTrackGuard:
    async def test_default_preset_does_not_even_probe(self, monkeypatch) -> None:
        stub = _StubPipelineService(_probe(2))
        monkeypatch.setattr("app.api.pipeline.get_pipeline_service", lambda: stub)
        await _check_audio_track("pid", RenderPreset())
        assert stub.probe_calls == 0

    async def test_existing_track_is_accepted(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "app.api.pipeline.get_pipeline_service",
            lambda: _StubPipelineService(_probe(2)),
        )
        await _check_audio_track("pid", RenderPreset(audio_track_index=1))

    async def test_out_of_range_track_is_rejected_before_the_job(self, monkeypatch) -> None:
        # 渲染跑到一半才报 "Stream map '0:a:7' matches no streams" 等于白等几分钟编码
        monkeypatch.setattr(
            "app.api.pipeline.get_pipeline_service",
            lambda: _StubPipelineService(_probe(2)),
        )
        with pytest.raises(HTTPException) as caught:
            await _check_audio_track("pid", RenderPreset(audio_track_index=7))
        assert caught.value.status_code == 409
        assert "2 条音轨" in str(caught.value.detail)

    async def test_silent_source_rejects_any_selection(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "app.api.pipeline.get_pipeline_service",
            lambda: _StubPipelineService(_probe(0)),
        )
        with pytest.raises(HTTPException) as caught:
            await _check_audio_track("pid", RenderPreset(audio_track_index=0))
        assert caught.value.status_code == 409


@pytest.mark.unit
class TestRevisionGuard:
    def test_no_expectation_means_no_check(self) -> None:
        _require_current_revision(uuid4(), None)

    @pytest.mark.parametrize(
        "current",
        [None, uuid4()],
    )
    def test_mismatch_is_rejected(self, current) -> None:
        with pytest.raises(HTTPException) as caught:
            _require_current_revision(current, uuid4())
        assert caught.value.status_code == 409

    def test_matching_revision_passes(self) -> None:
        revision = uuid4()
        _require_current_revision(revision, revision)

    async def test_undo_with_a_stale_revision_is_rejected(self, async_client, tmp_path) -> None:
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
        created = await async_client.post(
            "/api/v1/projects", json={"name": "并发守卫", "source_path": str(path)}
        )
        pid = created.json()["project_id"]

        response = await async_client.post(
            f"/api/v1/projects/{pid}/undo", params={"expected_revision_id": str(uuid4())}
        )
        assert response.status_code == 409

    async def test_decisions_with_a_stale_revision_are_rejected(
        self, async_client, tmp_path
    ) -> None:
        from app.models.edl import EDL, EDLSource, Suggestion
        from app.models.enums import SuggestionStatus, SuggestionType
        from app.services.project_service import get_project_service

        path = tmp_path / "clip.mp4"
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
        created = await async_client.post(
            "/api/v1/projects", json={"name": "并发守卫", "source_path": str(path)}
        )
        pid = created.json()["project_id"]

        edl = EDL(
            source=EDLSource(uri=str(path), duration_us=10_000_000),
            suggestions=[
                Suggestion(
                    id="cut-1",
                    source_start_us=1_000_000,
                    source_end_us=2_000_000,
                    suggestion_type=SuggestionType.SILENCE,
                    status=SuggestionStatus.PENDING,
                )
            ],
        )
        await get_project_service().save_edl(pid, edl, OperationType.MANUAL_EDIT, {})

        response = await async_client.post(
            f"/api/v1/projects/{pid}/suggestions/decisions",
            json={
                "suggestion_ids": ["cut-1"],
                "status": "accepted",
                "expected_revision_id": str(uuid4()),
            },
        )
        assert response.status_code == 409


@pytest.mark.unit
class TestCapabilitiesHonesty:
    async def test_declares_the_new_knobs(self, async_client) -> None:
        data = (await async_client.get("/api/v1/capabilities")).json()
        assert data["crf_range"] == [CRF_MIN, CRF_MAX]
        assert data["fps_options"] == [25, 30, 60]
        assert data["transcript_editable"] is True

    async def test_ffmpeg_flag_is_false_when_the_binary_fails(self, async_client, monkeypatch) -> None:
        class _BrokenFFmpeg:
            async def check_version(self) -> tuple[int, int]:
                raise FFmpegError("找不到 ffmpeg")

        monkeypatch.setattr(get_providers(), "ffmpeg", _BrokenFFmpeg())
        data = (await async_client.get("/api/v1/capabilities")).json()
        assert data["ffmpeg_available"] is False

    async def test_ffmpeg_flag_is_true_for_a_real_binary(self, async_client) -> None:
        data = (await async_client.get("/api/v1/capabilities")).json()
        assert data["ffmpeg_available"] is True
