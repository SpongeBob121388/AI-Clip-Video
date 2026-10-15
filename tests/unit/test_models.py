"""领域模型单元测试 — 序列化往返 + 校验逻辑。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.asr_result import ASRResult, Utterance, WordSpan
from app.models.avatar import AvatarSynthesisRequest, SynthesisJob, VoiceConfig
from app.models.edl import EDL, EDLOutput, EDLSource, Suggestion, TimeBase
from app.models.enums import (
    AspectRatio,
    ASREngine,
    Decision,
    JobStatus,
    ModelSize,
    Resolution,
    SubtitleFormat,
    SubtitleMode,
    SuggestionStatus,
    SuggestionType,
)
from app.models.media import MediaProbe, MediaTrack
from app.models.render_plan import FFmpegStep, RenderJob, RenderPlan, RenderPreset
from app.models.revision import ProjectRevision
from app.models.silence_candidate import SilenceCandidate
from app.models.subtitle_config import SubtitleConfig, SubtitleStyle


class TestEnums:
    def test_suggestion_type_values(self) -> None:
        assert SuggestionType.SILENCE.value == "silence"
        assert SuggestionType.FILLER_WORD.value == "filler_word"

    def test_decision_values(self) -> None:
        assert Decision.DELETE.value == "delete"
        assert Decision.KEEP.value == "keep"
        assert Decision.UNCERTAIN.value == "uncertain"

    def test_aspect_ratio(self) -> None:
        assert AspectRatio.VERTICAL.value == "9:16"
        assert AspectRatio.HORIZONTAL.value == "16:9"


class TestEDLModel:
    def _make_source(self) -> EDLSource:
        return EDLSource(
            uri="source.mp4",
            duration_us=612_340_000,
            time_base=TimeBase(num=1, den=90000),
        )

    def test_edl_source_creation(self) -> None:
        source = self._make_source()
        assert source.duration_us == 612_340_000
        assert source.is_vfr is False
        assert source.rotation == 0

    def test_edl_source_invalid_rotation(self) -> None:
        with pytest.raises(ValueError, match="rotation"):
            EDLSource(uri="x.mp4", duration_us=1000, rotation=45)

    def test_suggestion_time_validation(self) -> None:
        with pytest.raises(ValueError, match="source_end_us"):
            Suggestion(
                id="cut-1",
                source_start_us=100,
                source_end_us=50,
                suggestion_type=SuggestionType.SILENCE,
            )

    def test_edl_creation_and_roundtrip(self) -> None:
        edl = EDL(
            source=self._make_source(),
            suggestions=[
                Suggestion(
                    id="cut-001",
                    source_start_us=12_400_000,
                    source_end_us=15_100_000,
                    suggestion_type=SuggestionType.SILENCE,
                    reason="false_start",
                )
            ],
            output=EDLOutput(aspect_ratio=AspectRatio.VERTICAL),
        )
        json_data = edl.to_json()
        restored = EDL.from_json(json_data)
        assert restored.source.uri == "source.mp4"
        assert len(restored.suggestions) == 1
        assert restored.suggestions[0].id == "cut-001"

    def test_edl_overlapping_suggestions_rejected(self) -> None:
        with pytest.raises(ValueError, match="重叠"):
            EDL(
                source=self._make_source(),
                suggestions=[
                    Suggestion(
                        id="cut-1",
                        source_start_us=100,
                        source_end_us=300,
                        suggestion_type=SuggestionType.SILENCE,
                    ),
                    Suggestion(
                        id="cut-2",
                        source_start_us=200,
                        source_end_us=400,
                        suggestion_type=SuggestionType.SILENCE,
                    ),
                ],
            )

    def test_edl_accepted_pending_filters(self) -> None:
        edl = EDL(
            source=self._make_source(),
            suggestions=[
                Suggestion(
                    id="s1",
                    source_start_us=0,
                    source_end_us=100,
                    suggestion_type=SuggestionType.SILENCE,
                    status=SuggestionStatus.ACCEPTED,
                ),
                Suggestion(
                    id="s2",
                    source_start_us=200,
                    source_end_us=300,
                    suggestion_type=SuggestionType.SILENCE,
                    status=SuggestionStatus.PENDING,
                ),
            ],
        )
        assert len(edl.accepted_suggestions()) == 1
        assert len(edl.pending_suggestions()) == 1


class TestRevisionModel:
    def test_revision_creation(self) -> None:
        rev = ProjectRevision(
            project_id=uuid4(),
            edl_snapshot='{"test": true}',
            operation_type="import",
        )
        assert rev.revision_number == 0
        assert rev.parent_revision_id is None

    def test_revision_roundtrip(self) -> None:
        rev = ProjectRevision(
            project_id=uuid4(),
            edl_snapshot='{"test": true}',
            operation_type="manual_edit",
        )
        restored = ProjectRevision.from_json(rev.to_json())
        assert str(restored.project_id) == str(rev.project_id)


class TestRenderPlanModel:
    def test_render_preset_defaults(self) -> None:
        preset = RenderPreset()
        assert preset.resolution == Resolution.P1080
        assert preset.subtitle_mode == SubtitleMode.BURN_IN
        assert preset.aspect_ratio == AspectRatio.VERTICAL

    def test_render_plan_creation(self) -> None:
        plan_hash = "a" * 64
        plan = RenderPlan(
            project_revision_id=uuid4(),
            preset=RenderPreset(),
            ffmpeg_steps=[
                FFmpegStep(
                    step_index=0,
                    description="concat",
                    args=["-i", "input.mp4"],
                    output_file="output.mp4",
                )
            ],
            plan_hash=plan_hash,
        )
        assert len(plan.ffmpeg_steps) == 1
        assert plan.plan_hash == plan_hash

    def test_plan_hash_must_be_sha256_hex(self) -> None:
        """plan_hash 会被拼进缓存文件名，必须严格限形以防路径穿越。"""
        with pytest.raises(ValueError, match="十六进制"):
            RenderPlan(
                project_revision_id=uuid4(),
                preset=RenderPreset(),
                plan_hash="../../evil",
            )

    def test_render_job_defaults(self) -> None:
        job = RenderJob(plan_id=uuid4())
        assert job.status == JobStatus.PENDING
        assert job.progress == 0.0


class TestASRModel:
    def test_word_span_confidence_validation(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            WordSpan(
                word_id="w-1",
                start_us=0,
                end_us=100,
                text="你好",
                confidence=1.5,
            )

    def test_asr_result_creation(self) -> None:
        result = ASRResult(
            engine=ASREngine.FASTER_WHISPER,
            language="zh",
            model_size=ModelSize.MEDIUM,
            utterances=[
                Utterance(
                    utterance_id="u-1",
                    start_us=0,
                    end_us=5000000,
                    text="大家好",
                    words=[
                        WordSpan(
                            word_id="w-1",
                            start_us=0,
                            end_us=2000000,
                            text="大家",
                            confidence=0.95,
                        )
                    ],
                )
            ],
        )
        assert len(result.utterances) == 1
        assert result.utterances[0].words[0].confidence == 0.95


class TestSubtitleModel:
    def test_subtitle_style_defaults(self) -> None:
        style = SubtitleStyle()
        assert style.font_name == "Microsoft YaHei"
        assert style.font_size == 48
        assert style.alignment == 2

    def test_invalid_color_rejected(self) -> None:
        with pytest.raises(ValueError, match="颜色值"):
            SubtitleStyle(primary_color="#FFFFFF")

    def test_invalid_font_rejected(self) -> None:
        with pytest.raises(ValueError, match="字体不在白名单"):
            SubtitleStyle(font_name="EvilFont")

    def test_invalid_alignment_rejected(self) -> None:
        with pytest.raises(ValueError):
            SubtitleStyle(alignment=0)

    def test_subtitle_config_creation(self) -> None:
        config = SubtitleConfig(format=SubtitleFormat.ASS)
        assert config.format == SubtitleFormat.ASS
        assert config.max_chars_per_line == 20


class TestSilenceCandidate:
    def test_creation(self) -> None:
        candidate = SilenceCandidate(
            id="sil-1",
            start_us=1000,
            end_us=5000,
            detection_method="vad",
            avg_dbfs=-45.0,
        )
        assert candidate.duration_us == 4000

    def test_invalid_time_rejected(self) -> None:
        with pytest.raises(ValueError, match="end_us"):
            SilenceCandidate(
                id="sil-1",
                start_us=5000,
                end_us=1000,
                detection_method="vad",
                avg_dbfs=-45.0,
            )


class TestAvatarModel:
    def test_voice_config_defaults(self) -> None:
        vc = VoiceConfig()
        assert vc.speed == 1.0
        assert vc.pitch == 0.0

    def test_voice_config_validation(self) -> None:
        with pytest.raises(ValueError):
            VoiceConfig(speed=3.0)
        with pytest.raises(ValueError):
            VoiceConfig(pitch=20.0)

    def test_synthesis_request_creation(self) -> None:
        req = AvatarSynthesisRequest(
            text_script="大家好，欢迎观看",
            avatar_id="avatar-001",
        )
        assert req.avatar_id == "avatar-001"
        assert req.status.value == "pending"

    def test_empty_script_rejected(self) -> None:
        with pytest.raises(ValueError, match="text_script"):
            AvatarSynthesisRequest(text_script="   ", avatar_id="a1")

    def test_synthesis_job_defaults(self) -> None:
        job = SynthesisJob(request_id=uuid4())
        assert job.progress == 0.0


class TestMediaModel:
    def test_media_probe_creation(self) -> None:
        probe = MediaProbe(
            file_path="test.mp4",
            duration_us=10_000_000,
            width=1920,
            height=1080,
            fps=30.0,
            codec_video="h264",
            codec_audio="aac",
            tracks=[
                MediaTrack(
                    index=0,
                    codec_type="video",
                    codec_name="h264",
                    width=1920,
                    height=1080,
                )
            ],
        )
        assert probe.is_vertical is False
        assert probe.has_video is True
        assert probe.has_audio is False

    def test_invalid_rotation(self) -> None:
        with pytest.raises(ValueError, match="rotation"):
            MediaProbe(file_path="x.mp4", duration_us=1000, rotation=45)

    def test_invalid_time_base(self) -> None:
        with pytest.raises(ValueError, match="time_base"):
            MediaProbe(file_path="x.mp4", duration_us=1000, time_base="invalid")

    def test_vertical_detection(self) -> None:
        probe = MediaProbe(
            file_path="vertical.mp4",
            duration_us=1000,
            width=1080,
            height=1920,
        )
        assert probe.is_vertical is True
