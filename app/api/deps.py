"""本机 API 访问控制。

威胁模型：FastAPI 只监听环回口，但同机任意进程、以及浏览器里任意网页都可能访问它。
因此需要两道防线：

1. 启动期随机令牌。REST 走 `Authorization: Bearer`，WebSocket 走 `?token=`。
   令牌来自 `ACV_AUTH_TOKEN`，否则读/写 `data_dir/api.token`（供桌面外壳读取）。
2. Host / Origin 校验。抵御 DNS rebinding（令攻击域名解析到 127.0.0.1 后变成同源）
   与跨站 WebSocket 劫持；同时是写操作真正的 CSRF 防线。
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Header, HTTPException, WebSocket, status

from app.config import get_settings
from app.utils.logging import get_logger

logger = get_logger("api.deps")

_TOKEN_ENV = "ACV_AUTH_TOKEN"
# 令牌文件不存在时由首个实例创建；并发创建失败则采用已存在的令牌
_TOKEN_FILE_MODE = 0o600


class AuthError(HTTPException):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(status_code=status_code, detail=detail)


def load_or_create_token() -> str:
    """返回当前生效的访问令牌，必要时创建新令牌并落盘。"""
    settings = get_settings()
    if settings.auth_token:
        return settings.auth_token

    env_token = os.environ.get(_TOKEN_ENV)
    if env_token and env_token.strip():
        return env_token.strip()

    path = settings.token_file
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        existing = _read_token_file(path)
        if existing:
            return existing

    token = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, _TOKEN_FILE_MODE)
    except FileExistsError:
        # 另一实例刚写好，用它的令牌，否则会互相踢掉
        existing = _read_token_file(path)
        if existing:
            return existing
        raise
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    return token


def _read_token_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        logger.warning("token_file_unreadable")
        return ""


def _matches(supplied: str | None) -> bool:
    if not supplied:
        return False
    return hmac.compare_digest(supplied.encode("utf-8"), load_or_create_token().encode("utf-8"))


def _bearer_of(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None


def _origin_allowed(origin: str) -> bool:
    if origin in ("null", ""):
        return False
    settings = get_settings()
    if origin in settings.cors_origins:
        return True
    host = urlparse(origin).hostname
    return host in {"localhost", "127.0.0.1", "::1", "tauri.localhost"}


def check_host(host: str | None) -> None:
    """Host 头白名单：环回名 + 配置里的显式允许项（测试客户端用 http://test）。"""
    if not host:
        raise AuthError(status.HTTP_400_BAD_REQUEST, "缺少 Host 头")
    name = host.split("]")[0].split(":")[0].strip().lower()
    allowed = {h.lower() for h in get_settings().allowed_hosts} | {"test", "testserver"}
    if name not in allowed:
        logger.warning("rejected_host", host=host)
        raise AuthError(status.HTTP_400_BAD_REQUEST, "Host 头不被允许")


async def require_auth(
    authorization: str | None = Header(default=None),
    x_acv_token: str | None = Header(default=None),
    host: str | None = Header(default=None),
    origin: str | None = Header(default=None),
) -> None:
    """所有 REST 端点的安全依赖：令牌 + Host + Origin。"""
    check_host(host)

    if origin is not None and not _origin_allowed(origin):
        logger.warning("rejected_origin", origin=origin)
        raise AuthError(status.HTTP_403_FORBIDDEN, "来源不被允许")

    if not _matches(x_acv_token or _bearer_of(authorization)):
        raise AuthError(
            status.HTTP_401_UNAUTHORIZED,
            "缺少或错误的访问令牌",
        )


async def websocket_authenticated(websocket: WebSocket) -> bool:
    """WebSocket 握手校验；失败时已关闭连接，调用方直接返回。"""
    check_host(websocket.headers.get("host"))

    origin = websocket.headers.get("origin")
    if origin is not None and not _origin_allowed(origin):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return False

    if not _matches(websocket.query_params.get("token")):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return False

    return True
