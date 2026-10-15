"""应用配置管理。

使用 pydantic-settings 从环境变量和 .env 文件加载配置。

监听地址固定为环回口：本机 API 不做多机暴露，跨进程访问控制由启动期令牌负责。
数据目录默认落在用户可写的应用数据目录，避免安装到 Program Files 后无法写入。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal

from pydantic import Field, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_HOST_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _default_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local"))
        return base / "auto_clip_video"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "auto_clip_video"
    return Path(
        os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    ) / "auto_clip_video"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="ACV_",
        extra="ignore",
    )

    app_name: str = Field(default="auto-clip-video", description="应用名称")
    app_version: str = Field(default="0.1.0", description="应用版本")
    environment: Literal["development", "staging", "production"] = Field(
        default="production", description="运行环境"
    )

    host: str = Field(default="127.0.0.1", description="FastAPI 监听地址（仅限环回）")
    port: int = Field(default=8765, ge=1024, le=65535, description="FastAPI 监听端口")

    log_level: Literal["DEBUG", "INFO", "WARN", "ERROR"] = Field(
        default="INFO", description="日志级别"
    )
    log_format: Literal["json", "console"] = Field(
        default="json", description="日志输出格式"
    )

    data_dir: Path = Field(default_factory=_default_data_dir, description="数据目录")
    projects_dir: Path = Field(
        default_factory=lambda: _default_data_dir() / "projects", description="项目存储目录"
    )
    global_db_path: Path = Field(
        default_factory=lambda: _default_data_dir() / "app.db", description="全局数据库路径"
    )

    auth_token: str | None = Field(
        default=None, description="API 访问令牌；为空时从 data_dir/api.token 读写"
    )
    cors_origins: list[str] = Field(
        default_factory=lambda: [
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "tauri://localhost",
            "http://tauri.localhost",
            "https://tauri.localhost",
        ],
        description="允许的跨源来源",
    )
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1", "localhost", "::1"],
        description="允许的 Host 头（抵御 DNS rebinding）",
    )

    ffmpeg_path: str = Field(default="ffmpeg", description="FFmpeg 可执行文件路径")
    ffprobe_path: str = Field(default="ffprobe", description="ffprobe 可执行文件路径")
    asr_hf_endpoint: str | None = Field(
        default=None,
        description="Whisper 模型下载源；huggingface.co 不可达时可设为镜像（如 https://hf-mirror.com）",
    )
    credential_backend: Literal["auto", "in_memory"] = Field(
        default="auto", description="凭证后端；测试用 in_memory 避免写入真实凭据库"
    )

    max_render_duration_minutes: int = Field(default=15, ge=1, le=240)
    render_timeout_seconds: int = Field(default=900, ge=10, le=7200)
    max_upload_bytes: int = Field(default=4 * 1024**3, ge=1024, description="上传字节上限")

    @model_validator(mode="after")
    def _derive_paths(self) -> Settings:
        # 只显式设置了 data_dir 时，子路径跟随 data_dir，避免一半在 %LOCALAPPDATA%
        # 一半在 ./data 的分裂状态。
        if "data_dir" in self.model_fields_set:
            if "projects_dir" not in self.model_fields_set:
                self.projects_dir = self.data_dir / "projects"
            if "global_db_path" not in self.model_fields_set:
                self.global_db_path = self.data_dir / "app.db"
        if self.host not in _HOST_LOOPBACK:
            raise ValueError(
                f"host 只允许环回地址 {list(_HOST_LOOPBACK)}，当前为 {self.host!r}"
            )
        return self

    @field_validator("data_dir", "projects_dir", "global_db_path", mode="before")
    @classmethod
    def _absolute_path(cls, value: object) -> object:
        # 渲染输出与取流白名单都拒绝相对路径，sqlite URL 也按绝对路径拼接，
        # 因此目录在配置阶段就定死成绝对路径，不受启动时工作目录影响。
        # 用 abspath 而非 resolve：只做规范化，不跟随符号链接，避免真实目录与记录的路径不一致。
        if isinstance(value, (str, Path)):
            return Path(os.path.abspath(Path(value).expanduser()))
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_dev(self) -> bool:
        return self.environment == "development"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_frozen(self) -> bool:
        """PyInstaller 打包后运行：不允许 uvicorn reload，也不依赖源码树。"""
        return getattr(sys, "frozen", False)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def allow_docs(self) -> bool:
        return self.is_dev and not self.is_frozen

    @computed_field  # type: ignore[prop-decorator]
    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "uploads"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def render_work_dir(self) -> Path:
        return self.data_dir / "render"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def preview_cache_dir(self) -> Path:
        return self.data_dir / "preview_cache"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def export_dir(self) -> Path:
        """浏览器/无原生对话框环境下的默认导出位置。"""
        return self.data_dir / "exports"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def token_file(self) -> Path:
        return self.data_dir / "api.token"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def log_file(self) -> Path:
        """无控制台的后台进程只能靠这份日志排障。"""
        return self.data_dir / "logs" / "app.log"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def port_file(self) -> Path:
        """桌面外壳不带参数启动 sidecar，只能从这里读实际监听端口。"""
        return self.data_dir / "runtime.port"

    def ensure_dirs(self) -> None:
        for path in (
            self.data_dir,
            self.projects_dir,
            self.global_db_path.parent,
            self.upload_dir,
            self.render_work_dir,
            self.preview_cache_dir,
            self.export_dir,
            self.log_file.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    global _settings
    _settings = None
