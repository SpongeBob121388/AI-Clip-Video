"""日志配置：控制台 + 轮转文件双写。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from app.utils.logging import configure_logging, get_logger


@pytest.mark.unit
class TestFileLogging:
    def test_events_reach_the_log_file(self, tmp_path: Path) -> None:
        target = tmp_path / "logs" / "app.log"
        configure_logging(level="INFO", fmt="json", log_file=target)
        get_logger("test").info("打包日志检查", stage="export")

        assert target.is_file()
        text = target.read_text(encoding="utf-8")
        assert "打包日志检查" in text
        assert '"stage": "export"' in text

    def test_secrets_are_redacted_in_the_file(self, tmp_path: Path) -> None:
        target = tmp_path / "logs" / "app.log"
        configure_logging(level="INFO", fmt="json", log_file=target)
        get_logger("test").warning("credential_probe", api_key="sk-supersecret-value")

        text = target.read_text(encoding="utf-8")
        assert "sk-supersecret-value" not in text
        assert "api_key" in text

    def test_unwritable_log_path_degrades_to_console_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 目录创建失败时必须照常启动，否则用户改坏数据目录就再也打不开应用
        blocked = tmp_path / "blocked.log"
        blocked.mkdir()
        monkeypatch.setattr(Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))
        configure_logging(level="INFO", fmt="json", log_file=blocked)
        get_logger("test").info("仍然可用")

    def test_windowed_sidecar_without_stderr_still_logs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PyInstaller windowed 模式没有 stderr，日志必须继续写进文件。"""
        target = tmp_path / "logs" / "app.log"
        monkeypatch.setattr(sys, "stderr", None, raising=False)
        configure_logging(level="INFO", fmt="json", log_file=target)
        get_logger("test").info("无控制台事件", job_id="abc")

        assert "无控制台事件" in target.read_text(encoding="utf-8")
