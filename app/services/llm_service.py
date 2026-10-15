"""LLM Decision Service — 编排 LLM 剪辑决策流程。

流程：构建剪辑上下文 → 分批发送云 LLM → 解析结构化建议 → 降级规则策略 → 存储建议
LLM 不可用时降级为基于规则的默认策略。
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.models.enums import Decision, DecisionSource, SuggestionType
from app.providers.llm.openai_adapter import LLMUnavailableError
from app.providers.llm.protocol import CandidateContext, CandidateDecision, LLMProviderProtocol
from app.utils.logging import get_logger

logger = get_logger("services.llm_service")

_DEFAULT_BATCH_SIZE = 50
_DEFAULT_SILENCE_THRESHOLD_US = 500_000


class RuleBasedDecisionStrategy:
    """基于规则的默认决策策略。

    LLM 不可用时降级使用：删除所有超过阈值的静音候选。
    """

    def __init__(
        self, silence_threshold_us: int = _DEFAULT_SILENCE_THRESHOLD_US
    ) -> None:
        self._silence_threshold_us = silence_threshold_us

    def decide(
        self, candidates: list[CandidateContext]
    ) -> list[CandidateDecision]:
        now_iso = datetime.now(UTC).isoformat()
        result: list[CandidateDecision] = []

        for c in candidates:
            if c.type == SuggestionType.SILENCE and c.duration_us > self._silence_threshold_us:
                decision = Decision.DELETE
                reason = f"静音时长 {c.duration_us / 1_000_000:.2f}s 超过阈值，规则策略建议删除"
            else:
                decision = Decision.KEEP
                reason = "规则策略保留"

            result.append(
                CandidateDecision(
                    id=c.id,
                    decision=decision,
                    reason=reason,
                    decision_source=DecisionSource.RULE,
                    timestamp=now_iso,
                )
            )

        return result


class LLMDecisionService:
    """LLM 决策服务 — 编排分批 LLM 调用与降级策略。"""

    def __init__(
        self,
        llm_provider: LLMProviderProtocol,
        rule_strategy: RuleBasedDecisionStrategy | None = None,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> None:
        self._llm_provider = llm_provider
        self._rule_strategy = rule_strategy or RuleBasedDecisionStrategy()
        self._batch_size = batch_size

    async def get_decisions(
        self, candidates: list[CandidateContext]
    ) -> tuple[list[CandidateDecision], bool]:
        """获取剪辑决策建议。

        返回 (decisions, used_fallback):
          - decisions: 每个候选对应的决策建议
          - used_fallback: 是否使用了降级规则策略
        """
        if not candidates:
            return [], False

        if not self._llm_provider.is_available():
            logger.warning("llm_not_available_using_rule_strategy")
            return self._rule_strategy.decide(candidates), True

        batches = self._split_batches(candidates)
        all_decisions: list[CandidateDecision] = []
        used_fallback = False

        for i, batch in enumerate(batches):
            try:
                batch_decisions = await self._llm_provider.suggest_decisions(batch)
                all_decisions.extend(batch_decisions)
                logger.info(
                    "llm_batch_completed",
                    batch_index=i + 1,
                    total_batches=len(batches),
                    batch_size=len(batch),
                )
            except (LLMUnavailableError, TimeoutError) as exc:
                # 只兜住"外部服务不可用"，编程错误（KeyError/属性拼错等）必须抛出，
                # 否则整条 LLM 链路坏掉而结果看起来正常
                logger.error(
                    "llm_batch_fallback",
                    batch_index=i + 1,
                    error_type=type(exc).__name__,
                    error=str(exc)[:200],
                )
                fallback = self._rule_strategy.decide(batch)
                all_decisions.extend(fallback)
                used_fallback = True

        logger.info(
            "llm_decisions_completed",
            total=len(all_decisions),
            used_fallback=used_fallback,
        )
        return all_decisions, used_fallback

    def _split_batches(
        self, candidates: list[CandidateContext]
    ) -> list[list[CandidateContext]]:
        batches: list[list[CandidateContext]] = []
        for i in range(0, len(candidates), self._batch_size):
            batches.append(candidates[i : i + self._batch_size])
        return batches
