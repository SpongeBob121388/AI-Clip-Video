"""Undo/Redo 管理器单元测试。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from app.models.edl import EDL, EDLSource, Suggestion, TimeBase
from app.models.enums import (
    DecisionSource,
    OperationType,
    SuggestionStatus,
    SuggestionType,
)
from app.persistence.initializer import init_project_db
from app.services.undo_redo_manager import UndoRedoError, UndoRedoManager


def _make_edl(duration_us: int = 10_000_000) -> EDL:
    return EDL(
        source=EDLSource(
            uri="test.mp4",
            duration_us=duration_us,
            time_base=TimeBase(num=1, den=90000),
        ),
    )


def _add_suggestion(edl: EDL, start: int, end: int) -> EDL:
    new_edl = edl.model_copy(deep=True)
    new_edl.suggestions.append(
        Suggestion(
            id=f"cut-{uuid4().hex[:8]}",
            source_start_us=start,
            source_end_us=end,
            suggestion_type=SuggestionType.SILENCE,
            status=SuggestionStatus.ACCEPTED,
            source=DecisionSource.MANUAL,
        )
    )
    return new_edl


@pytest.fixture
async def undo_redo_manager(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'undo_test.db'}",
        echo=False,
    )
    project_id = str(uuid4())
    await init_project_db(engine, project_id)
    manager = UndoRedoManager(engine, max_depth=50)
    yield manager, project_id
    await engine.dispose()


@pytest.mark.unit
class TestUndoRedo:
    async def test_create_and_load_revision(self, undo_redo_manager) -> None:
        manager, project_id = undo_redo_manager
        edl = _make_edl()
        revision = await manager.create_revision(
            project_id, edl, OperationType.IMPORT, {"file": "test.mp4"}
        )
        loaded = await manager.load_revision(revision.revision_id)
        assert str(loaded.revision_id) == str(revision.revision_id)

    async def test_undo_redo_roundtrip(self, undo_redo_manager) -> None:
        manager, project_id = undo_redo_manager
        edl0 = _make_edl()
        rev0 = await manager.create_revision(project_id, edl0, OperationType.IMPORT, {})

        edl1 = _add_suggestion(edl0, 1_000_000, 2_000_000)
        rev1 = await manager.create_revision(
            project_id, edl1, OperationType.MANUAL_EDIT, {"op": "add"}, rev0.revision_id
        )

        edl2 = _add_suggestion(edl1, 3_000_000, 4_000_000)
        await manager.create_revision(
            project_id, edl2, OperationType.MANUAL_EDIT, {"op": "add"}, rev1.revision_id
        )

        undone = await manager.undo(project_id)
        assert len(undone.suggestions) == 1

        redone = await manager.redo(project_id)
        assert len(redone.suggestions) == 2

    async def test_undo_no_history_raises(self, undo_redo_manager) -> None:
        manager, project_id = undo_redo_manager
        with pytest.raises(UndoRedoError, match="无可撤销"):
            await manager.undo(project_id)

    async def test_redo_empty_raises(self, undo_redo_manager) -> None:
        manager, project_id = undo_redo_manager
        with pytest.raises(UndoRedoError, match="无可重做"):
            await manager.redo(project_id)

    async def test_history_depth_limit(self, tmp_path) -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'depth_test.db'}",
            echo=False,
        )
        project_id = str(uuid4())
        await init_project_db(engine, project_id)
        manager = UndoRedoManager(engine, max_depth=3)

        for i in range(5):
            edl = _make_edl()
            await manager.create_revision(
                project_id, edl, OperationType.MANUAL_EDIT, {"index": i}
            )

        from sqlalchemy import select
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from app.persistence.models.project_models import EDLRevision

        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        async with sessionmaker() as session:
            result = await session.execute(
                select(EDLRevision).where(EDLRevision.project_id == project_id)
            )
            count = len(result.scalars().all())

        assert count <= 3
        await engine.dispose()

    async def test_apply_operation_with_revision(self, undo_redo_manager) -> None:
        manager, project_id = undo_redo_manager
        edl0 = _make_edl()
        await manager.create_revision(project_id, edl0, OperationType.IMPORT, {})

        new_edl, revision = await manager.apply_operation_with_revision(
            project_id,
            edl0,
            OperationType.MANUAL_EDIT,
            {"op": "add"},
            lambda e: _add_suggestion(e, 1_000_000, 2_000_000),
        )
        assert len(new_edl.suggestions) == 1
        assert revision.operation_type == OperationType.MANUAL_EDIT
