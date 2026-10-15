"""SQLAlchemy 2.x 引擎配置 — 全局 app.db 和项目 project.db。

SQLite WAL 模式确保断电恢复后数据零丢失。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.utils.logging import get_logger

logger = get_logger("persistence.database")

_global_engine: AsyncEngine | None = None
_project_engines: dict[str, AsyncEngine] = {}


def _validate_project_id(project_id: str) -> str:
    """project_id 会被拼进文件路径，必须是 UUID 字符串。"""
    try:
        return str(UUID(str(project_id)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"非法项目 ID（需为 UUID）: {project_id!r}") from exc


def _get_project_db_path(project_id: str) -> Path:
    settings = get_settings()
    return settings.projects_dir / _validate_project_id(project_id) / "project.db"


def _apply_sqlite_pragmas(engine: Engine | AsyncEngine) -> None:
    @event.listens_for(engine.sync_engine if isinstance(engine, AsyncEngine) else engine, "connect")
    def _set_pragmas(dbapi_conn: Any, _connection_record: Any) -> None:
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.execute("PRAGMA synchronous = NORMAL")
        cursor.close()


async def get_global_engine() -> AsyncEngine:
    global _global_engine
    if _global_engine is not None:
        return _global_engine

    settings = get_settings()
    settings.ensure_dirs()
    db_url = f"sqlite+aiosqlite:///{settings.global_db_path}"

    _global_engine = create_async_engine(
        db_url,
        echo=False,
        pool_pre_ping=True,
    )
    _apply_sqlite_pragmas(_global_engine)
    logger.info("global_engine_created", db_path=str(settings.global_db_path))
    return _global_engine


async def get_project_engine(project_id: str) -> AsyncEngine:
    if project_id in _project_engines:
        return _project_engines[project_id]

    db_path = _get_project_db_path(project_id)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_url = f"sqlite+aiosqlite:///{db_path}"

    engine = create_async_engine(
        db_url,
        echo=False,
        pool_pre_ping=True,
    )
    _apply_sqlite_pragmas(engine)
    _project_engines[project_id] = engine
    await _ensure_project_schema(engine, project_id)
    logger.info("project_engine_created", project_id=project_id, db_path=str(db_path))
    return engine


async def _ensure_project_schema(engine: AsyncEngine, project_id: str) -> None:
    """打开老项目时补建后加的新表。

    项目库是在它被创建那一刻的元数据下建好的，只在 create_project 时跑 create_all 意味着
    升级后新增的表永远不存在，第一次读写就是 no such table。create_all 幂等，只补缺失的表。
    加**列**不走这条路：得登记迁移。
    """
    from app.persistence.initializer import find_schema_drift
    from app.persistence.models.project_models import ProjectBase

    async with engine.begin() as conn:
        await conn.run_sync(ProjectBase.metadata.create_all)
    drift = await find_schema_drift(engine, ProjectBase)
    if drift:
        logger.warning("project_schema_drift", project_id=project_id, drift=str(drift)[:500])


async def get_global_sessionmaker() -> async_sessionmaker:
    """异步环境下必须 await 引擎创建；旧实现用 run_until_complete 会在运行中的
    事件循环里抛 RuntimeError。"""
    engine = await get_global_engine()
    return async_sessionmaker(engine, expire_on_commit=False)


async def close_global_engine() -> None:
    global _global_engine
    if _global_engine is not None:
        await _global_engine.dispose()
        _global_engine = None
        logger.info("global_engine_closed")


async def close_project_engine(project_id: str) -> None:
    if project_id in _project_engines:
        await _project_engines[project_id].dispose()
        del _project_engines[project_id]
        logger.info("project_engine_closed", project_id=project_id)


async def close_all_project_engines() -> None:
    for project_id in list(_project_engines.keys()):
        await close_project_engine(project_id)


async def delete_project_db(project_id: str) -> None:
    await close_project_engine(project_id)
    db_path = _get_project_db_path(project_id)
    for suffix in ("", "-wal", "-shm", "-journal", ".lock"):
        p = Path(str(db_path) + suffix)
        try:
            if p.exists():
                os.remove(p)
        except OSError as exc:
            # 删不掉要报出来，否则会留下不可见的孤儿文件
            logger.error("project_db_remove_failed", path=p.name, error=str(exc))
    logger.info("project_db_deleted", project_id=project_id)


async def verify_wal_mode(engine: AsyncEngine) -> bool:
    async with engine.connect() as conn:
        result = await conn.execute(text("PRAGMA journal_mode"))
        mode = result.scalar()
        return mode == "wal"
