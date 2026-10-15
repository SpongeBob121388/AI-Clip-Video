"""Undo/Redo 管理器 — 基于 ProjectRevision 不可变快照链。

语义：project_meta.current_revision_id 是"当前位置"指针。
- 新编辑：在指针处追加子版本，指针前移，并清空该项目的 redo 记录。
- undo：指针后移到父版本，并把离开的版本压入 redo 栈。
- redo：从 redo 栈取回该版本，指针前移。

早期实现没有指针，undo() 每次都取最新 revision 的父本，于是第二次撤销仍返回同一个
EDL；且 redo 查询与 redo 栈清理都没有按 project_id 过滤，会跨项目取出别人的版本。
历史截断也直接删掉链上最旧的一行，留下悬空 parent_revision_id，走到第 50 步就报错。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.models.edl import EDL
from app.models.enums import OperationType
from app.models.revision import ProjectRevision
from app.persistence.models.project_models import EDLRevision, ProjectMeta, UndoRedoHistory
from app.utils.logging import get_logger

logger = get_logger("services.undo_redo_manager")

DEFAULT_HISTORY_DEPTH = 50


class UndoRedoError(RuntimeError):
    pass


class UndoRedoManager:
    def __init__(self, engine: AsyncEngine, max_depth: int = DEFAULT_HISTORY_DEPTH) -> None:
        self.engine = engine
        self.max_depth = max(max_depth, 1)
        # 单个 manager 内串行化同一项目的读-改-写，避免并发编辑分叉出同一个父本
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, project_id: str) -> asyncio.Lock:
        return self._locks.setdefault(project_id, asyncio.Lock())

    def _sessionmaker(self) -> async_sessionmaker:
        return async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_revision(
        self,
        project_id: str,
        edl: EDL,
        operation_type: OperationType,
        operation_detail: dict[str, Any] | None = None,
        parent_revision_id: UUID | None = None,
    ) -> ProjectRevision:
        project_uuid = self._as_uuid(project_id, "project_id")
        revision = ProjectRevision(
            project_id=project_uuid,
            parent_revision_id=parent_revision_id,
            edl_snapshot=json.dumps(edl.to_json(), ensure_ascii=False),
            operation_type=operation_type,
            operation_detail=operation_detail or {},
        )
        async with self._lock_for(str(project_id)):
            await self._insert_and_point(revision, str(project_id))
        logger.info(
            "revision_created",
            revision_id=str(revision.revision_id),
            operation_type=operation_type.value,
        )
        return revision

    async def save_revision(self, revision: ProjectRevision) -> None:
        """低级写入：仅落一行，不前移指针（供需要自行控制事务的调用方使用）。"""
        async with self._lock_for(str(revision.project_id)):
            await self._insert_and_point(revision, str(revision.project_id))

    async def _insert_and_point(
        self, revision: ProjectRevision, project_id: str
    ) -> None:
        async with self._sessionmaker()() as session:
            try:
                parent_id = revision.parent_revision_id
                if parent_id is None:
                    pointer = await self._read_pointer(session, project_id)
                    parent_id = pointer

                revision_number = 0
                if parent_id is not None:
                    parent = await session.get(EDLRevision, str(parent_id))
                    if parent is None:
                        raise UndoRedoError(f"父版本不存在: {parent_id}")
                    revision_number = (parent.revision_number or 0) + 1
                    revision.parent_revision_id = parent_id

                record = EDLRevision(
                    revision_id=str(revision.revision_id),
                    project_id=project_id,
                    parent_revision_id=(
                        str(revision.parent_revision_id) if revision.parent_revision_id else None
                    ),
                    edl_snapshot_json=revision.edl_snapshot,
                    operation_type=revision.operation_type.value,
                    operation_detail_json=json.dumps(
                        revision.operation_detail, ensure_ascii=False
                    ),
                    revision_number=revision_number,
                )
                session.add(record)
                await session.flush()

                await self._write_pointer(session, project_id, str(revision.revision_id))
                # 新编辑使既有 redo 失效
                await session.execute(
                    delete(UndoRedoHistory).where(UndoRedoHistory.project_id == project_id)
                )
                await self._truncate(session, project_id)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _truncate(self, session: Any, project_id: str) -> None:
        """保留最近 max_depth 个版本。

        被截断后新的链头 parent 置空，否则 load_revision 会顺着悬空引用报错。
        """
        result = await session.execute(
            select(EDLRevision)
            .where(EDLRevision.project_id == project_id)
            .order_by(EDLRevision.revision_number.desc())
        )
        rows = result.scalars().all()
        keep_count = max(self.max_depth, 1)
        if len(rows) <= keep_count:
            return

        keep = rows[:keep_count]
        dropped = rows[keep_count:]
        head = keep[-1]
        dropped_ids = [r.revision_id for r in dropped]
        await session.execute(
            delete(UndoRedoHistory).where(
                UndoRedoHistory.project_id == project_id,
                UndoRedoHistory.revision_id.in_(dropped_ids),
            )
        )
        await session.execute(
            delete(EDLRevision).where(EDLRevision.revision_id.in_(dropped_ids))
        )
        await session.execute(
            update(EDLRevision)
            .where(EDLRevision.revision_id == head.revision_id)
            .values(parent_revision_id=None)
        )
        logger.info(
            "revision_history_truncated",
            project_id=project_id,
            dropped=len(dropped_ids),
            head=head.revision_id,
        )

    async def load_revision(self, revision_id: UUID) -> ProjectRevision:
        async with self._sessionmaker()() as session:
            record = await session.get(EDLRevision, str(revision_id))
            if record is None:
                raise UndoRedoError(f"版本不存在: {revision_id}")
            return self._to_model(record)

    async def get_current_revision(self, project_id: str) -> ProjectRevision | None:
        async with self._sessionmaker()() as session:
            pointer = await self._read_pointer(session, project_id)
            if pointer is None:
                return None
            record = await session.get(EDLRevision, str(pointer))
            if record is None:
                return None
            return self._to_model(record)

    async def get_latest_revision(self, project_id: str) -> ProjectRevision | None:
        """兼容旧调用：等价于"当前位置"，按 revision_number 而非时间排序。"""
        return await self.get_current_revision(project_id)

    async def undo(self, project_id: str) -> EDL:
        async with self._lock_for(project_id), self._sessionmaker()() as session:
            try:
                pointer = await self._read_pointer(session, project_id)
                if pointer is None:
                    raise UndoRedoError("无可撤销操作")

                current = await session.get(EDLRevision, str(pointer))
                if current is None:
                    raise UndoRedoError(f"版本不存在: {pointer}")
                if not current.parent_revision_id:
                    raise UndoRedoError("无可撤销操作")

                parent = await session.get(EDLRevision, current.parent_revision_id)
                if parent is None:
                    raise UndoRedoError(f"父版本不存在: {current.parent_revision_id}")

                session.add(
                    UndoRedoHistory(
                        id=str(uuid4()),
                        project_id=project_id,
                        revision_id=str(current.revision_id),
                        direction="undo",
                    )
                )
                await self._write_pointer(session, project_id, str(parent.revision_id))
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        logger.info(
            "undo_executed",
            project_id=project_id,
            from_rev=str(current.revision_id),
            to_rev=str(parent.revision_id),
        )
        return EDL.from_json(json.loads(parent.edl_snapshot_json))

    async def redo(self, project_id: str) -> EDL:
        async with self._lock_for(project_id), self._sessionmaker()() as session:
            try:
                result = await session.execute(
                    select(UndoRedoHistory)
                    .where(
                        UndoRedoHistory.project_id == project_id,
                        UndoRedoHistory.direction == "undo",
                    )
                    .order_by(UndoRedoHistory.created_at.desc(), UndoRedoHistory.id.desc())
                )
                undo_entry = result.scalars().first()
                if undo_entry is None:
                    raise UndoRedoError("无可重做操作")

                record = await session.get(EDLRevision, undo_entry.revision_id)
                if record is None:
                    raise UndoRedoError("重做目标版本不存在")

                pointer = await self._read_pointer(session, project_id)
                if pointer and str(pointer) != str(record.parent_revision_id or ""):
                    # 撤销后又做了新编辑，redo 链已断
                    raise UndoRedoError("redo 链已因新编辑失效")

                await session.execute(
                    delete(UndoRedoHistory).where(UndoRedoHistory.id == undo_entry.id)
                )
                await self._write_pointer(session, project_id, str(record.revision_id))
                await session.commit()
            except Exception:
                await session.rollback()
                raise

        logger.info("redo_executed", project_id=project_id, revision_id=record.revision_id)
        return EDL.from_json(json.loads(record.edl_snapshot_json))

    async def can_undo(self, project_id: str) -> bool:
        async with self._sessionmaker()() as session:
            pointer = await self._read_pointer(session, project_id)
            if pointer is None:
                return False
            record = await session.get(EDLRevision, str(pointer))
            return bool(record and record.parent_revision_id)

    async def can_redo(self, project_id: str) -> bool:
        async with self._sessionmaker()() as session:
            result = await session.execute(
                select(UndoRedoHistory.id)
                .where(
                    UndoRedoHistory.project_id == project_id,
                    UndoRedoHistory.direction == "undo",
                )
                .limit(1)
            )
            return result.first() is not None

    async def clear_redo_stack(self, project_id: str) -> None:
        async with self._sessionmaker()() as session:
            await session.execute(
                delete(UndoRedoHistory).where(UndoRedoHistory.project_id == project_id)
            )
            await session.commit()

    async def apply_operation_with_revision(
        self,
        project_id: str,
        edl: EDL,
        operation_type: OperationType,
        operation_detail: dict[str, Any],
        apply_func: Callable[[EDL], EDL],
    ) -> tuple[EDL, ProjectRevision]:
        new_edl = apply_func(edl)
        revision = await self.create_revision(
            project_id=project_id,
            edl=new_edl,
            operation_type=operation_type,
            operation_detail=operation_detail,
        )
        return new_edl, revision

    async def _read_pointer(self, session: Any, project_id: str) -> UUID | None:
        meta = await session.get(ProjectMeta, project_id)
        if meta is None:
            result = await session.execute(
                select(ProjectMeta).where(ProjectMeta.id == project_id)
            )
            meta = result.scalars().first()
        if meta is None or not meta.current_revision_id:
            return None
        return self._as_uuid(meta.current_revision_id, "current_revision_id")

    @staticmethod
    async def _write_pointer(session: Any, project_id: str, revision_id: str) -> None:
        result = await session.execute(
            update(ProjectMeta)
            .where(ProjectMeta.id == project_id)
            .values(current_revision_id=revision_id)
        )
        if result.rowcount == 0:
            # 元信息行缺失时补一行，否则指针永远写不进去（undo 会一直"无可撤销"）
            session.add(
                ProjectMeta(
                    id=project_id,
                    name=f"project-{project_id[:8]}",
                    source_file_path="",
                    duration_us=0,
                    current_revision_id=revision_id,
                )
            )
            await session.flush()

    @staticmethod
    def _as_uuid(value: str, field: str) -> UUID:
        if isinstance(value, UUID):
            return value
        try:
            return UUID(str(value))
        except ValueError as exc:
            raise UndoRedoError(f"{field} 必须是 UUID，得到: {value!r}") from exc

    @staticmethod
    def _to_model(record: EDLRevision) -> ProjectRevision:
        return ProjectRevision(
            revision_id=UUID(record.revision_id),
            project_id=UndoRedoManager._as_uuid(record.project_id, "project_id"),
            parent_revision_id=(
                UUID(record.parent_revision_id) if record.parent_revision_id else None
            ),
            edl_snapshot=record.edl_snapshot_json,
            operation_type=OperationType(record.operation_type),
            operation_detail=json.loads(record.operation_detail_json),
            created_at=record.created_at,
            revision_number=record.revision_number,
        )
