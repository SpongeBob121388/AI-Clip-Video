"""配置加载：数据目录必须规范化为绝对路径。

起因是相对 ACV_DATA_DIR 会让预览缓存产物变成相对路径，被输出路径校验拒绝，
预览任务直接失败（PathValidationError: 输出路径必须为绝对路径）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, reset_settings
from app.utils.path_validator import validate_output_path


@pytest.mark.unit
class TestDataDirNormalization:
    def _settings(self, value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
        monkeypatch.setenv("ACV_DATA_DIR", value)
        monkeypatch.chdir(tmp_path)
        reset_settings()
        return Settings()

    def test_relative_dir_becomes_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = self._settings("relative/data", tmp_path, monkeypatch)
        assert settings.data_dir == tmp_path / "relative" / "data"
        assert settings.data_dir.is_absolute()

    def test_derived_paths_follow_absolute_data_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = self._settings("relative/data", tmp_path, monkeypatch)
        for path in (
            settings.projects_dir,
            settings.global_db_path,
            settings.upload_dir,
            settings.render_work_dir,
            settings.preview_cache_dir,
            settings.export_dir,
            settings.token_file,
        ):
            assert path.is_absolute(), path

    def test_preview_output_passes_path_validation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = self._settings("relative/data", tmp_path, monkeypatch)
        target = settings.preview_cache_dir / "cache-part.part.mp4"
        assert validate_output_path(str(target)) == target

    def test_dotdot_in_data_dir_is_collapsed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        settings = self._settings("data/../other", tmp_path, monkeypatch)
        assert ".." not in str(settings.data_dir)
        assert settings.data_dir == tmp_path / "other"

    def test_absolute_dir_is_kept(self, tmp_path: Path) -> None:
        assert Settings(data_dir=tmp_path / "x").data_dir == tmp_path / "x"
