"""API 路由配置 — 所有 REST API 端点注册。

前缀：/api/v1
认证：每进程随机 bearer token（见 app/api/deps.py）
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.avatar import router as avatar_router
from app.api.jobs import router as jobs_router
from app.api.media import router as media_router
from app.api.media import stream_router as media_stream_router
from app.api.pipeline import router as pipeline_router
from app.api.projects import router as projects_router
from app.api.settings import router as settings_router


def create_api_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    router.include_router(media_router)
    # 取流端点不带 bearer 依赖：票据本身即凭证
    router.include_router(media_stream_router)
    router.include_router(projects_router)
    router.include_router(pipeline_router)
    router.include_router(jobs_router)
    router.include_router(settings_router)
    router.include_router(avatar_router)
    return router
