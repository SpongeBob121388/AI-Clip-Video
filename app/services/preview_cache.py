"""预览缓存 — 基于 RenderPlan Hash 的 LRU 缓存。

缓存位置：%LOCALAPPDATA%/auto_clip_video/preview_cache/
缓存键：RenderPlan Hash
缓存失效：EDL 变更或预设变更时 Hash 自然不同
"""

from __future__ import annotations

import os
import shutil
from contextlib import suppress
from pathlib import Path

from app.config import get_settings
from app.utils.logging import get_logger

logger = get_logger("services.preview_cache")

_MAX_CACHE_SIZE_BYTES = 5 * 1024 * 1024 * 1024


def _default_cache_dir() -> Path:
    return get_settings().preview_cache_dir


def _quiet_unlink(path: Path) -> bool:
    """删除缓存条目。

    <video> 还在播这个文件时，Windows 会以共享冲突拒绝删除；此时跳过这条即可，
    不能让一次已经渲染成功的预览因为清理失败而报错。
    """
    try:
        path.unlink()
        return True
    except OSError as exc:
        logger.warning("preview_cache_delete_failed", file=path.name, error=str(exc))
        return False


def _size_or_zero(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _mtime_or_zero(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


class PreviewCache:
    """预览渲染 LRU 缓存。"""

    def __init__(
        self,
        cache_dir: Path | None = None,
        max_size_bytes: int = _MAX_CACHE_SIZE_BYTES,
    ) -> None:
        self._cache_dir = cache_dir or _default_cache_dir()
        self._max_size_bytes = max_size_bytes
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def get(self, plan_hash: str) -> Path | None:
        """缓存命中返回文件路径，未命中返回 None。"""
        cache_file = self._cache_dir / f"{plan_hash}.mp4"
        if cache_file.exists():
            with suppress(OSError):
                cache_file.touch()
            logger.info("preview_cache_hit", plan_hash=plan_hash)
            return cache_file
        logger.info("preview_cache_miss", plan_hash=plan_hash)
        return None

    def staging_path(self, plan_hash: str) -> Path:
        """渲染直接写这里，成功后原子改名为正式条目。

        半截文件如果直接叫 `{hash}.mp4`，下一次预览会把没写完的视频当缓存命中播放。
        """
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        return self._cache_dir / f"{plan_hash}.part.mp4"

    def promote(self, plan_hash: str) -> Path:
        staging = self.staging_path(plan_hash)
        if not staging.exists():
            raise FileNotFoundError(f"预览产物缺失: {staging.name}")
        final = self._cache_dir / f"{plan_hash}.mp4"
        os.replace(staging, final)
        logger.info("preview_cache_stored", plan_hash=plan_hash, size=final.stat().st_size)
        self._evict_if_needed()
        return final

    def discard(self, plan_hash: str) -> None:
        try:
            self.staging_path(plan_hash).unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - 清理失败不应改变结果
            logger.warning("preview_staging_cleanup_failed", error=str(exc))

    def put(self, plan_hash: str, source_path: Path) -> Path:
        """将渲染结果存入缓存。"""
        cache_file = self._cache_dir / f"{plan_hash}.mp4"
        shutil.copy2(source_path, cache_file)
        logger.info("preview_cache_stored", plan_hash=plan_hash, size=cache_file.stat().st_size)
        self._evict_if_needed()
        return cache_file

    def invalidate(self, plan_hash: str) -> None:
        """手动失效指定缓存。"""
        cache_file = self._cache_dir / f"{plan_hash}.mp4"
        if cache_file.exists() and _quiet_unlink(cache_file):
            logger.info("preview_cache_invalidated", plan_hash=plan_hash)

    def clear(self) -> None:
        """清空所有缓存；删不掉的（播放器还占着）跳过。"""
        for path in self._cache_dir.glob("*.mp4"):
            _quiet_unlink(path)
        logger.info("preview_cache_cleared")

    def total_size(self) -> int:
        """当前缓存总大小（字节）。"""
        return sum(_size_or_zero(path) for path in self._cache_dir.glob("*.mp4"))

    def _evict_if_needed(self) -> None:
        """LRU 清理：超过最大大小时删除最旧文件。"""
        files = sorted(self._cache_dir.glob("*.mp4"), key=_mtime_or_zero)
        total = sum(_size_or_zero(f) for f in files)
        while total > self._max_size_bytes and files:
            oldest = files.pop(0)
            freed = _size_or_zero(oldest)
            total -= freed
            if _quiet_unlink(oldest):
                logger.info("preview_cache_evicted", file=oldest.name, freed_bytes=freed)
