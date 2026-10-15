"""预览缓存与导出辅助功能测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.preview_cache import PreviewCache
from app.services.render_service import RenderService


@pytest.mark.unit
class TestPreviewCache:
    def test_miss_on_empty_cache(self, tmp_path: Path) -> None:
        cache = PreviewCache(cache_dir=tmp_path / "cache", max_size_bytes=1024 * 1024)
        assert cache.get("nonexistent") is None

    def test_put_and_get(self, tmp_path: Path) -> None:
        cache = PreviewCache(cache_dir=tmp_path / "cache", max_size_bytes=1024 * 1024)
        source = tmp_path / "source.mp4"
        source.write_bytes(b"fake video content")
        cache.put("hash123", source)
        result = cache.get("hash123")
        assert result is not None
        assert result.exists()

    def test_invalidate(self, tmp_path: Path) -> None:
        cache = PreviewCache(cache_dir=tmp_path / "cache", max_size_bytes=1024 * 1024)
        source = tmp_path / "source.mp4"
        source.write_bytes(b"content")
        cache.put("hash456", source)
        assert cache.get("hash456") is not None
        cache.invalidate("hash456")
        assert cache.get("hash456") is None

    def test_clear(self, tmp_path: Path) -> None:
        cache = PreviewCache(cache_dir=tmp_path / "cache", max_size_bytes=1024 * 1024)
        source = tmp_path / "source.mp4"
        source.write_bytes(b"content")
        cache.put("h1", source)
        cache.put("h2", source)
        cache.clear()
        assert cache.get("h1") is None
        assert cache.get("h2") is None

    def test_lru_eviction(self, tmp_path: Path) -> None:
        cache = PreviewCache(cache_dir=tmp_path / "cache", max_size_bytes=15)
        source = tmp_path / "source.mp4"
        source.write_bytes(b"0123456789")
        cache.put("old", source)
        import time
        time.sleep(0.05)
        cache.put("new", source)
        assert cache.total_size() == 10

    def test_total_size(self, tmp_path: Path) -> None:
        cache = PreviewCache(cache_dir=tmp_path / "cache", max_size_bytes=1024 * 1024)
        source = tmp_path / "source.mp4"
        source.write_bytes(b"0123456789")
        cache.put("h1", source)
        assert cache.total_size() == 10

    def test_clear_survives_locked_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """<video> 还在播时 Windows 会拒绝删除，清空缓存不能因此抛错。"""
        cache = PreviewCache(cache_dir=tmp_path / "cache", max_size_bytes=1024 * 1024)
        source = tmp_path / "source.mp4"
        source.write_bytes(b"0123456789")
        cache.put("h1", source)

        def locked(self: Path, *args: object, **kwargs: object) -> None:
            raise PermissionError(13, "另一个程序正在使用此文件")

        monkeypatch.setattr(Path, "unlink", locked)
        cache.clear()
        assert cache.get("h1") is not None

    def test_eviction_survives_locked_files(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cache = PreviewCache(cache_dir=tmp_path / "cache", max_size_bytes=15)
        source = tmp_path / "source.mp4"
        source.write_bytes(b"0123456789")

        def locked(self: Path, *args: object, **kwargs: object) -> None:
            raise PermissionError(13, "另一个程序正在使用此文件")

        monkeypatch.setattr(Path, "unlink", locked)
        cache.put("a", source)
        cache.put("b", source)
        assert cache.total_size() == 20

    def test_invalidate_survives_locked_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        cache = PreviewCache(cache_dir=tmp_path / "cache", max_size_bytes=1024 * 1024)
        source = tmp_path / "source.mp4"
        source.write_bytes(b"0123456789")
        cache.put("h1", source)
        monkeypatch.setattr(Path, "unlink", lambda self, *a, **k: (_ for _ in ()).throw(PermissionError(13)))
        cache.invalidate("h1")
        assert cache.get("h1") is not None


@pytest.mark.unit
class TestExportHelpers:
    def test_default_filename(self) -> None:
        name = RenderService.generate_default_filename("video.mp4")
        assert name.startswith("video_edited_")
        assert name.endswith(".mp4")

    def test_metadata_args(self) -> None:
        args = RenderService.build_metadata_args("proj1", "hash123", "v1.0.0")
        assert "-metadata" in args
        assert "project_id=proj1" in args
        assert "renderplan_hash=hash123" in args
        assert "software_version=v1.0.0" in args
