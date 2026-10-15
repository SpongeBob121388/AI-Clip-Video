"""FastAPI 应用入口。

启动本机 orchestrator，提供 REST API 和 WebSocket 端点。
监听地址由配置固定为环回口，所有业务端点要求访问令牌（见 app/api/deps.py）。
"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, closing, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.deps import load_or_create_token
from app.api.errors import register_exception_handlers
from app.api.router import create_api_router
from app.api.websocket import router as ws_router
from app.config import get_settings
from app.utils.logging import configure_logging, get_logger

_ALLOWED_METHODS = ["GET", "POST", "PUT", "DELETE"]
_ALLOWED_HEADERS = ["Authorization", "Content-Type", "X-ACV-Token"]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from app.persistence.database import close_all_project_engines, close_global_engine, get_global_engine
    from app.persistence.initializer import init_global_db

    settings = get_settings()
    settings.ensure_dirs()
    configure_logging(level=settings.log_level, fmt=settings.log_format, log_file=settings.log_file)
    logger = get_logger("app")
    # 提前落盘令牌文件，桌面外壳启动 webview 前即可读取（不记录令牌本身）
    load_or_create_token()
    # 外壳以无参数方式启动 sidecar，端口与令牌一样只能落盘交接
    settings.port_file.write_text(str(settings.port), encoding="utf-8")

    global_engine = await get_global_engine()
    await init_global_db(global_engine)

    logger.info(
        "application_starting",
        app_name=settings.app_name,
        version=settings.app_version,
        environment=settings.environment,
        host=settings.host,
        port=settings.port,
        frozen=settings.is_frozen,
    )
    try:
        yield
    finally:
        # 后台渲染/分析任务必须先行取消，否则子进程会在引擎关闭后继续写库
        from app.providers.registry import shutdown_providers
        from app.services.job_runner import get_job_runner, reset_job_runner

        await get_job_runner().shutdown(cancel=True)
        reset_job_runner()
        await shutdown_providers()
        await close_all_project_engines()
        await close_global_engine()
        # 端口文件留着会让外壳以为后端还活着
        with suppress(OSError):
            settings.port_file.unlink(missing_ok=True)
        logger.info("application_shutting_down")


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
        docs_url="/docs" if settings.allow_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.allow_docs else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=_ALLOWED_METHODS,
        allow_headers=_ALLOWED_HEADERS,
    )
    register_exception_handlers(app)

    @app.get("/")
    async def root() -> dict[str, str]:
        return {"name": settings.app_name, "version": settings.app_version}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "app": settings.app_name, "version": settings.app_version}

    app.include_router(create_api_router())
    app.include_router(ws_router)

    return app


app = create_app()


def resolve_bind_port(host: str, preferred: int) -> int:
    """优先用配置端口；已被占用时退回系统分配的空闲端口。

    端口是靠 runtime.port 交接给桌面外壳的，换个端口前端照样连得上。不退回的话，
    8765 被残留的开发服务或第二个实例占着时 uvicorn 绑定失败会直接 sys.exit，
    而 windowed 产物没有控制台，用户只看到窗口空转。
    """
    # 探测时不设 SO_REUSEADDR：Windows 下它允许多个套接字绑同一端口，结果不可信
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with closing(socket.socket(family, socket.SOCK_STREAM)) as probe:
        try:
            probe.bind((host, preferred))
            return preferred
        except OSError:
            pass
    with closing(socket.socket(family, socket.SOCK_STREAM)) as fallback:
        fallback.bind((host, 0))
        return int(fallback.getsockname()[1])


def run() -> None:
    import uvicorn

    settings = get_settings()
    # 端口冲突要留痕，所以先把日志通道建起来（lifespan 里会按同样配置再配一次）
    settings.ensure_dirs()
    configure_logging(
        level=settings.log_level, fmt=settings.log_format, log_file=settings.log_file
    )
    logger = get_logger("app")

    try:
        port = resolve_bind_port(settings.host, settings.port)
    except OSError as exc:
        # windowed 产物里未捕获异常不会出现在任何地方，只能靠这条日志排障
        logger.error("port_bind_failed", host=settings.host, port=settings.port, error=str(exc))
        raise
    if port != settings.port:
        logger.warning("preferred_port_busy", requested=settings.port, fallback=port)
        settings.port = port

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        # 冻结产物里没有源码可 reload
        reload=settings.is_dev and not settings.is_frozen,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()
