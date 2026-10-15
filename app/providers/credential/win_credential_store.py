"""Windows Credential Manager Adapter。

使用 keyring 库（Windows DPAPI / Credential Manager 后端）存储凭证。
凭证存储于 OS 级密钥存储，不写入 SQLite/日志/配置文件。

keyring 缺失或解析到非持久后端时，退化为进程内存储并对外暴露 `is_persistent`，
由调用方（设置端点）明确告知用户凭证不会被持久化 —— 静默假装保存成功是最坏结果。
"""

from __future__ import annotations

from typing import Any

from app.providers.credential.protocol import CREDENTIAL_KEYS
from app.utils.logging import get_logger

logger = get_logger("providers.credential.win_credential_store")

_SERVICE_NAME = "AutoClipVideo"


class CredentialStoreError(RuntimeError):
    pass


class WinCredentialStore:
    def __init__(self, use_in_memory: bool = False) -> None:
        self._keyring: Any | None = None
        self._memory: _InMemoryStore | None = None
        self._backend_name = "in_memory"
        self._init_backend(use_in_memory)

    def _init_backend(self, use_in_memory: bool) -> None:
        if use_in_memory:
            self._memory = _InMemoryStore()
            logger.info("credential_backend_initialized", backend="in_memory")
            return

        try:
            import keyring
        except ImportError:
            self._memory = _InMemoryStore()
            logger.warning("credential_backend_unavailable", backend="in_memory")
            return

        try:
            # keyring 在非 Windows 上可能解析成 plaintext 后端，此时不可用于存密钥
            backend = keyring.get_current_backend() if hasattr(keyring, "get_current_backend") else None
        except Exception:  # pragma: no cover - 后端探测本身可能抛错
            backend = None

        if backend is not None and any(
            t in str(backend).lower() for t in ("plaintext", "file", "chainer", "null")
        ):
            self._memory = _InMemoryStore()
            logger.warning("credential_backend_insecure", resolved_backend=str(backend))
            return

        self._keyring = keyring
        self._backend_name = "keyring"
        logger.info("credential_backend_initialized", backend="keyring")

    @property
    def backend(self) -> str:
        return self._backend_name

    @property
    def is_persistent(self) -> bool:
        return self._keyring is not None

    def get_secret(self, key: str) -> str | None:
        self._validate_key(key)
        if self._memory is not None:
            return self._memory.get(key)
        try:
            return self._keyring.get_password(_SERVICE_NAME, key)
        except Exception as exc:
            # 只记录键名，绝不记录值
            raise CredentialStoreError(f"读取凭证失败: {key}") from exc

    def set_secret(self, key: str, value: str) -> None:
        """写入凭证。空字符串表示清除，与 delete_secret 等价。"""
        self._validate_key(key)
        if value is None:
            raise CredentialStoreError(f"凭证值不可为 None: {key}")
        if value == "":
            self.delete_secret(key)
            return

        try:
            if self._memory is not None:
                self._memory.set(key, value)
            else:
                self._keyring.set_password(_SERVICE_NAME, key, value)
        except Exception as exc:
            raise CredentialStoreError(f"写入凭证失败: {key}") from exc
        logger.info("credential_set", key=key, persistent=self.is_persistent)

    def delete_secret(self, key: str) -> None:
        """删除凭证；不存在时视为成功（幂等）。"""
        self._validate_key(key)
        try:
            if self._memory is not None:
                self._memory.delete(key)
            else:
                self._keyring.delete_password(_SERVICE_NAME, key)
        except Exception as exc:
            if self._is_not_found(exc):
                logger.info("credential_delete_noop", key=key)
                return
            raise CredentialStoreError(f"删除凭证失败: {key}") from exc
        logger.info("credential_deleted", key=key)

    @staticmethod
    def _is_not_found(exc: Exception) -> bool:
        if type(exc).__name__ in ("PasswordNotFound", "InitError"):
            return True
        return "not found" in str(exc).lower()

    def list_keys(self) -> list[str]:
        return sorted(CREDENTIAL_KEYS)

    def _validate_key(self, key: str) -> None:
        if key not in CREDENTIAL_KEYS:
            raise CredentialStoreError(
                f"未知凭证键: {key}。允许: {sorted(CREDENTIAL_KEYS)}"
            )


class _InMemoryStore:
    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self._store.get(key)

    def set(self, key: str, value: str) -> None:
        self._store[key] = value

    def delete(self, key: str) -> None:
        self._store.pop(key, None)


_store: WinCredentialStore | None = None


def get_credential_store() -> WinCredentialStore:
    """进程内共享实例。

    每请求新建会让内存回落下「保存成功但下次启动全丢」，因此必须单例。
    """
    global _store
    if _store is None:
        from app.config import get_settings

        _store = WinCredentialStore(
            use_in_memory=get_settings().credential_backend == "in_memory"
        )
    return _store


def reset_credential_store() -> None:
    global _store
    _store = None
