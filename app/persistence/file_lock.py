"""项目锁 —— 防止同一项目被两个实例同时写入。

原实现用 `PRAGMA locking_mode=EXCLUSIVE` + `BEGIN IMMEDIATE; COMMIT`，但在
`async with` 退出时连接即关闭，锁随之释放，等于什么都没锁。

这里改为跨进程可靠的锁文件：O_CREAT|O_EXCL 原子创建（不存在 check-then-act 竞态），
内容含 PID 与心跳时间戳；持有者周期性 refresh；超过 TTL 未刷新即视为陈旧并可被接管，
因此进程崩溃留下的残留锁不会永久阻塞项目。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from app.utils.logging import get_logger

logger = get_logger("persistence.file_lock")

DEFAULT_TTL_SECONDS = 30.0
_LOCK_SUFFIX = ".lock"


class ProjectLockError(Exception):
    pass


class ProjectFileLock:
    def __init__(
        self,
        project_id: str,
        engine: AsyncEngine,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.project_id = project_id
        self.engine = engine
        self.ttl_seconds = ttl_seconds
        self._acquired = False
        self._lock_path = _lock_path_for(engine)

    @property
    def lock_file(self) -> Path:
        return self._lock_path

    @property
    def is_held(self) -> bool:
        return self._acquired

    async def acquire(self) -> None:
        try:
            await asyncio.to_thread(self._acquire_sync)
        except ProjectLockError:
            raise
        except OSError as exc:
            raise ProjectLockError(
                f"无法锁定项目 {self.project_id}：{exc}"
            ) from exc

    def _acquire_sync(self) -> None:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        for attempt in (1, 2):
            try:
                fd = os.open(
                    self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
                )
            except FileExistsError as exc:
                if attempt == 2 or not self._is_stale(self._lock_path):
                    owner = self._read_owner(self._lock_path)
                    raise ProjectLockError(
                        f"项目 {self.project_id} 已被占用（{owner or '未知进程'}）"
                    ) from exc
                logger.warning(
                    "stale_lock_stolen",
                    project_id=self.project_id,
                    age_seconds=round(_age_seconds(self._lock_path), 1),
                )
                self._unlink_quietly()
                continue

            try:
                os.write(
                    fd,
                    json.dumps(
                        {"pid": os.getpid(), "ts": time.time(), "project_id": self.project_id}
                    ).encode("utf-8"),
                )
            finally:
                os.close(fd)
            self._acquired = True
            logger.info("project_lock_acquired", project_id=self.project_id, pid=os.getpid())
            return

    def refresh(self) -> None:
        """心跳：只有持有者能刷新，避免接管者覆盖别人的锁。"""
        if not self._acquired:
            return
        try:
            self._lock_path.touch()
            payload = {"pid": os.getpid(), "ts": time.time(), "project_id": self.project_id}
            self._lock_path.write_text(json.dumps(payload), encoding="utf-8")
        except OSError as exc:  # pragma: no cover
            logger.warning("project_lock_refresh_failed", error=str(exc))

    async def release(self) -> None:
        if not self._acquired:
            return
        self._acquired = False
        if self._read_owner_pid() not in (None, os.getpid()):
            # 锁已被别人接管，不能删别人的锁
            logger.warning("project_lock_already_stolen", project_id=self.project_id)
            return
        await asyncio.to_thread(self._unlink_quietly)
        logger.info("project_lock_released", project_id=self.project_id)

    def _is_stale(self, path: Path) -> bool:
        try:
            return _age_seconds(path) > self.ttl_seconds
        except OSError:
            return True

    def _read_owner(self, path: Path) -> str:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return f"pid={data.get('pid')} age={_age_seconds(path):.0f}s"
        except (OSError, ValueError):
            return ""

    def _read_owner_pid(self) -> int | None:
        try:
            return int(json.loads(self._lock_path.read_text(encoding="utf-8")).get("pid"))
        except (OSError, ValueError, TypeError):
            return None

    def _unlink_quietly(self) -> None:
        try:
            self._lock_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # pragma: no cover
            logger.warning("project_lock_remove_failed", error=str(exc))

    async def __aenter__(self) -> ProjectFileLock:
        await self.acquire()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.release()


def _lock_path_for(engine: AsyncEngine) -> Path:
    db_path = Path(str(engine.url.database) or ":memory:")
    return db_path.with_suffix(db_path.suffix + _LOCK_SUFFIX)


def _age_seconds(path: Path) -> float:
    return max(0.0, time.time() - path.stat().st_mtime)


def check_lock_file(project_dir: Path) -> bool:
    """存在且未陈旧的锁才算被占用。"""
    for path in project_dir.glob(f"*{_LOCK_SUFFIX}"):
        try:
            if _age_seconds(path) <= DEFAULT_TTL_SECONDS:
                return True
        except OSError:
            continue
    return False


def create_lock_file(project_dir: Path, pid: int) -> Path:
    """原子创建项目目录级锁文件；持有且未过期则抛 ProjectLockError。"""
    project_dir.mkdir(parents=True, exist_ok=True)
    lock_file = project_dir / ".lock"
    payload = json.dumps({"pid": pid, "ts": time.time()})

    try:
        fd = os.open(lock_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            stale = _age_seconds(lock_file) > DEFAULT_TTL_SECONDS
        except OSError:
            stale = False
        if not stale:
            raise ProjectLockError(f"项目目录已锁定: {lock_file}") from None
        logger.warning("stale_project_lock_stolen", path=lock_file.name)
        fd = os.open(lock_file, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)

    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    return lock_file


def remove_lock_file(project_dir: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        (project_dir / ".lock").unlink()
