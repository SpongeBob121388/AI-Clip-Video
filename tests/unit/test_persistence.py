"""持久化层单元测试。"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.persistence.database import (
    verify_wal_mode,
)
from app.persistence.file_lock import ProjectFileLock, ProjectLockError
from app.persistence.initializer import (
    get_project_schema_version,
    init_global_db,
    init_project_db,
    verify_tables_exist,
)
from app.persistence.migrations.runner import MigrationRunner
from app.persistence.models.global_models import AppSetting, ProjectRecord
from app.persistence.models.project_models import (
    EDLRevision,
    SuggestionRecord,
)


@pytest.fixture
async def global_engine(tmp_path):
    from app.config import get_settings, reset_settings

    reset_settings()
    import app.config as cfg

    cfg._settings = get_settings()
    cfg._settings.data_dir = tmp_path / "data"
    cfg._settings.projects_dir = tmp_path / "data" / "projects"
    cfg._settings.global_db_path = tmp_path / "data" / "app.db"
    cfg._settings.ensure_dirs()

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'test_global.db'}",
        echo=False,
    )
    await init_global_db(engine)
    yield engine
    await engine.dispose()
    reset_settings()


@pytest.fixture
async def project_engine(tmp_path):
    db_path = tmp_path / "test_project.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        echo=False,
    )
    project_id = str(uuid4())
    await init_project_db(engine, project_id, "Test Project")
    yield engine, project_id
    await engine.dispose()


@pytest.mark.unit
class TestGlobalDatabase:
    async def test_global_db_tables_created(self, global_engine) -> None:
        expected = [
            "projects",
            "app_settings",
            "cost_records",
            "avatar_profiles",
            "recent_projects",
        ]
        assert await verify_tables_exist(global_engine, expected)

    async def test_global_db_wal_mode(self, tmp_path) -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'wal_test.db'}",
            echo=False,
        )
        from sqlalchemy import event

        @event.listens_for(engine.sync_engine, "connect")
        def set_pragma(dbapi_conn, _):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.close()

        await init_global_db(engine)
        assert await verify_wal_mode(engine)
        await engine.dispose()

    async def test_app_setting_crud(self, global_engine) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        sessionmaker = async_sessionmaker(global_engine, expire_on_commit=False)
        async with sessionmaker() as session:
            setting = AppSetting(key="test_key", value="test_value", category="test")
            session.add(setting)
            await session.commit()

        async with sessionmaker() as session:
            result = await session.execute(
                select(AppSetting).where(AppSetting.key == "test_key")
            )
            found = result.scalar_one()
            assert found.value == "test_value"
            assert found.category == "test"

    async def test_project_record_crud(self, global_engine) -> None:
        from sqlalchemy.ext.asyncio import async_sessionmaker

        sessionmaker = async_sessionmaker(global_engine, expire_on_commit=False)
        pid = str(uuid4())
        async with sessionmaker() as session:
            record = ProjectRecord(
                id=pid,
                name="Test Project",
                db_path=f"/tmp/{pid}/project.db",
            )
            session.add(record)
            await session.commit()

        async with sessionmaker() as session:
            result = await session.execute(
                select(ProjectRecord).where(ProjectRecord.id == pid)
            )
            found = result.scalar_one()
            assert found.name == "Test Project"


@pytest.mark.unit
class TestProjectDatabase:
    async def test_project_db_tables_created(self, project_engine) -> None:
        engine, _ = project_engine
        expected = [
            "project_meta",
            "media_assets",
            "transcripts",
            "silence_candidates",
            "suggestions",
            "edl_revisions",
            "render_plans",
            "render_jobs",
            "undo_redo_history",
            "export_records",
        ]
        assert await verify_tables_exist(engine, expected)

    async def test_project_meta_initialized(self, project_engine) -> None:
        engine, project_id = project_engine
        version = await get_project_schema_version(engine)
        assert version == "1.0"

    async def test_suggestion_record_crud(self, project_engine) -> None:
        engine, _ = project_engine
        from sqlalchemy.ext.asyncio import async_sessionmaker

        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        sid = str(uuid4())
        async with sessionmaker() as session:
            suggestion = SuggestionRecord(
                id=sid,
                start_us=1000,
                end_us=5000,
                type="silence",
                decision="uncertain",
                source="llm",
            )
            session.add(suggestion)
            await session.commit()

        async with sessionmaker() as session:
            result = await session.execute(
                select(SuggestionRecord).where(SuggestionRecord.id == sid)
            )
            found = result.scalar_one()
            assert found.start_us == 1000
            assert found.decision == "uncertain"

    async def test_edl_revision_crud(self, project_engine) -> None:
        engine, project_id = project_engine
        from sqlalchemy.ext.asyncio import async_sessionmaker

        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        rid = str(uuid4())
        async with sessionmaker() as session:
            rev = EDLRevision(
                revision_id=rid,
                project_id=project_id,
                edl_snapshot_json='{"version": 1}',
                operation_type="import",
            )
            session.add(rev)
            await session.commit()

        async with sessionmaker() as session:
            result = await session.execute(
                select(EDLRevision).where(EDLRevision.revision_id == rid)
            )
            found = result.scalar_one()
            assert found.operation_type == "import"

    async def test_indexes_exist(self, project_engine) -> None:
        engine, _ = project_engine
        async with engine.connect() as conn:
            result = await conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")
            )
            indexes = {row[0] for row in result.fetchall()}
        assert "idx_suggestions_decision" in indexes
        assert "idx_suggestions_source" in indexes
        assert "idx_edl_revisions_parent" in indexes
        assert "idx_render_jobs_status" in indexes


@pytest.mark.unit
class TestMigrationRunner:
    async def test_migration_noop_same_version(self, project_engine) -> None:
        engine, _ = project_engine
        runner = MigrationRunner()
        version = await runner.get_current_version(engine)
        assert version == "1.0"

    async def test_migration_skip_when_already_at_target(self, project_engine) -> None:
        engine, _ = project_engine
        runner = MigrationRunner()
        await runner.run(engine, target_version="1.0")
        version = await runner.get_current_version(engine)
        assert version == "1.0"


@pytest.mark.unit
class TestProjectFileLock:
    async def test_lock_acquire_release(self, tmp_path) -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'lock_test.db'}",
            echo=False,
        )
        project_id = str(uuid4())
        await init_project_db(engine, project_id)

        lock = ProjectFileLock(project_id, engine)
        await lock.acquire()
        assert lock._acquired
        await lock.release()
        assert not lock._acquired
        await engine.dispose()

    async def test_lock_context_manager(self, tmp_path) -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'lock_ctx_test.db'}",
            echo=False,
        )
        project_id = str(uuid4())
        await init_project_db(engine, project_id)

        async with ProjectFileLock(project_id, engine):
            pass

        await engine.dispose()

    async def test_second_holder_is_rejected(self, tmp_path) -> None:
        """锁必须真的互斥 —— 旧实现释放即关闭连接，第二实例照样能进。"""
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'lock_conflict.db'}",
            echo=False,
        )
        project_id = str(uuid4())
        await init_project_db(engine, project_id)

        first = ProjectFileLock(project_id, engine)
        await first.acquire()
        second = ProjectFileLock(project_id, engine)
        with pytest.raises(ProjectLockError, match="已被占用"):
            await second.acquire()
        assert second.is_held is False

        await first.release()
        await second.acquire()
        assert second.is_held is True
        await second.release()
        await engine.dispose()

    async def test_stale_lock_is_stealable(self, tmp_path) -> None:
        """崩溃残留的锁在超过 TTL 后应可被接管，而不是永久锁死项目。"""
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'lock_stale.db'}",
            echo=False,
        )
        project_id = str(uuid4())
        await init_project_db(engine, project_id)

        lock = ProjectFileLock(project_id, engine, ttl_seconds=30.0)
        await lock.acquire()
        # 伪造陈旧：把锁文件时间戳推到 TTL 之后
        path = lock.lock_file
        old = path.stat().st_mtime - 60
        os.utime(path, (old, old))

        other = ProjectFileLock(project_id, engine, ttl_seconds=30.0)
        await other.acquire()
        assert other.is_held is True
        await other.release()
        await engine.dispose()

    async def test_release_does_not_delete_others_lock(self, tmp_path) -> None:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'lock_owner.db'}",
            echo=False,
        )
        project_id = str(uuid4())
        await init_project_db(engine, project_id)

        first = ProjectFileLock(project_id, engine)
        await first.acquire()
        # 模拟锁已被别的进程接管
        first.lock_file.write_text('{"pid": 999999, "ts": 0}', encoding="utf-8")
        await first.release()

        assert first.lock_file.exists(), "接管者持有的锁不能被原持有者删除"
        await engine.dispose()
