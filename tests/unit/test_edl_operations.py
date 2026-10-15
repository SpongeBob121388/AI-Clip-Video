"""EDL 原子操作单元测试。"""

from __future__ import annotations

import pytest

from app.models.edl import (
    EDL,
    AddInterval,
    EDLSource,
    MergeIntervals,
    ModifyInterval,
    RemoveInterval,
    SplitInterval,
    Suggestion,
    TimeBase,
)
from app.models.enums import (
    Decision,
    DecisionSource,
    SuggestionStatus,
    SuggestionType,
)
from app.services.edl_service import EDLService, EDLValidationError


def _make_edl(duration_us: int = 10_000_000) -> EDL:
    return EDL(
        source=EDLSource(
            uri="test.mp4",
            duration_us=duration_us,
            time_base=TimeBase(num=1, den=90000),
        ),
    )


def _make_suggestion(
    sid: str, start: int, end: int, status: SuggestionStatus = SuggestionStatus.ACCEPTED
) -> Suggestion:
    return Suggestion(
        id=sid,
        source_start_us=start,
        source_end_us=end,
        suggestion_type=SuggestionType.SILENCE,
        status=status,
        source=DecisionSource.LLM,
    )


@pytest.mark.unit
class TestEDLOperations:
    def test_add_interval(self) -> None:
        edl = _make_edl()
        op = AddInterval(source_start_us=1_000_000, source_end_us=2_000_000)
        new_edl = EDLService.apply_operation(edl, op)
        assert len(new_edl.suggestions) == 1
        assert new_edl.suggestions[0].source_start_us == 1_000_000

    def test_manual_operations_are_not_labelled_as_silence(self) -> None:
        """手工加/合并的区间必须记成 manual。

        以前 add 与 merge 都硬编码 suggestion_type="silence"，时间轴上用户自己加的段落
        会在列表里被显示成"静音建议"，按类型统计也跟着错。
        """
        edl = _make_edl()
        edl = EDLService.apply_operation(
            edl, AddInterval(source_start_us=1_000_000, source_end_us=2_000_000)
        )
        edl = EDLService.apply_operation(
            edl, AddInterval(source_start_us=3_000_000, source_end_us=4_000_000)
        )
        assert all(s.suggestion_type is SuggestionType.MANUAL for s in edl.suggestions)

        merged = EDLService.apply_operation(
            edl, MergeIntervals(first_source_start_us=1_000_000, second_source_end_us=4_000_000)
        )
        assert len(merged.suggestions) == 1
        assert merged.suggestions[0].suggestion_type is SuggestionType.MANUAL

    def test_immutability(self) -> None:
        edl = _make_edl()
        op = AddInterval(source_start_us=1_000_000, source_end_us=2_000_000)
        new_edl = EDLService.apply_operation(edl, op)
        assert len(edl.suggestions) == 0
        assert len(new_edl.suggestions) == 1

    def test_remove_interval(self) -> None:
        edl = _make_edl()
        edl = EDLService.apply_operation(
            edl, AddInterval(source_start_us=1_000_000, source_end_us=2_000_000)
        )
        new_edl = EDLService.apply_operation(
            edl, RemoveInterval(source_start_us=1_000_000, source_end_us=2_000_000)
        )
        assert len(new_edl.suggestions) == 0

    def test_modify_interval(self) -> None:
        edl = _make_edl()
        edl = EDLService.apply_operation(
            edl, AddInterval(source_start_us=1_000_000, source_end_us=2_000_000)
        )
        new_edl = EDLService.apply_operation(
            edl,
            ModifyInterval(
                old_source_start_us=1_000_000,
                new_source_start_us=1_500_000,
                new_source_end_us=3_000_000,
            ),
        )
        assert new_edl.suggestions[0].source_start_us == 1_500_000
        assert new_edl.suggestions[0].source_end_us == 3_000_000

    def test_modify_keeps_accepted_decision(self) -> None:
        """拖动已决定删除的块只该挪边界，不该把删除决定改掉。"""
        edl = _make_edl()
        edl.suggestions = [_make_suggestion("a", 1_000_000, 2_000_000, SuggestionStatus.ACCEPTED)]
        new_edl = EDLService.apply_operation(
            edl,
            ModifyInterval(
                old_source_start_us=1_000_000,
                new_source_start_us=1_200_000,
                new_source_end_us=2_400_000,
            ),
        )
        assert new_edl.suggestions[0].status is SuggestionStatus.ACCEPTED
        assert [
            # (该片段在源里的起点, 片段时长)：end_us 是输出时间轴，不能直接减 source_start_us
            (i.source_start_us, i.end_us - i.start_us)
            for i in EDLService.compute_output_intervals(new_edl)
        ] == [(0, 1_200_000), (2_400_000, 7_600_000)]

    def test_modify_keeps_pending_in_the_pending_set(self) -> None:
        """pending 被微调后必须还是 pending，否则"全部接受"会漏掉它。"""
        edl = _make_edl()
        edl.suggestions = [_make_suggestion("a", 1_000_000, 2_000_000, SuggestionStatus.PENDING)]
        new_edl = EDLService.apply_operation(
            edl,
            ModifyInterval(
                old_source_start_us=1_000_000,
                new_source_start_us=1_100_000,
                new_source_end_us=2_000_000,
            ),
        )
        assert [s.id for s in new_edl.pending_suggestions()] == ["a"]

    def test_merge_inherits_unanimous_decision(self) -> None:
        edl = _make_edl()
        edl.suggestions = [
            _make_suggestion("a", 1_000_000, 2_000_000, SuggestionStatus.ACCEPTED),
            _make_suggestion("b", 3_000_000, 4_000_000, SuggestionStatus.ACCEPTED),
        ]
        merged = EDLService.apply_operation(
            edl, MergeIntervals(first_source_start_us=1_000_000, second_source_end_us=4_000_000)
        )
        assert len(merged.suggestions) == 1
        assert merged.suggestions[0].status is SuggestionStatus.ACCEPTED
        assert merged.accepted_suggestions()[0].source_end_us == 4_000_000

    def test_merge_of_conflicting_decisions_asks_again(self) -> None:
        edl = _make_edl()
        edl.suggestions = [
            _make_suggestion("a", 1_000_000, 2_000_000, SuggestionStatus.ACCEPTED),
            _make_suggestion("b", 3_000_000, 4_000_000, SuggestionStatus.REJECTED),
        ]
        merged = EDLService.apply_operation(
            edl, MergeIntervals(first_source_start_us=1_000_000, second_source_end_us=4_000_000)
        )
        assert merged.suggestions[0].status is SuggestionStatus.PENDING
        assert merged.accepted_suggestions() == []

    def test_split_interval(self) -> None:
        edl = _make_edl()
        edl = EDLService.apply_operation(
            edl, AddInterval(source_start_us=1_000_000, source_end_us=3_000_000)
        )
        new_edl = EDLService.apply_operation(
            edl, SplitInterval(source_start_us=1_000_000, split_at_us=2_000_000)
        )
        assert len(new_edl.suggestions) == 2

    def test_clamp_to_source_duration(self) -> None:
        edl = _make_edl(duration_us=5_000_000)
        new_edl = EDLService.apply_operation(
            edl, AddInterval(source_start_us=4_000_000, source_end_us=10_000_000)
        )
        assert new_edl.suggestions[0].source_end_us == 5_000_000

    def test_unknown_operation_rejected(self) -> None:
        """判别符收窄为枚举后，非法 operation_type 在构造期即被拒绝。"""
        from pydantic import ValidationError

        from app.models.edl import EDLOperation

        with pytest.raises(ValidationError):
            EDLOperation(operation_type="unknown")

    def test_subclass_discriminator_is_enforced(self) -> None:
        from pydantic import ValidationError

        from app.models.edl import AddInterval

        with pytest.raises(ValidationError):
            AddInterval(operation_type="remove", source_start_us=1, source_end_us=2)

    def test_negative_interval_rejected_on_all_operations(self) -> None:
        from pydantic import ValidationError

        from app.models.edl import MergeIntervals, ModifyInterval, RemoveInterval

        with pytest.raises(ValidationError):
            RemoveInterval(source_start_us=5_000, source_end_us=5_000)
        with pytest.raises(ValidationError):
            ModifyInterval(
                old_source_start_us=0, new_source_start_us=9_000, new_source_end_us=9_000
            )
        with pytest.raises(ValidationError):
            MergeIntervals(first_source_start_us=9_000, second_source_end_us=1_000)


@pytest.mark.unit
class TestOutputIntervals:
    def test_no_deletions_full_output(self) -> None:
        edl = _make_edl(duration_us=10_000_000)
        intervals = EDLService.compute_output_intervals(edl)
        assert len(intervals) == 1
        assert intervals[0].start_us == 0
        assert intervals[0].end_us == 10_000_000

    def test_one_deletion(self) -> None:
        edl = _make_edl(duration_us=10_000_000)
        edl.suggestions.append(_make_suggestion("s1", 3_000_000, 5_000_000))
        intervals = EDLService.compute_output_intervals(edl)
        assert len(intervals) == 2
        assert intervals[0].start_us == 0
        assert intervals[0].end_us == 3_000_000
        assert intervals[1].start_us == 3_000_000
        assert intervals[1].end_us == 8_000_000

    def test_output_empty_rejected(self) -> None:
        edl = _make_edl(duration_us=10_000_000)
        edl.suggestions.append(_make_suggestion("s1", 0, 10_000_000))
        with pytest.raises(EDLValidationError, match="为空"):
            EDLService.compute_output_intervals(edl)


@pytest.mark.unit
class TestTimeMapping:
    def test_source_to_output_no_deletion(self) -> None:
        edl = _make_edl(duration_us=10_000_000)
        assert EDLService.source_to_output_time(edl, 5_000_000) == 5_000_000

    def test_source_to_output_with_deletion(self) -> None:
        edl = _make_edl(duration_us=10_000_000)
        edl.suggestions.append(_make_suggestion("s1", 3_000_000, 5_000_000))
        assert EDLService.source_to_output_time(edl, 2_000_000) == 2_000_000
        assert EDLService.source_to_output_time(edl, 6_000_000) == 4_000_000

    def test_output_to_source_roundtrip(self) -> None:
        edl = _make_edl(duration_us=10_000_000)
        edl.suggestions.append(_make_suggestion("s1", 3_000_000, 5_000_000))
        src = 6_000_000
        out = EDLService.source_to_output_time(edl, src)
        back = EDLService.output_to_source_time(edl, out)
        assert back == src


@pytest.mark.unit
class TestSuggestionManagement:
    def test_add_suggestions(self) -> None:
        edl = _make_edl()
        suggestions = [
            _make_suggestion("s1", 1_000_000, 2_000_000, SuggestionStatus.PENDING),
        ]
        new_edl = EDLService.add_suggestions(edl, suggestions)
        assert len(new_edl.suggestions) == 1
        assert new_edl.suggestions[0].status == SuggestionStatus.PENDING

    def test_confirm_suggestion_delete(self) -> None:
        edl = _make_edl()
        edl = EDLService.add_suggestions(
            edl, [_make_suggestion("s1", 1_000_000, 2_000_000, SuggestionStatus.PENDING)]
        )
        new_edl = EDLService.confirm_suggestion(edl, "s1", Decision.DELETE)
        s = next(s for s in new_edl.suggestions if s.id == "s1")
        assert s.status == SuggestionStatus.ACCEPTED
        assert s.source == DecisionSource.MANUAL

    def test_confirm_suggestion_keep(self) -> None:
        edl = _make_edl()
        edl = EDLService.add_suggestions(
            edl, [_make_suggestion("s1", 1_000_000, 2_000_000, SuggestionStatus.PENDING)]
        )
        new_edl = EDLService.confirm_suggestion(edl, "s1", Decision.KEEP)
        s = next(s for s in new_edl.suggestions if s.id == "s1")
        assert s.status == SuggestionStatus.REJECTED


@pytest.mark.unit
class TestEDLSerialization:
    def test_json_roundtrip(self) -> None:
        edl = _make_edl()
        edl = EDLService.add_suggestions(
            edl, [_make_suggestion("s1", 1_000_000, 2_000_000)]
        )
        json_data = edl.to_json()
        restored = EDL.from_json(json_data)
        assert len(restored.suggestions) == 1
        assert restored.suggestions[0].id == "s1"

    def test_validate_full(self) -> None:
        edl = _make_edl()
        errors = edl.validate_full()
        assert errors == []
