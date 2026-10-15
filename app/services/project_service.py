"""项目服务 —— 全局库记录（app.db）+ 每项目独立 SQLite（project.db）。

取代 `app/api/projects.py` 里的进程内字典：那意味着重启后所有项目消失，
而且多实例之间互相看不见。

两库分工：app.db 只回答"有哪些项目"，project.db 存该项目全部业务数据。
因此列表接口不需要打开每个项目的引擎之外的任何东西（aiosqlite 默认 NullPool，
引擎缓存不会常驻连接文件句柄）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.config import get_settings
from app.models.edl import EDL, EDLSource
from app.models.enums import OperationType
from app.persistence.database import (
    delete_project_db,
    get_global_engine,
    get_project_engine,
)
from app.persistence.initializer import init_project_db
from app.persistence.models.global_models import ProjectRecord
from app.persistence.models.project_models import EDLRevision, ProjectMeta
from app.services.undo_redo_manager import UndoRedoManager
from app.utils.logging import get_logger

logger = get_logger("services.project_service")


class ProjectNotFoundError(LookupError):
    pass


class ProjectServiceError(RuntimeError):
    pass


async def _global_sessionmaker() -> async_sessionmaker:
    engine = await get_global_engine()
    return async_sessionmaker(engine, expire_on_commit=False)


class ProjectService:
    """项目生命周期与 EDL 读写入口。

    不缓存 UndoRedoManager：它按引擎构造且内部锁表以项目为键，跨项目复用同一实例
    会让两个项目的写串行化在同一把锁表上，收益为零。
    """

    def __init__(self) -> None:
        self._managers: dict[str, UndoRedoManager] = {}

    async def engine_for(self, project_id: str) -> AsyncEngine:
        return await get_project_engine(project_id)

    async def undo_redo(self, project_id: str) -> UndoRedoManager:
        engine = await self.engine_for(project_id)
        cached = self._managers.get(project_id)
        if cached is not None and cached.engine is engine:
            return cached
        manager = UndoRedoManager(engine)
        self._managers[project_id] = manager
        return manager

    async def create_project(self, name: str, source_path: str | None = None) -> dict[str, Any]:
        project_id = str(uuid4())
        settings = get_settings()
        project_dir = settings.projects_dir / project_id
        project_dir.mkdir(parents=True, exist_ok=True)
        db_path = project_dir / "project.db"

        engine = await get_project_engine(project_id)
        await init_project_db(engine, project_id, name)

        maker = await _global_sessionmaker()
        async with maker() as session:
            session.add(
                ProjectRecord(id=project_id, name=name[:255], db_path=str(db_path))
            )
            await session.commit()

        if source_path:
            await self.attach_source(project_id, source_path)

        logger.info("project_created", project_id=project_id, name=name)
        return await self.get_project(project_id)

    async def list_projects(self, limit: int = 50) -> list[dict[str, Any]]:
        maker = await _global_sessionmaker()
        async with maker() as session:
            result = await session.execute(
                select(ProjectRecord)
                .order_by(ProjectRecord.updated_at.desc())
                .limit(max(1, min(limit, 200)))
            )
            records = result.scalars().all()

        items: list[dict[str, Any]] = []
        for record in records:
            items.append(
                {
                    "project_id": record.id,
                    "name": record.name,
                    "source_path": await self._source_path(record.id),
                    "created_at": record.created_at,
                    "updated_at": record.updated_at,
                }
            )
        return items

    async def get_project(self, project_id: str) -> dict[str, Any]:
        maker = await _global_sessionmaker()
        async with maker() as session:
            record = await session.get(ProjectRecord, project_id)
        if record is None:
            raise ProjectNotFoundError(project_id)

        meta = await self._meta(project_id)
        return {
            "project_id": record.id,
            "name": record.name,
            "source_path": (meta.source_file_path or None) if meta else None,
            "duration_us": meta.duration_us if meta else 0,
            "current_revision_id": meta.current_revision_id if meta else None,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    async def delete_project(self, project_id: str) -> None:
        await self.get_project(project_id)

        maker = await _global_sessionmaker()
        async with maker() as session:
            await session.execute(delete(ProjectRecord).where(ProjectRecord.id == project_id))
            await session.commit()

        self._managers.pop(project_id, None)
        await delete_project_db(project_id)

        project_dir = get_settings().projects_dir / project_id
        if project_dir.is_dir():
            # 残留的是上传副本/临时产物；删除失败要报出来，否则磁盘静默累积
            leftovers = [p for p in project_dir.iterdir() if p.is_file()]
            for item in leftovers:
                try:
                    item.unlink()
                except OSError as exc:
                    logger.warning(
                        "project_file_cleanup_failed", file_name=item.name, error=str(exc)
                    )
            try:
                project_dir.rmdir()
            except OSError as exc:
                logger.warning(
                    "project_dir_cleanup_failed", project_id=project_id, error=str(exc)
                )
        logger.info("project_deleted", project_id=project_id)

    async def attach_source(
        self,
        project_id: str,
        source_path: str,
        duration_us: int = 0,
        rotation: int = 0,
        is_vfr: bool | None = None,
        time_base: str | None = None,
    ) -> None:
        """记录源文件路径与探测结果；0/None 表示沿用已有值，不抹掉旧的探测结论。"""
        engine = await self.engine_for(project_id)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            meta = await session.get(ProjectMeta, project_id)
            if meta is None:
                raise ProjectNotFoundError(project_id)
            meta.source_file_path = source_path
            if duration_us > 0:
                meta.duration_us = duration_us
            if rotation in (0, 90, 180, 270):
                meta.rotation = rotation
            if is_vfr is not None:
                meta.is_vfr = is_vfr
            if time_base:
                meta.time_base = time_base
            await session.commit()

    async def source_path(self, project_id: str) -> str:
        meta = await self._meta(project_id)
        if meta is None or not meta.source_file_path:
            raise ProjectServiceError("项目尚未绑定源文件，请先上传或探测媒体")
        return meta.source_file_path

    async def ensure_initial_edl(
        self,
        project_id: str,
        source_path: str,
        duration_us: int,
        rotation: int = 0,
    ) -> tuple[EDL, UUID]:
        """首次分析时建立基线 EDL 与修订记录；已有当前修订则直接复用。"""
        manager = await self.undo_redo(project_id)
        existing = await manager.get_current_revision(project_id)
        if existing is not None:
            edl = EDL.from_json(json.loads(existing.edl_snapshot))
            if edl.source.duration_us != duration_us and duration_us > 0:
                # 媒体被重新探测过（时长变了），旧 EDL 的边界不再可信
                raise ProjectServiceError(
                    "源时长与已有剪辑记录不一致，请删除项目后重新分析"
                )
            return edl, existing.revision_id

        edl = EDL(
            project_id=UUID(project_id),
            source=EDLSource(
                uri=source_path,
                duration_us=duration_us,
                rotation=rotation,
            ),
        )
        revision = await manager.create_revision(
            project_id=project_id,
            edl=edl,
            operation_type=OperationType.IMPORT,
            operation_detail={"file": Path(source_path).name, "duration_us": duration_us},
        )
        await self.attach_source(project_id, source_path, duration_us, rotation)
        return edl, revision.revision_id

    async def load_current_edl(self, project_id: str) -> tuple[EDL, UUID]:
        manager = await self.undo_redo(project_id)
        current = await manager.get_current_revision(project_id)
        if current is None:
            raise ProjectServiceError("项目尚无剪辑记录，请先完成一次分析")
        edl = EDL.from_json(json.loads(current.edl_snapshot))
        return edl, current.revision_id

    async def load_edl(self, project_id: str, revision_id: UUID) -> EDL:
        manager = await self.undo_redo(project_id)
        revision = await manager.load_revision(revision_id)
        if str(revision.project_id) != str(project_id):
            raise ProjectServiceError("版本不属于该项目")
        return EDL.from_json(json.loads(revision.edl_snapshot))

    async def save_edl(
        self,
        project_id: str,
        edl: EDL,
        operation_type: OperationType,
        detail: dict[str, Any] | None = None,
    ) -> UUID:
        manager = await self.undo_redo(project_id)
        revision = await manager.create_revision(
            project_id=project_id,
            edl=edl,
            operation_type=operation_type,
            operation_detail=detail or {},
        )
        return revision.revision_id

    async def list_revisions(self, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
        engine = await self.engine_for(project_id)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            result = await session.execute(
                select(EDLRevision)
                .where(EDLRevision.project_id == project_id)
                .order_by(EDLRevision.revision_number.desc())
                .limit(max(1, min(limit, 200)))
            )
            return [
                {
                    "revision_id": row.revision_id,
                    "revision_number": row.revision_number,
                    "parent_revision_id": row.parent_revision_id,
                    "operation_type": row.operation_type,
                    "created_at": row.created_at,
                }
                for row in result.scalars().all()
            ]

    async def _meta(self, project_id: str) -> ProjectMeta | None:
        engine = await self.engine_for(project_id)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            return await session.get(ProjectMeta, project_id)

    async def _source_path(self, project_id: str) -> str | None:
        meta = await self._meta(project_id)
        if meta is None or not meta.source_file_path:
            return None
        return meta.source_file_path


_service: ProjectService | None = None


def get_project_service() -> ProjectService:
    global _service
    if _service is None:
        _service = ProjectService()
    return _service


def reset_project_service() -> None:
    global _service
    _service = None
