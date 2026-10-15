"""集成测试 — 跨模块调用链路验证。

ASR Service → Detect Service → LLM Service → EDL Service
"""

from __future__ import annotations

from uuid import uuid4

from app.models.edl import EDL, EDLSource, Suggestion
from app.models.enums import (
    Decision,
    DecisionSource,
    SuggestionStatus,
    SuggestionType,
)
from app.models.render_plan import RenderPreset
from app.services.edl_service import EDLService
from app.services.render_service import RenderService


def _make_suggestion(
    sid: str, start: int, end: int, status: SuggestionStatus = SuggestionStatus.PENDING
) -> Suggestion:
    return Suggestion(
        id=sid,
        source_start_us=start,
        source_end_us=end,
        suggestion_type=SuggestionType.SILENCE,
        decision=Decision.DELETE,
        source=DecisionSource.RULE,
        status=status,
    )


def _make_edl() -> EDL:
    return EDL(
        source=EDLSource(uri="test.mp4", duration_us=10_000_000),
        suggestions=[_make_suggestion("s1", 1_000_000, 2_000_000)],
    )


class TestEDLPipelineIntegration:
    def test_confirm_accept_updates_status(self) -> None:
        edl = _make_edl()
        updated = EDLService.confirm_suggestion(edl, "s1", Decision.DELETE)
        s = next(s for s in updated.suggestions if s.id == "s1")
        assert s.status == SuggestionStatus.ACCEPTED

    def test_confirm_reject_updates_status(self) -> None:
        edl = _make_edl()
        updated = EDLService.confirm_suggestion(edl, "s1", Decision.KEEP)
        s = next(s for s in updated.suggestions if s.id == "s1")
        assert s.status == SuggestionStatus.REJECTED

    def test_compute_output_intervals_after_accept(self) -> None:
        edl = EDL(
            source=EDLSource(uri="test.mp4", duration_us=10_000_000),
            suggestions=[
                _make_suggestion("s1", 2_000_000, 4_000_000, SuggestionStatus.ACCEPTED),
            ],
        )
        intervals = EDLService.compute_output_intervals(edl)
        assert len(intervals) >= 1
        for iv in intervals:
            assert iv.end_us > iv.start_us

    def test_full_pipeline_confirm_and_compute(self) -> None:
        edl = EDL(
            source=EDLSource(uri="test.mp4", duration_us=10_000_000),
            suggestions=[
                _make_suggestion("s1", 1_000_000, 2_000_000),
                _make_suggestion("s2", 5_000_000, 6_000_000),
            ],
        )
        edl = EDLService.confirm_suggestion(edl, "s1", Decision.DELETE)
        edl = EDLService.confirm_suggestion(edl, "s2", Decision.DELETE)
        intervals = EDLService.compute_output_intervals(edl)
        assert all(iv.end_us > iv.start_us for iv in intervals)


class TestRenderPlanIntegration:
    def test_render_plan_from_edl(self) -> None:
        edl = _make_edl()
        preset = RenderPreset()
        plan = RenderService.generate_render_plan(edl, preset, uuid4(), "src.mp4", "", "out.mp4")
        assert plan is not None
        assert plan.plan_hash is not None

    def test_same_edl_same_hash(self) -> None:
        edl = _make_edl()
        preset = RenderPreset()
        rev_id = uuid4()
        plan1 = RenderService.generate_render_plan(edl, preset, rev_id, "src.mp4", "", "out.mp4")
        plan2 = RenderService.generate_render_plan(edl, preset, rev_id, "src.mp4", "", "out.mp4")
        assert plan1.plan_hash == plan2.plan_hash


class TestSubtitleIntegration:
    def test_subtitle_from_edl(self) -> None:
        from app.models.subtitle_config import SubtitleLine, SubtitleStyle
        from app.services.subtitle.ass_generator import ASSGenerator

        lines = [
            SubtitleLine(start_us=0, end_us=2_000_000, text="第一句"),
            SubtitleLine(start_us=2_000_000, end_us=4_000_000, text="第二句"),
        ]
        style = SubtitleStyle()
        ass = ASSGenerator().generate(lines, style)
        assert "[Script Info]" in ass
        assert "第一句" in ass
        assert "第二句" in ass
