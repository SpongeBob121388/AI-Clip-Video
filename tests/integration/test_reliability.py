"""可靠性测试 — 渲染确定性。"""

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
from app.services.render_service import RenderService


def _make_edl(start: int = 1_000_000, end: int = 2_000_000) -> EDL:
    return EDL(
        source=EDLSource(uri="test.mp4", duration_us=10_000_000),
        suggestions=[
            Suggestion(
                id="s1",
                source_start_us=start,
                source_end_us=end,
                suggestion_type=SuggestionType.SILENCE,
                decision=Decision.DELETE,
                source=DecisionSource.RULE,
                status=SuggestionStatus.ACCEPTED,
            ),
        ],
    )


class TestRenderDeterminism:
    def test_same_edl_produces_same_hash(self) -> None:
        edl = _make_edl()
        preset = RenderPreset()
        rev_id = uuid4()
        plan1 = RenderService.generate_render_plan(edl, preset, rev_id, "src.mp4", "", "out.mp4")
        plan2 = RenderService.generate_render_plan(edl, preset, rev_id, "src.mp4", "", "out.mp4")
        assert plan1.plan_hash == plan2.plan_hash

    def test_different_edl_produces_different_hash(self) -> None:
        edl1 = _make_edl(1_000_000, 2_000_000)
        edl2 = _make_edl(3_000_000, 5_000_000)
        preset = RenderPreset()
        rev_id = uuid4()
        plan1 = RenderService.generate_render_plan(edl1, preset, rev_id, "src.mp4", "", "out.mp4")
        plan2 = RenderService.generate_render_plan(edl2, preset, rev_id, "src.mp4", "", "out.mp4")
        assert plan1.plan_hash != plan2.plan_hash
