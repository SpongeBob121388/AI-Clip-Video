"""OpenAI 兼容 LLM Adapter。

使用 openai Python SDK（≥1.0），支持 OpenAI 兼容接口。
API Key 从 CredentialStore 读取，不硬编码。
支持超时（30s）与重试（最多 3 次），容错解析 LLM 返回的 JSON。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from datetime import UTC, datetime

from app.models.enums import Decision, DecisionSource
from app.providers.credential.protocol import CredentialStoreProtocol
from app.providers.llm.protocol import CandidateContext, CandidateDecision
from app.utils.logging import get_logger

logger = get_logger("providers.llm.openai_adapter")

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_MODEL = "gpt-4o-mini"
_RETRY_BASE_BACKOFF = 0.5
_RETRY_MAX_BACKOFF = 8.0

_SYSTEM_PROMPT = (
    "你是一个视频剪辑助手。根据候选片段的上下文信息，判断每个片段应该删除（delete）还是保留（keep）。"
    "只返回 JSON，格式为："
    '{"decisions": [{"id": "候选ID", "decision": "delete或keep", "reason": "理由"}, ...]}'
)


class LLMUnavailableError(RuntimeError):
    pass


class OpenAILLMAdapter:
    def __init__(
        self,
        credential_store: CredentialStoreProtocol,
        model_name: str = _DEFAULT_MODEL,
        base_url: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
        max_retries: int = _DEFAULT_MAX_RETRIES,
    ) -> None:
        self._credential_store = credential_store
        self._model_name = model_name
        self._base_url = base_url
        self._timeout = timeout
        self._max_retries = max_retries
        self._client: object | None = None
        self._client_key: str | None = None

    def is_available(self) -> bool:
        try:
            import importlib.util

            if importlib.util.find_spec("openai") is None:
                return False
        except ImportError:
            return False

        api_key = self._credential_store.get_secret("llm_api_key")
        return api_key is not None and api_key != ""

    async def suggest_decisions(
        self, candidates: list[CandidateContext]
    ) -> list[CandidateDecision]:
        if not candidates:
            return []

        if len(candidates) > 50:
            raise ValueError(
                f"单批次候选数量超过 50 限制（当前 {len(candidates)}），请使用 Service 分批"
            )

        prompt = self._build_prompt(candidates)
        response_text = await self._call_with_retry(prompt)
        expected_ids = {c.id for c in candidates}
        decisions = self._parse_response(response_text, expected_ids)

        now_iso = datetime.now(UTC).isoformat()
        for d in decisions:
            d.decision_source = DecisionSource.LLM
            d.timestamp = now_iso

        logger.info(
            "llm_decisions_parsed",
            input_count=len(candidates),
            output_count=len(decisions),
        )
        return decisions

    def _build_prompt(self, candidates: list[CandidateContext]) -> str:
        items = []
        for c in candidates:
            items.append(
                {
                    "id": c.id,
                    "prev_text": c.prev_text,
                    "next_text": c.next_text,
                    "duration": round(c.duration_us / 1_000_000, 2),
                    "type": c.type.value,
                }
            )
        return f"{_SYSTEM_PROMPT}\n\n候选片段列表：\n{json.dumps(items, ensure_ascii=False)}"

    async def _call_with_retry(self, prompt: str) -> str:
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return await self._call_llm(prompt)
            except LLMUnavailableError:
                raise
            except Exception as exc:  # noqa: BLE001 - 仅兜住传输层异常
                last_error = exc
                logger.warning(
                    "llm_call_failed",
                    attempt=attempt,
                    max_retries=self._max_retries,
                    error_type=type(exc).__name__,
                )
                if attempt < self._max_retries:
                    # 指数退避 + 抖动；无退避的重试在故障期等于对上游施加压力
                    delay = min(
                        _RETRY_MAX_BACKOFF,
                        _RETRY_BASE_BACKOFF * (2 ** (attempt - 1)),
                    ) * (0.8 + 0.4 * random.random())
                    await asyncio.sleep(delay)

        raise LLMUnavailableError(
            f"LLM 调用 {self._max_retries} 次重试均失败（{type(last_error).__name__}）"
        ) from last_error

    async def _call_llm(self, prompt: str) -> str:
        api_key = self._credential_store.get_secret("llm_api_key")
        if not api_key:
            raise LLMUnavailableError("未配置 LLM API Key")

        client = self._get_client(api_key)
        response = await client.chat.completions.create(
            model=self._model_name,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
        )
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise LLMUnavailableError("LLM 返回不含 choices")
        return choices[0].message.content or ""

    def _get_client(self, api_key: str):
        """复用同一个客户端（各自持有连接池）；每次新建会泄漏 socket。"""
        from openai import AsyncOpenAI

        client = getattr(self, "_client", None)
        if client is None or getattr(self, "_client_key", None) != api_key:
            client = AsyncOpenAI(
                api_key=api_key,
                base_url=self._base_url,
                timeout=self._timeout,
                max_retries=0,  # 重试由 _call_with_retry 统一负责，避免 3x3 相乘
            )
            self._client = client
            self._client_key = api_key
        return client

    async def aclose(self) -> None:
        client = getattr(self, "_client", None)
        if client is not None:
            await client.close()
            self._client = None
            self._client_key = None

    @staticmethod
    def _parse_response(
        response_text: str, expected_ids: set[str]
    ) -> list[CandidateDecision]:
        data = OpenAILLMAdapter._extract_json(response_text)
        if data is None:
            logger.warn("llm_response_parse_failed", response=response_text[:200])
            return [
                CandidateDecision(
                    id=cid,
                    decision=Decision.UNCERTAIN,
                    reason="LLM 返回格式异常，解析失败",
                    decision_source=DecisionSource.LLM,
                    timestamp=datetime.now(UTC).isoformat(),
                )
                for cid in expected_ids
            ]

        raw_decisions = data.get("decisions", [])
        if not isinstance(raw_decisions, list):
            raw_decisions = []

        now_iso = datetime.now(UTC).isoformat()
        result: list[CandidateDecision] = []
        matched_ids: set[str] = set()

        for raw in raw_decisions:
            if not isinstance(raw, dict):
                continue
            raw_id = raw.get("id")
            if raw_id is None:
                continue
            if raw_id not in expected_ids:
                logger.warn("llm_unknown_candidate_id", candidate_id=raw_id)
                continue

            matched_ids.add(raw_id)
            decision_str = raw.get("decision", "uncertain")
            try:
                decision = Decision(decision_str)
            except ValueError:
                decision = Decision.UNCERTAIN

            reason = raw.get("reason", "")
            if not isinstance(reason, str):
                reason = str(reason)

            result.append(
                CandidateDecision(
                    id=raw_id,
                    decision=decision,
                    reason=reason,
                    decision_source=DecisionSource.LLM,
                    timestamp=now_iso,
                )
            )

        for cid in expected_ids - matched_ids:
            result.append(
                CandidateDecision(
                    id=cid,
                    decision=Decision.UNCERTAIN,
                    reason="LLM 未返回该候选的决策",
                    decision_source=DecisionSource.LLM,
                    timestamp=now_iso,
                )
            )

        return result

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            pass

        match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if match:
            try:
                return json.loads(match.group(1))
            except (json.JSONDecodeError, TypeError):
                pass

        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except (json.JSONDecodeError, TypeError):
                pass

        return None
