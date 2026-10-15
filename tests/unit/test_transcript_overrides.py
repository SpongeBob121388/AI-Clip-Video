"""字幕文案人工覆盖：原始转录不动，覆盖在读取路径上套用。

起因是字幕文案改不了：识别错一个字要么重跑 ASR，要么手改导出出来的 .ass，
而手改的 .ass 在下次导出时会被重新生成的内容整体盖掉。
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.api.pipeline import _effective_transcript
from app.models.asr_result import ASRResult, Utterance, apply_text_overrides
from app.models.enums import ASREngine, ModelSize
from app.services.pipeline_service import get_pipeline_service


def _asr(*items: tuple[int, int, str]) -> ASRResult:
    return ASRResult(
        engine=ASREngine.FASTER_WHISPER,
        model_size=ModelSize.TINY,
        utterances=[
            Utterance(utterance_id=f"u-{index}", start_us=start, end_us=end, text=text)
            for index, (start, end, text) in enumerate(items)
        ],
    )


@pytest.mark.unit
class TestApplyTextOverrides:
    def test_empty_overrides_return_the_original_object(self) -> None:
        result = _asr((0, 1_000_000, "裸机"))
        effective, unapplied = apply_text_overrides(result, {})
        assert effective is result
        assert unapplied == []

    def test_override_replaces_text_matched_by_time_range(self) -> None:
        result = _asr((0, 1_000_000, "裸机"), (1_000_000, 2_000_000, "文本"))
        effective, unapplied = apply_text_overrides(result, {"0-1000000": "落机"})
        assert effective.utterances[0].text == "落机"
        assert effective.utterances[1].text == "文本"
        assert unapplied == []

    def test_original_transcript_is_not_mutated(self) -> None:
        result = _asr((0, 1_000_000, "裸机"))
        apply_text_overrides(result, {"0-1000000": "落机"})
        assert result.utterances[0].text == "裸机"

    def test_unmatched_key_is_reported_instead_of_swallowed(self) -> None:
        # 重跑识别后切分可能变；改动没生效必须回传，否则用户写的字凭空消失
        result = _asr((0, 1_000_000, "甲"))
        effective, unapplied = apply_text_overrides(result, {"9-99": "乙"})
        assert unapplied == ["9-99"]
        assert effective.utterances[0].text == "甲"

    def test_time_key_stays_out_of_the_serialised_payload(self) -> None:
        # 键是 property：混进 payload_json 会让旧转录读回来多字段（extra=forbid 直接报错）
        assert "time_key" not in _asr((0, 1_000_000, "甲")).to_json()["utterances"][0]


class _StubPipeline:
    def __init__(self, result: ASRResult | None, overrides: dict[str, str]) -> None:
        self._result = result
        self._overrides = overrides

    async def load_transcript(self, project_id: str) -> ASRResult | None:
        return self._result

    async def load_transcript_overrides(self, project_id: str) -> dict[str, str]:
        return self._overrides


@pytest.mark.unit
class TestEffectiveTranscriptWiring:
    """字幕端点与渲染期共用这条读取路径，才不会「预览改了、导出还是旧字」。"""

    async def test_overrides_reach_the_subtitles_generator(self) -> None:
        effective = await _effective_transcript(
            _StubPipeline(_asr((0, 1_000_000, "裸机")), {"0-1000000": "落机"}), "pid"
        )
        assert effective is not None
        assert effective.utterances[0].text == "落机"

    async def test_missing_transcript_stays_none(self) -> None:
        assert await _effective_transcript(_StubPipeline(None, {}), "pid") is None


@pytest.mark.unit
class TestOverridePersistence:
    async def test_round_trip_and_empty_text_removes(self) -> None:
        service = get_pipeline_service()
        pid = str(uuid4())
        await service.save_transcript_overrides(pid, {"0-1000000": "落机"})
        assert await service.load_transcript_overrides(pid) == {"0-1000000": "落机"}

        await service.save_transcript_overrides(pid, {"0-1000000": "   "})
        assert await service.load_transcript_overrides(pid) == {}


@pytest.mark.unit
class TestTranscriptOverrideAPI:
    async def _project(self, client, tmp_path) -> str:
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
        response = await client.post(
            "/api/v1/projects", json={"name": "字幕覆盖", "source_path": str(path)}
        )
        assert response.status_code == 200, response.text
        return str(response.json()["project_id"])

    async def test_put_before_analysis_is_rejected(self, async_client, tmp_path) -> None:
        pid = await self._project(async_client, tmp_path)
        response = await async_client.put(
            f"/api/v1/projects/{pid}/transcript/overrides",
            json={"overrides": [{"time_key": "0-1000000", "text": "落机"}]},
        )
        assert response.status_code == 409

    async def test_edit_then_read_reports_edited_state(self, async_client, tmp_path) -> None:
        pid = await self._project(async_client, tmp_path)
        await get_pipeline_service().save_transcript(pid, _asr((0, 1_000_000, "裸机")))

        before = await async_client.get(f"/api/v1/projects/{pid}/transcript")
        assert before.json()["utterances"][0]["edited"] is False

        put = await async_client.put(
            f"/api/v1/projects/{pid}/transcript/overrides",
            json={"overrides": [{"time_key": "0-1000000", "text": " 落机 "}],},
        )
        assert put.status_code == 200, put.text
        utterance = put.json()["utterances"][0]
        # 首尾空白没有意义，落库前应抹掉
        assert utterance["text"] == "落机"
        assert utterance["edited"] is True
        assert put.json()["unapplied_overrides"] == []

    async def test_undo_a_override_restores_machine_text(self, async_client, tmp_path) -> None:
        pid = await self._project(async_client, tmp_path)
        await get_pipeline_service().save_transcript(pid, _asr((0, 1_000_000, "裸机")))
        await async_client.put(
            f"/api/v1/projects/{pid}/transcript/overrides",
            json={"overrides": [{"time_key": "0-1000000", "text": "落机"}]},
        )
        undone = await async_client.put(
            f"/api/v1/projects/{pid}/transcript/overrides",
            json={"overrides": [{"time_key": "0-1000000", "text": ""}]},
        )
        assert undone.json()["utterances"][0]["text"] == "裸机"
        assert undone.json()["utterances"][0]["edited"] is False

    async def test_key_absent_from_current_transcript_is_rejected(
        self, async_client, tmp_path
    ) -> None:
        # 拦住任意键：否则覆盖表会被历次重跑识别留下的孤儿键越堆越大
        pid = await self._project(async_client, tmp_path)
        await get_pipeline_service().save_transcript(pid, _asr((0, 1_000_000, "裸机")))
        response = await async_client.put(
            f"/api/v1/projects/{pid}/transcript/overrides",
            json={"overrides": [{"time_key": "5000000-6000000", "text": "凭空"}]},
        )
        assert response.status_code == 409

    async def test_reanalysis_reports_overrides_that_no_longer_apply(
        self, async_client, tmp_path
    ) -> None:
        pid = await self._project(async_client, tmp_path)
        service = get_pipeline_service()
        await service.save_transcript(pid, _asr((0, 1_000_000, "裸机")))
        await async_client.put(
            f"/api/v1/projects/{pid}/transcript/overrides",
            json={"overrides": [{"time_key": "0-1000000", "text": "落机"}]},
        )
        # 重跑识别后切分变了：改动不再静默消失，而是明确报告出来
        await service.save_transcript(pid, _asr((0, 900_000, "裸机"), (900_000, 2_000_000, "文本")))

        got = await async_client.get(f"/api/v1/projects/{pid}/transcript")
        data = got.json()
        assert [u["text"] for u in data["utterances"]] == ["裸机", "文本"]
        assert data["unapplied_overrides"] == [{"time_key": "0-1000000", "text": "落机"}]

    async def test_malformed_time_key_is_rejected(self, async_client, tmp_path) -> None:
        pid = await self._project(async_client, tmp_path)
        response = await async_client.put(
            f"/api/v1/projects/{pid}/transcript/overrides",
            json={"overrides": [{"time_key": "abc", "text": "x"}]},
        )
        assert response.status_code == 422
