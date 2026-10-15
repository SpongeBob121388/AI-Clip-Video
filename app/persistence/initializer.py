"""数据库初始化 — 创建全局 app.db 和项目 project.db 表结构。"""

from __future__ import annotations

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.persistence.models.global_models import GlobalBase
from app.persistence.models.project_models import ProjectBase
from app.utils.logging import get_logger

logger = get_logger("persistence.initializer")


async def init_global_db(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(GlobalBase.metadata.create_all)
    logger.info("global_db_initialized")


async def init_project_db(engine: AsyncEngine, project_id: str, name: str = "") -> None:
    async with engine.begin() as conn:
        await conn.run_sync(ProjectBase.metadata.create_all)

    from app.persistence.models.project_models import ProjectMeta

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        existing = await session.execute(
            select(ProjectMeta).where(ProjectMeta.id == project_id)
        )
        if existing.scalar_one_or_none() is None:
            meta = ProjectMeta(
                id=project_id,
                name=name or f"Project-{project_id[:8]}",
                schema_version="1.0",
                source_file_path="",
                duration_us=0,
            )
            session.add(meta)
            await session.commit()

    logger.info("project_db_initialized", project_id=project_id)


async def get_project_schema_version(engine: AsyncEngine) -> str | None:
    from app.persistence.models.project_models import ProjectMeta

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        result = await session.execute(select(ProjectMeta.schema_version))
        return result.scalar_one_or_none()


async def verify_tables_exist(engine: AsyncEngine, expected_tables: list[str]) -> bool:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        )
        existing = {row[0] for row in result.fetchall()}
    return all(t in existing for t in expected_tables)


async def find_schema_drift(engine: AsyncEngine, base: object) -> dict[str, dict[str, list[str]]]:
    """比对 ORM 元数据与真实表结构，返回缺失/多余列。

    仅检查表名存在与否会让"模型加了列但迁移没跑"静默通过，直到某次查询报
    no such column 才在运行期炸掉。
    """
    def _inspect(sync_conn) -> dict[str, dict[str, list[str]]]:  # pragma: no cover - 由 run_sync 调用
        from sqlalchemy import inspect as sa_inspect

        inspector = sa_inspect(sync_conn)
        drift: dict[str, dict[str, list[str]]] = {}
        for table in base.metadata.tables.values():  # type: ignore[attr-defined]
            if not inspector.has_table(table.name):
                drift[table.name] = {"missing_table": [], "missing_columns": ["<table>"]}
                continue
            actual = {c["name"] for c in inspector.get_columns(table.name)}
            declared = {c.name for c in table.columns}
            missing = sorted(declared - actual)
            extra = sorted(actual - declared)
            if missing or extra:
                drift[table.name] = {"missing_columns": missing, "extra_columns": extra}
        return drift

    async with engine.connect() as conn:
        return await conn.run_sync(_inspect)


async def integrity_check(engine: AsyncEngine) -> bool:
    """PRAGMA integrity_check —— 迁移前后各跑一次，成本极低。"""
    async with engine.connect() as conn:
        result = await conn.execute(text("PRAGMA integrity_check"))
        rows = [row[0] for row in result.fetchall()]
    ok = rows == ["ok"]
    if not ok:
        logger.error("integrity_check_failed", result=str(rows)[:400])
    return ok
