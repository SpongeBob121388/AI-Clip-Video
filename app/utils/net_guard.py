"""出站 URL 校验（SSRF 防护）。

设置端点会拿用户填写的 base_url 直接发请求，因此必须假定输入是敌意的：
- 只允许 http/https，且 http 仅限环回（本机 Ollama / LM Studio 之类）
- 禁止 userinfo（http://host#@evil/）、禁止端口越界
- 解析后的每个 IP 都不得是私网/链路本地/保留/组播地址，堵住
  http://169.254.169.254/ 这类云元数据探测与内网扫描

DNS 解析是阻塞调用，公开入口 `validate_endpoint_url_async` 走线程池。
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = {"http", "https"}
_MAX_PORT = 65535
_IPV6_ZONE = re.compile(r"%.*$")


class UnsafeURLError(ValueError):
    pass


def _ip_is_allowed(ip: ipaddress.IPv4Address | ipaddress.IPv6Address, scheme: str) -> bool:
    # 先判环回：Python 把 ::1 同时标记为 is_reserved，顺序反了会误拒本机地址
    if ip.is_loopback:
        return True
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    if ip.is_link_local:
        return False
    if scheme == "http":
        # 明文只允许指向本机；任何可路由地址都必须走 TLS
        return False
    return not ip.is_private


def _literal_host(host: str) -> str:
    """去掉 IPv6 的方括号与 zone id。"""
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return _IPV6_ZONE.sub("", host)


def validate_endpoint_url(url: str, *, resolve_dns: bool = True) -> str:
    """校验并返回规范化的端点 URL；不安全则抛 UnsafeURLError。"""
    if not url or not url.strip():
        raise UnsafeURLError("URL 为空")

    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"只允许 {sorted(_ALLOWED_SCHEMES)} 协议，得到 {scheme or '无协议'!r}")

    if parts.username or parts.password:
        raise UnsafeURLError("URL 不允许包含用户名/密码段")
    if "@" in parts.netloc:
        raise UnsafeURLError("URL 不允许包含 '@'（可能的 userinfo 绕过）")

    host = _literal_host(parts.hostname or "")
    if not host:
        raise UnsafeURLError("URL 缺少主机名")
    if host.endswith("."):
        host = host[:-1]

    try:
        port = parts.port
    except ValueError as exc:
        raise UnsafeURLError("URL 端口非法") from exc
    if port is not None and not 1 <= port <= _MAX_PORT:
        raise UnsafeURLError("URL 端口越界")
    if port == 0:
        raise UnsafeURLError("URL 端口非法")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if not _ip_is_allowed(literal, scheme):
            raise UnsafeURLError("目标地址不属于允许范围")
        return url.strip().rstrip("/")

    if scheme == "http" and not _is_loopback_name(host):
        raise UnsafeURLError("明文 http 只允许指向本机地址")

    if resolve_dns:
        try:
            infos = socket.getaddrinfo(host, port or (443 if scheme == "https" else 80),
                                       type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise UnsafeURLError(f"主机无法解析: {host}") from exc
        if not infos:
            raise UnsafeURLError(f"主机无法解析: {host}")
        for info in infos:
            addr = ipaddress.ip_address(info[4][0])
            if not _ip_is_allowed(addr, scheme):
                raise UnsafeURLError(f"{host} 解析到不允许的地址")

    return url.strip().rstrip("/")


def _is_loopback_name(host: str) -> bool:
    return host.lower() in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(
        ".localhost"
    )


async def validate_endpoint_url_async(url: str) -> str:
    return await asyncio.to_thread(validate_endpoint_url, url)
