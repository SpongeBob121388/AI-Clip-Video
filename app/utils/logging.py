"""结构化日志配置。

使用 structlog 输出 JSON 结构化日志，支持 trace_id 链路追踪。
日志经 stdlib 的 root logger 分发：控制台一份、数据目录下的轮转文件一份，
两条通道共用同一个脱敏处理器，因此文件里也不会出现密钥或用户名。
日志不保存音视频内容，仅记录 job_id/stage/耗时/错误码。
"""

from __future__ import annotations

import logging
import sys
import uuid
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import structlog

from app.utils.log_redaction import redact_processor

_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
_job_id_var: ContextVar[str] = ContextVar("job_id", default="")

_MAX_LOG_BYTES = 5 * 1024 * 1024
_LOG_BACKUPS = 3


def _add_context_vars(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    trace_id = _trace_id_var.get()
    job_id = _job_id_var.get()
    if trace_id:
        event_dict["trace_id"] = trace_id
    if job_id:
        event_dict["job_id"] = job_id
    return event_dict


def _is_interactive() -> bool:
    return bool(getattr(sys.stderr, "isatty", lambda: False)())


class _NullStream:
    """PyInstaller windowed 模式下 sys.stderr 可能是 None，写日志不能因此崩掉。"""

    def write(self, text: str) -> int:
        return len(text)

    def flush(self) -> None:
        return None


def _console_stream() -> Any:
    return sys.stderr if sys.stderr is not None else _NullStream()


def _base_processors() -> list[Any]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _add_context_vars,
        structlog.processors.StackInfoRenderer(),
        # 放在异常文本已展开之后，回溯行里的密钥/用户名同样被遮蔽
        redact_processor,
    ]


def _renderer(fmt: str) -> Any:
    if fmt == "json":
        return structlog.processors.JSONRenderer(ensure_ascii=False)
    # 只有交互式终端才上色：同一份输出还要写进日志文件，ANSI 转义码会污染文件
    return structlog.dev.ConsoleRenderer(colors=_is_interactive())


def _open_file_handler(path: Path, formatter: logging.Formatter) -> logging.Handler | None:
    """打开轮转日志文件；失败就退回纯控制台，不能让应用因为日志而启动不了。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            path,
            maxBytes=_MAX_LOG_BYTES,
            backupCount=_LOG_BACKUPS,
            encoding="utf-8",
        )
    except OSError:
        return None
    handler.setFormatter(formatter)
    return handler


def configure_logging(
    level: str = "INFO",
    fmt: str = "json",
    log_file: Path | str | None = None,
) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    foreign = _base_processors()

    structlog.configure(
        processors=[
            *foreign,
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        # 模块导入期就有 get_logger() 调用，缓存会让运行中的重新配置完全失效
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, _renderer(fmt)],
        foreign_pre_chain=foreign,
    )

    console = logging.StreamHandler(_console_stream())
    console.setFormatter(formatter)
    handlers: list[logging.Handler] = [console]
    if log_file is not None:
        file_handler = _open_file_handler(Path(log_file), formatter)
        if file_handler is not None:
            handlers.append(file_handler)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(log_level)


def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)


def set_trace_id(trace_id: str | None = None) -> str:
    tid = trace_id or uuid.uuid4().hex[:16]
    _trace_id_var.set(tid)
    structlog.contextvars.bind_contextvars(trace_id=tid)
    return tid


def set_job_id(job_id: str) -> None:
    _job_id_var.set(job_id)
    structlog.contextvars.bind_contextvars(job_id=job_id)


def clear_context() -> None:
    _trace_id_var.set("")
    _job_id_var.set("")
    structlog.contextvars.clear_contextvars()
