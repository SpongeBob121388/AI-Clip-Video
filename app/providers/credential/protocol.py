"""CredentialStore Protocol 接口定义。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

CREDENTIAL_KEYS = {
    "llm_api_key",
    "avatar_api_key",
    "llm_base_url",
    "asr_model_path",
    "avatar_base_url",
}


@runtime_checkable
class CredentialStoreProtocol(Protocol):
    def get_secret(self, key: str) -> str | None: ...

    def set_secret(self, key: str, value: str) -> None: ...

    def delete_secret(self, key: str) -> None: ...

    def list_keys(self) -> list[str]: ...
