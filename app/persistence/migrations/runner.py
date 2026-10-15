"""Schema 版本迁移框架。

三条硬约束：
1. 迁移体与版本号写入必须在同一连接、同一事务里，否则失败时结构改了但版本没落库。
2. 版本号只认 project_meta.schema_version 这一处真源；写入用"先 UPDATE，未命中则
   INSERT"的 upsert —— 旧实现在元信息行尚未创建时 UPDATE 影响 0 行，版本号永远记不上，
   于是每次启动都重跑迁移。
3. 备份必须覆盖 WAL 的真实状态：先 wal_checkpoint 再连同 -wal/-shm 一起复制，
   否则恢复出来的是主文件与陈旧 WAL 错配的空洞数据库。
"""

from __future__ import annotations

import asyncio
import inspect
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from app.utils.logging import get_logger

logger = get_logger("persistence.migrations")

#: 迁移函数在目标连接上执行，与版本号写入共享同一事务
MigrationFunc = Callable[[AsyncConnection], Any]


def parse_version(value: str) -> tuple[int, ...]:
    """按数字段比较版本，避免字符串比较把 '10.0' 判给 '2.0' 之前。"""
    numbers: list[int] = []
    for chunk in str(value).strip().split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        numbers.append(int(digits) if digits else 0)
    return tuple(numbers) if numbers else (0,)


def _cmp(a: str, b: str) -> int:
    va, vb = parse_version(a), parse_version(b)
    width = max(len(va), len(vb))
    va += (0,) * (width - len(va))
    vb += (0,) * (width - len(vb))
    return (va > vb) - (va < vb)


class MigrationError(RuntimeError):
    pass


class MigrationRunner:
    def __init__(self) -> None:
        self._migrations: list[tuple[str, str, MigrationFunc]] = []
        self._lock = threading.Lock()

    def register(self, from_version: str, to_version: str, func: MigrationFunc) -> None:
        with self._lock:
            for existing_from, existing_to, _ in self._migrations:
                if (existing_from, existing_to) == (from_version, to_version):
                    raise MigrationError(
                        f"迁移 {from_version} -> {existing_to} 重复注册"
                    )
                if existing_to == to_version and existing_from != from_version:
                    raise MigrationError(
                        f"目标版本 {to_version} 已有其他迁移路径（分叉不被支持）"
                    )
            self._migrations.append((from_version, to_version, func))
        logger.info(
            "migration_registered",
            from_version=from_version,
            to_version=to_version,
        )

    async def _ensure_meta_row(self, conn: AsyncConnection, version: str) -> None:
        """把 schema_version 落到 project_meta 单行上（UPDATE 未命中则 INSERT）。"""
        from app.persistence.models.project_models import ProjectBase

        await conn.run_sync(ProjectBase.metadata.create_all)

        result = await conn.execute(
            text("UPDATE project_meta SET schema_version = :v"), {"v": version}
        )
        if result.rowcount:
            return

        db_path = Path(str(conn.engine.url.database))  # type: ignore[attr-defined]
        await conn.execute(
            text(
                "INSERT INTO project_meta (id, name, schema_version, source_file_path, duration_us)"
                " VALUES (:id, :name, :v, '', 0)"
            ),
            {
                "id": db_path.parent.name,
                "name": f"project-{db_path.parent.name[:8]}",
                "v": version,
            },
        )
        logger.info("project_meta_row_created", version=version)

    async def get_current_version(self, engine: AsyncEngine) -> str:
        async with engine.connect() as conn:
            try:
                result = await conn.execute(
                    text("SELECT schema_version FROM project_meta LIMIT 1")
                )
                row = result.fetchone()
                return row[0] if row and row[0] else "0.0"
            except Exception:
                return "0.0"

    async def run(self, engine: AsyncEngine, target_version: str) -> None:
        current = await self.get_current_version(engine)

        if _cmp(current, target_version) > 0:
            raise MigrationError(
                f"数据库结构版本 {current} 高于本程序支持的 {target_version}，拒绝降级运行"
            )
        if current == target_version:
            logger.info("migration_skipped", current=current)
            return

        applicable = [
            (fv, tv, func)
            for fv, tv, func in self._migrations
            if _cmp(fv, current) >= 0 and _cmp(tv, target_version) <= 0
        ]
        applicable.sort(key=lambda item: parse_version(item[0]))

        if not applicable:
            raise MigrationError(
                f"从 {current} 到 {target_version} 没有可用的迁移路径"
            )

        await asyncio.to_thread(self._backup, engine)

        for from_v, to_v, func in applicable:
            logger.info("migration_running", from_version=from_v, to_version=to_v)
            try:
                async with engine.begin() as conn:
                    outcome = func(conn)
                    if inspect.isawaitable(outcome):
                        await outcome
                    # 同一事务内落版本，失败即一起回滚
                    await self._ensure_meta_row(conn, to_v)
            except Exception as exc:
                logger.error(
                    "migration_step_failed", from_version=from_v, to_version=to_v, error=str(exc)
                )
                raise MigrationError(f"迁移 {from_v} -> {to_v} 失败: {exc}") from exc
            logger.info("migration_completed", to_version=to_v)

        await asyncio.to_thread(self._drop_backup, engine)
        logger.info("all_migrations_completed", target_version=target_version)

    def _backup(self, engine: AsyncEngine) -> Path | None:
        """把 WAL 排空后整体复制主文件，备份点与真实数据一致。"""
        db_path = Path(str(engine.url.database))
        if not db_path.exists():
            return None
        import sqlite3

        try:
            # 用 sync 驱动做一次 checkpoint；不能与 async 连接同时持有写锁时忽略
            raw = sqlite3.connect(db_path)
            try:
                raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                raw.close()
        except sqlite3.Error as exc:
            logger.warning("wal_checkpoint_failed", error=str(exc))

        backup = db_path.with_suffix(".db.bak")
        shutil.copy2(db_path, backup)
        for suffix in ("-wal", "-shm"):
            side = Path(str(db_path) + suffix)
            if side.exists():
                shutil.copy2(side, Path(str(backup) + suffix))
        logger.info("backup_created", backup_path=str(backup))
        return backup

    def _drop_backup(self, engine: AsyncEngine) -> None:
        db_path = Path(str(engine.url.database))
        backup = db_path.with_suffix(".db.bak")
        for candidate in (backup, *[_suffixed(backup, s) for s in ("-wal", "-shm")]):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:  # pragma: no cover
                logger.warning("backup_cleanup_failed", path=candidate.name)

    async def restore_backup(self, engine: AsyncEngine) -> bool:
        db_path = Path(str(engine.url.database))
        backup = db_path.with_suffix(".db.bak")
        if not backup.exists():
            return False
        shutil.copy2(backup, db_path)
        # 主文件与 WAL 必须成对恢复，单独恢复主文件会留下错配
        for suffix in ("-wal", "-shm"):
            side = Path(str(db_path) + suffix)
            backup_side = _suffixed(backup, suffix)
            if side.exists() and not backup_side.exists():
                side.unlink()
            elif backup_side.exists():
                shutil.copy2(backup_side, side)
        logger.info("backup_restored")
        return True


def _suffixed(path: Path, suffix: str) -> Path:
    return Path(str(path) + suffix)


migration_runner = MigrationRunner()


def register_migration(from_version: str, to_version: str, func: MigrationFunc) -> None:
    migration_runner.register(from_version, to_version, func)
