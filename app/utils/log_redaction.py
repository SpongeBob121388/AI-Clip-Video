"""日志脱敏。

两条独立路径：
1. `redact_processor` —— structlog 管线钩子，按事件字段名判定机密字段并整体遮蔽，
   再对字符串值跑模式匹配（`configure_logging` 已装载，见 utils/logging.py）。
2. `redact_value` / `redact_dict` —— 供把异常文本回显给用户前的显式调用。

字段名判定优先于模式判定：真实密钥（HeyGen/D-ID 为不定长 hex）无法靠形状识别。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# 保留前 8 字符便于人工比对，其余遮蔽
_API_KEY_PATTERN = re.compile(r"(sk-[A-Za-z0-9]{8})[A-Za-z0-9]*", re.IGNORECASE)
_BEARER_PATTERN = re.compile(r"((?:Bearer|Basic)\s+[A-Za-z0-9+/=]{8})[A-Za-z0-9+/=]*", re.IGNORECASE)
_URL_CREDENTIALS = re.compile(r"://[^/@:\s]+:[^/@\s]+@", re.IGNORECASE)
# C:\Users\<name>、\\?\C:\Users\... 以及 /home/<name>
_USER_PATH_PATTERN = re.compile(
    r"(?P<drive>(?:\\\\\?\\)?[A-Za-z]:\\Users\\)[^\\\"']+", re.IGNORECASE
)
_HOME_PATH_PATTERN = re.compile(r"(?P<base>/home/)[^/\"']+")
_QUERY_TOKEN_PATTERN = re.compile(
    r"([?&](?:api[_-]?key|access_token|token|key)=)[^&\s\"]+", re.IGNORECASE
)

SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "pwd",
    "token",
    "credential",
    "authorization",
    "access_key",
}

_REDACTED = "***REDACTED***"


def is_sensitive_key(key: str) -> bool:
    key_lower = str(key).lower()
    return any(s in key_lower for s in SENSITIVE_KEYS)


def redact_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    redacted = _API_KEY_PATTERN.sub(r"\1****", value)
    redacted = _BEARER_PATTERN.sub(r"\1****", redacted)
    redacted = _QUERY_TOKEN_PATTERN.sub(r"\1<redacted>", redacted)
    redacted = _URL_CREDENTIALS.sub("://<redacted>@", redacted)
    redacted = _USER_PATH_PATTERN.sub(lambda m: f"{m.group('drive')}<redacted>", redacted)
    redacted = _HOME_PATH_PATTERN.sub(lambda m: f"{m.group('base')}<redacted>", redacted)
    return redacted


def redact_dict(data: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in data.items():
        if is_sensitive_key(str(key)):
            result[key] = _REDACTED
        elif isinstance(value, Mapping):
            result[key] = redact_dict(value)
        elif isinstance(value, (list, tuple)):
            result[key] = [
                redact_dict(item) if isinstance(item, Mapping) else redact_value(item)
                for item in value
            ]
        else:
            result[key] = redact_value(value)
    return result


def redact_processor(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """structlog processor：就地脱敏事件字典。"""
    for key in list(event_dict):
        value = event_dict[key]
        if is_sensitive_key(key):
            event_dict[key] = _REDACTED
        elif isinstance(value, str):
            event_dict[key] = redact_value(value)
        elif isinstance(value, Mapping):
            event_dict[key] = redact_dict(value)
    return event_dict
