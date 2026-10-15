"""EDL 服务 — 原子操作引擎、建议管理、持久化。

所有操作生成新 EDL（不可变更新），原 EDL 不变。
"""

from __future__ import annotations

import json
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.models.edl import (
    EDL,
    AddInterval,
    EDLOperation,
    MergeIntervals,
    ModifyInterval,
    OutputInterval,
    RemoveInterval,
    SplitInterval,
    Suggestion,
)
from app.models.enums import (
    Decision,
    DecisionSource,
    OperationType,
    SuggestionStatus,
    SuggestionType,
)
from app.persistence.models.project_models import EDLRevision
from app.utils.logging import get_logger

logger = get_logger("services.edl_service")


class EDLValidationError(ValueError):
    pass


class EDLService:
    @staticmethod
    def apply_operation(edl: EDL, operation: EDLOperation) -> EDL:
        new_edl = edl.model_copy(deep=True)
        duration = new_edl.source.duration_us

        if isinstance(operation, AddInterval):
            new_edl = EDLService._add_interval(new_edl, operation, duration)
        elif isinstance(operation, RemoveInterval):
            new_edl = EDLService._remove_interval(new_edl, operation)
        elif isinstance(operation, ModifyInterval):
            new_edl = EDLService._modify_interval(new_edl, operation, duration)
        elif isinstance(operation, MergeIntervals):
            new_edl = EDLService._merge_intervals(new_edl, operation)
        elif isinstance(operation, SplitInterval):
            new_edl = EDLService._split_interval(new_edl, operation)
        else:
            raise EDLValidationError(f"未知操作类型: {operation.operation_type}")

        errors = new_edl.validate_full()
        if errors:
            raise EDLValidationError(f"操作后校验失败: {errors}")

        return new_edl

    @staticmethod
    def _clamp(value: int, max_value: int) -> int:
        return max(0, min(value, max_value))

    @staticmethod
    def _add_interval(edl: EDL, op: AddInterval, duration: int) -> EDL:
        start = EDLService._clamp(op.source_start_us, duration)
        end = EDLService._clamp(op.source_end_us, duration)
        if end <= start:
            raise EDLValidationError("clamp 后区间无效")
        new_suggestion = Suggestion(
            id=f"cut-{uuid4().hex[:8]}",
            source_start_us=start,
            source_end_us=end,
            suggestion_type=SuggestionType.MANUAL,
            status=SuggestionStatus.PENDING,
            source=DecisionSource.MANUAL,
        )
        edl.suggestions.append(new_suggestion)
        edl.suggestions.sort(key=lambda s: s.source_start_us)
        return edl

    @staticmethod
    def _remove_interval(edl: EDL, op: RemoveInterval) -> EDL:
        matched = [
            s
            for s in edl.suggestions
            if s.source_start_us == op.source_start_us and s.source_end_us == op.source_end_us
        ]
        if not matched:
            raise EDLValidationError(
                f"未找到区间 [{op.source_start_us}, {op.source_end_us})，remove 不生效"
            )
        removed_ids = {s.id for s in matched}
        edl.suggestions = [s for s in edl.suggestions if s.id not in removed_ids]
        return edl

    @staticmethod
    def _modify_interval(edl: EDL, op: ModifyInterval, duration: int) -> EDL:
        new_start = EDLService._clamp(op.new_source_start_us, duration)
        new_end = EDLService._clamp(op.new_source_end_us, duration)
        if new_end <= new_start:
            raise EDLValidationError("修改后区间无效")

        candidates = [s for s in edl.suggestions if s.source_start_us == op.old_source_start_us]
        if not candidates:
            raise EDLValidationError(
                f"未找到 start_us={op.old_source_start_us} 的建议，modify 不生效"
            )
        if len(candidates) > 1:
            raise EDLValidationError(
                f"start_us={op.old_source_start_us} 匹配到 {len(candidates)} 个建议，无法确定目标"
            )

        target = candidates[0]
        # 只改边界，不改用户已经表过的态：以前这里强制写 modified，
        # 而 accepted_suggestions() 只认 accepted，拖一下边缘就把"删除"撤销了
        target.source_start_us = new_start
        target.source_end_us = new_end
        edl.suggestions.sort(key=lambda s: s.source_start_us)
        return edl

    @staticmethod
    def _merge_intervals(edl: EDL, op: MergeIntervals) -> EDL:
        to_merge = [
            s
            for s in edl.suggestions
            if s.source_start_us >= op.first_source_start_us
            and s.source_end_us <= op.second_source_end_us
        ]
        if len(to_merge) < 2:
            raise EDLValidationError("合并需要至少 2 个区间")

        # 按列表顺序取首末会在乱序 EDL 上产生倒挂边界，必须按时间取极值
        to_merge.sort(key=lambda s: s.source_start_us)
        statuses = {s.status for s in to_merge}
        # 决定一致时继承，否则回到 pending 让用户重新表态：
        # 把两个 accepted 合成 pending 等于悄悄撤销删除，把 accepted+rejected 合成 accepted 等于擅自删除
        merged_status = statuses.pop() if len(statuses) == 1 else SuggestionStatus.PENDING
        merged = Suggestion(
            id=f"cut-{uuid4().hex[:8]}",
            source_start_us=min(s.source_start_us for s in to_merge),
            source_end_us=max(s.source_end_us for s in to_merge),
            suggestion_type=SuggestionType.MANUAL,
            status=merged_status,
            source=DecisionSource.MANUAL,
        )
        ids_to_remove = {s.id for s in to_merge}
        edl.suggestions = [s for s in edl.suggestions if s.id not in ids_to_remove]
        edl.suggestions.append(merged)
        edl.suggestions.sort(key=lambda s: s.source_start_us)
        return edl

    @staticmethod
    def _split_interval(edl: EDL, op: SplitInterval) -> EDL:
        matched = [s for s in edl.suggestions if s.source_start_us == op.source_start_us]
        if not matched:
            raise EDLValidationError(
                f"未找到 start_us={op.source_start_us} 的建议，split 不生效"
            )
        if len(matched) > 1:
            raise EDLValidationError(
                f"start_us={op.source_start_us} 匹配到 {len(matched)} 个建议，无法确定目标"
            )

        target = matched[0]
        if not (target.source_start_us < op.split_at_us < target.source_end_us):
            raise EDLValidationError("split_at_us 必须在区间内部")

        first = target.model_copy(deep=True)
        first.id = f"cut-{uuid4().hex[:8]}"
        first.source_end_us = op.split_at_us
        second = target.model_copy(deep=True)
        second.id = f"cut-{uuid4().hex[:8]}"
        second.source_start_us = op.split_at_us

        edl.suggestions = [s for s in edl.suggestions if s.id != target.id]
        edl.suggestions.extend([first, second])
        edl.suggestions.sort(key=lambda s: s.source_start_us)
        return edl

    @staticmethod
    def compute_output_intervals(edl: EDL) -> list[OutputInterval]:
        duration = edl.source.duration_us
        if duration <= 0:
            raise EDLValidationError("源时长必须为正")

        accepted = edl.accepted_suggestions()
        delete_ranges = sorted(
            (
                (
                    EDLService._clamp(s.source_start_us, duration),
                    EDLService._clamp(s.source_end_us, duration),
                )
                for s in accepted
                if s.source_end_us > s.source_start_us
            ),
            key=lambda x: x[0],
        )

        if not delete_ranges:
            return [OutputInterval(start_us=0, end_us=duration, source_start_us=0)]

        output: list[OutputInterval] = []
        current_source = 0
        current_output = 0

        for del_start, del_end in delete_ranges:
            if del_start > current_source:
                seg_duration = del_start - current_source
                output.append(
                    OutputInterval(
                        start_us=current_output,
                        end_us=current_output + seg_duration,
                        source_start_us=current_source,
                    )
                )
                current_output += seg_duration
            current_source = max(current_source, del_end)

        if current_source < duration:
            seg_duration = duration - current_source
            output.append(
                OutputInterval(
                    start_us=current_output,
                    end_us=current_output + seg_duration,
                    source_start_us=current_source,
                )
            )

        if not output:
            raise EDLValidationError("操作导致 output 为空")

        return output

    @staticmethod
    def source_to_output_time(
        edl: EDL, src_us: int, intervals: list[OutputInterval] | None = None
    ) -> int:
        """源时间轴 → 输出时间轴。

        落在被删除区间内的时间点塌缩到其后的第一个保留帧（即剪切点），
        而不是像旧实现那样一律返回末尾区间 end_us，导致片尾字幕整体错位。
        """
        resolved = intervals if intervals is not None else EDLService.compute_output_intervals(edl)
        if not resolved:
            return 0

        for iv in resolved:
            source_end = iv.source_start_us + iv.duration_us
            if iv.source_start_us <= src_us < source_end:
                return iv.start_us + (src_us - iv.source_start_us)
            if iv.source_start_us > src_us:
                return iv.start_us

        return resolved[-1].end_us

    @staticmethod
    def output_to_source_time(
        edl: EDL, out_us: int, intervals: list[OutputInterval] | None = None
    ) -> int:
        resolved = intervals if intervals is not None else EDLService.compute_output_intervals(edl)
        for iv in resolved:
            if iv.start_us <= out_us < iv.end_us:
                return iv.source_start_us + (out_us - iv.start_us)
            if iv.start_us > out_us:
                return iv.source_start_us
        return resolved[-1].source_start_us + resolved[-1].duration_us if resolved else 0

    @staticmethod
    def add_suggestions(edl: EDL, suggestions: list[Suggestion]) -> EDL:
        new_edl = edl.model_copy(deep=True)
        for s in suggestions:
            # 深拷贝入参：调用方持有的对象不应被服务层改写或共享进 EDL
            copy = s.model_copy(deep=True)
            copy.status = SuggestionStatus.PENDING
            new_edl.suggestions.append(copy)
        new_edl.suggestions.sort(key=lambda s: s.source_start_us)
        return new_edl

    @staticmethod
    def set_suggestion_status(
        edl: EDL, suggestion_id: str, status: SuggestionStatus
    ) -> EDL:
        """批量确认的原子写法：一次复制、一次返回，避免前端逐条 setState 覆盖。"""
        new_edl = edl.model_copy(deep=True)
        found = False
        for s in new_edl.suggestions:
            if s.id == suggestion_id:
                s.status = status
                if status == SuggestionStatus.ACCEPTED:
                    s.decision = Decision.DELETE
                elif status == SuggestionStatus.REJECTED:
                    s.decision = Decision.KEEP
                s.source = DecisionSource.MANUAL
                found = True
                break
        if not found:
            raise EDLValidationError(f"建议不存在: {suggestion_id}")
        return new_edl

    @staticmethod
    def confirm_suggestion(
        edl: EDL, suggestion_id: str, decision: Decision
    ) -> EDL:
        new_edl = edl.model_copy(deep=True)
        for s in new_edl.suggestions:
            if s.id == suggestion_id:
                s.decision = decision
                s.source = DecisionSource.MANUAL
                s.status = (
                    SuggestionStatus.ACCEPTED
                    if decision == Decision.DELETE
                    else SuggestionStatus.REJECTED
                )
                return new_edl
        raise EDLValidationError(f"建议不存在: {suggestion_id}")

    @staticmethod
    async def save_edl(
        engine: AsyncEngine,
        project_id: str,
        edl: EDL,
        revision_id: UUID | None = None,
        parent_revision_id: UUID | None = None,
        operation_type: OperationType = OperationType.MANUAL_EDIT,
    ) -> UUID:
        rev_id = revision_id or uuid4()
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        async with sessionmaker() as session:
            rev = EDLRevision(
                revision_id=str(rev_id),
                project_id=project_id,
                parent_revision_id=(
                    str(parent_revision_id) if parent_revision_id else None
                ),
                edl_snapshot_json=json.dumps(edl.to_json(), ensure_ascii=False),
                operation_type=operation_type.value,
                operation_detail_json="{}",
            )
            session.add(rev)
            await session.commit()
        logger.info(
            "edl_saved",
            project_id=project_id,
            revision_id=str(rev_id),
            parent_revision_id=str(parent_revision_id) if parent_revision_id else None,
        )
        return rev_id

    @staticmethod
    async def load_edl(engine: AsyncEngine, revision_id: UUID) -> EDL:
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        async with sessionmaker() as session:
            result = await session.execute(
                select(EDLRevision).where(EDLRevision.revision_id == str(revision_id))
            )
            record = result.scalar_one_or_none()
            if record is None:
                raise EDLValidationError(f"版本不存在: {revision_id}")
            data = json.loads(record.edl_snapshot_json)
            return EDL.from_json(data)
