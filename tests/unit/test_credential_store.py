"""CredentialStore 与日志脱敏单元测试。"""

from __future__ import annotations

import pytest

from app.providers.credential.protocol import (
    CREDENTIAL_KEYS,
    CredentialStoreProtocol,
)
from app.providers.credential.win_credential_store import (
    CredentialStoreError,
    WinCredentialStore,
    get_credential_store,
)
from app.utils.log_redaction import (
    is_sensitive_key,
    redact_dict,
    redact_processor,
    redact_value,
)


@pytest.mark.unit
class TestCredentialStore:
    def test_protocol_compliance(self) -> None:
        store = WinCredentialStore(use_in_memory=True)
        assert isinstance(store, CredentialStoreProtocol)

    def test_set_and_get_secret(self) -> None:
        store = WinCredentialStore(use_in_memory=True)
        store.set_secret("llm_api_key", "sk-test-12345678")
        result = store.get_secret("llm_api_key")
        assert result == "sk-test-12345678"

    def test_get_nonexistent_secret_returns_none(self) -> None:
        store = WinCredentialStore(use_in_memory=True)
        result = store.get_secret("llm_api_key")
        assert result is None

    def test_delete_secret(self) -> None:
        store = WinCredentialStore(use_in_memory=True)
        store.set_secret("llm_api_key", "sk-test")
        store.delete_secret("llm_api_key")
        assert store.get_secret("llm_api_key") is None

    def test_delete_nonexistent_no_error(self) -> None:
        store = WinCredentialStore(use_in_memory=True)
        store.delete_secret("llm_api_key")  # 幂等：不应抛出
        assert store.get_secret("llm_api_key") is None

    def test_invalid_key_rejected(self) -> None:
        store = WinCredentialStore(use_in_memory=True)
        with pytest.raises(CredentialStoreError, match="未知凭证键"):
            store.get_secret("invalid_key")

    def test_empty_value_clears_secret(self) -> None:
        """空串表示清除：API 必须能撤掉已保存的 Key。"""
        store = WinCredentialStore(use_in_memory=True)
        store.set_secret("llm_api_key", "sk-test-12345678")
        store.set_secret("llm_api_key", "")
        assert store.get_secret("llm_api_key") is None

    def test_memory_backend_is_not_persistent(self) -> None:
        store = WinCredentialStore(use_in_memory=True)
        assert store.is_persistent is False
        assert store.backend == "in_memory"

    def test_singleton_survives_calls(self) -> None:
        """同一进程内必须共享实例，否则内存回落下'保存成功'是假的。"""
        first = get_credential_store()
        first.set_secret("llm_api_key", "sk-shared-123")
        assert get_credential_store().get_secret("llm_api_key") == "sk-shared-123"

    def test_list_keys(self) -> None:
        store = WinCredentialStore(use_in_memory=True)
        keys = store.list_keys()
        assert "llm_api_key" in keys
        assert "avatar_api_key" in keys
        assert len(keys) == len(CREDENTIAL_KEYS)

    def test_roundtrip_multiple_keys(self) -> None:
        store = WinCredentialStore(use_in_memory=True)
        store.set_secret("llm_api_key", "key1")
        store.set_secret("llm_base_url", "https://api.example.com")
        store.set_secret("avatar_api_key", "key2")
        assert store.get_secret("llm_api_key") == "key1"
        assert store.get_secret("llm_base_url") == "https://api.example.com"
        assert store.get_secret("avatar_api_key") == "key2"


@pytest.mark.unit
class TestLogRedaction:
    def test_redact_api_key(self) -> None:
        result = redact_value("sk-abcdefgh1234567890")
        assert "sk-abcdefgh****" in result
        assert "1234567890" not in result

    def test_redact_bearer_token(self) -> None:
        result = redact_value("Bearer abcdefgh1234567890")
        assert "Bearer abcdefgh****" in result

    def test_redact_user_path(self) -> None:
        result = redact_value("C:\\Users\\JohnDoe\\Documents\\file.mp4")
        assert "<redacted>" in result
        assert "JohnDoe" not in result

    def test_non_string_passthrough(self) -> None:
        assert redact_value(123) == 123
        assert redact_value(None) is None
        assert redact_value([1, 2]) == [1, 2]

    def test_redact_dict_masks_by_key_name(self) -> None:
        """字段名命中机密词即整体遮蔽 —— 不依赖密钥形状（HeyGen/D-ID 为不定长 hex）。"""
        data = {"api_key": "sk-abcdefgh1234", "safe": "hello"}
        result = redact_dict(data)
        assert "sk-abcdefgh1234" not in result["api_key"]
        assert result["api_key"] == "***REDACTED***"
        assert result["safe"] == "hello"

    def test_redact_dict_masks_shapeless_keys(self) -> None:
        data = {"avatar_api_key": "9f2c1d3e4b5a6978", "nested": {"token": "abc"}}
        result = redact_dict(data)
        assert "9f2c1d3e4b5a6978" not in str(result)
        assert result["nested"]["token"] == "***REDACTED***"

    def test_processor_masks_event_fields(self) -> None:
        event = {
            "event": "llm_test_success",
            "api_key": "anything-at-all",
            "path": "C:\\Users\\Jane\\a.mp4",
        }
        out = redact_processor(None, "info", event)
        assert out["api_key"] == "***REDACTED***"
        assert "Jane" not in out["path"]
        assert out["path"] == "C:\\Users\\<redacted>\\a.mp4"

    def test_configure_logging_installs_redaction(self) -> None:
        """脱敏必须真的挂进 structlog 管线，否则只是摆设。"""
        import structlog

        from app.utils.logging import configure_logging

        configure_logging(level="INFO", fmt="json")
        assert redact_processor in structlog.get_config()["processors"]

    def test_is_sensitive_key(self) -> None:
        assert is_sensitive_key("api_key") is True
        assert is_sensitive_key("password") is True
        assert is_sensitive_key("llm_token") is True
        assert is_sensitive_key("safe_field") is False

    def test_no_redaction_for_safe_string(self) -> None:
        assert redact_value("hello world") == "hello world"
