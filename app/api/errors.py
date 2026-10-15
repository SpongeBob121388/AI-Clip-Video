"""错误响应的安全序列化。

Starlette 默认的校验错误响应用 `ensure_ascii=False` 编码。客户端提交的坏字节经
`json.loads` 的 surrogateescape 解码会变成孤立代理项，一旦回显到错误详情里就会让
`json.dumps().encode("utf-8")` 抛 UnicodeEncodeError，把本该 422 的坏请求变成 500。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

_SCALARS = (int, float, bool)


def _escape(value: str) -> str:
    return value.encode("utf-8", errors="backslashreplace").decode("utf-8", errors="replace")


def sanitize_for_json(value: Any) -> Any:
    """递归清理字符串，保证结果一定能用 UTF-8 编码。"""
    if isinstance(value, str):
        return _escape(value)
    if isinstance(value, dict):
        return {_escape(str(k)): sanitize_for_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize_for_json(item) for item in value]
    if value is None or isinstance(value, _SCALARS):
        return value
    return _escape(str(value))


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": sanitize_for_json(exc.errors())})
