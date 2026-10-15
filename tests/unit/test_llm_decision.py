"""LLM 决策 Provider 单元测试。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.enums import Decision, DecisionSource, SuggestionType
from app.providers.credential.protocol import CredentialStoreProtocol
from app.providers.llm.openai_adapter import LLMUnavailableError, OpenAILLMAdapter
from app.providers.llm.protocol import CandidateContext, CandidateDecision, LLMProviderProtocol
from app.services.llm_service import LLMDecisionService, RuleBasedDecisionStrategy


def _make_candidate(
    cid: str = "c1",
    duration_us: int = 1_000_000,
    type_: SuggestionType = SuggestionType.SILENCE,
    prev_text: str = "前面的话",
    next_text: str = "后面的话",
) -> CandidateContext:
    return CandidateContext(
        id=cid,
        prev_text=prev_text,
        next_text=next_text,
        duration_us=duration_us,
        type=type_,
    )


def _make_credential_store(api_key: str | None = "test-key") -> CredentialStoreProtocol:
    store = MagicMock(spec=CredentialStoreProtocol)
    store.get_secret = MagicMock(return_value=api_key)
    return store


@pytest.mark.unit
class TestCandidateContext:
    def test_creation(self) -> None:
        ctx = _make_candidate()
        assert ctx.id == "c1"
        assert ctx.duration_us == 1_000_000
        assert ctx.type == SuggestionType.SILENCE

    def test_default_texts(self) -> None:
        ctx = CandidateContext(id="c1", duration_us=500_000, type=SuggestionType.SILENCE)
        assert ctx.prev_text == ""
        assert ctx.next_text == ""

    def test_negative_duration_rejected(self) -> None:
        with pytest.raises(ValueError):
            CandidateContext(id="c1", duration_us=-1, type=SuggestionType.SILENCE)


@pytest.mark.unit
class TestCandidateDecision:
    def test_creation(self) -> None:
        now = datetime.now(UTC).isoformat()
        d = CandidateDecision(
            id="c1",
            decision=Decision.DELETE,
            reason="测试",
            decision_source=DecisionSource.LLM,
            timestamp=now,
        )
        assert d.decision == Decision.DELETE
        assert d.decision_source == DecisionSource.LLM


@pytest.mark.unit
class TestLLMProtocol:
    def test_openai_adapter_protocol(self) -> None:
        store = _make_credential_store()
        adapter = OpenAILLMAdapter(store)
        assert isinstance(adapter, LLMProviderProtocol)


@pytest.mark.unit
class TestOpenAILLMAdapter:
    def test_is_available_with_key(self) -> None:
        store = _make_credential_store("test-key")
        adapter = OpenAILLMAdapter(store)
        result = adapter.is_available()
        assert isinstance(result, bool)

    def test_is_available_without_key(self) -> None:
        store = _make_credential_store(None)
        adapter = OpenAILLMAdapter(store)
        assert adapter.is_available() is False

    def test_is_available_empty_key(self) -> None:
        store = _make_credential_store("")
        adapter = OpenAILLMAdapter(store)
        assert adapter.is_available() is False

    async def test_suggest_empty_candidates(self) -> None:
        store = _make_credential_store()
        adapter = OpenAILLMAdapter(store)
        result = await adapter.suggest_decisions([])
        assert result == []

    async def test_suggest_over_batch_limit(self) -> None:
        store = _make_credential_store()
        adapter = OpenAILLMAdapter(store)
        candidates = [_make_candidate(f"c{i}") for i in range(51)]
        with pytest.raises(ValueError, match="超过 50"):
            await adapter.suggest_decisions(candidates)

    async def test_normal_decision_flow(self) -> None:
        store = _make_credential_store()
        adapter = OpenAILLMAdapter(store)

        llm_response = json.dumps(
            {
                "decisions": [
                    {"id": "c1", "decision": "delete", "reason": "2秒静音，前后语义连贯"},
                    {"id": "c2", "decision": "keep", "reason": "语义停顿，保留"},
                ]
            }
        )
        adapter._call_with_retry = AsyncMock(return_value=llm_response)

        candidates = [_make_candidate("c1"), _make_candidate("c2")]
        decisions = await adapter.suggest_decisions(candidates)

        assert len(decisions) == 2
        assert decisions[0].id == "c1"
        assert decisions[0].decision == Decision.DELETE
        assert decisions[0].reason == "2秒静音，前后语义连贯"
        assert decisions[1].id == "c2"
        assert decisions[1].decision == Decision.KEEP

    async def test_decision_source_is_llm(self) -> None:
        store = _make_credential_store()
        adapter = OpenAILLMAdapter(store)
        adapter._call_with_retry = AsyncMock(
            return_value='{"decisions": [{"id": "c1", "decision": "delete", "reason": "r"}]}'
        )
        decisions = await adapter.suggest_decisions([_make_candidate("c1")])
        assert decisions[0].decision_source == DecisionSource.LLM
        assert decisions[0].timestamp != ""

    async def test_retry_exhausted_raises(self) -> None:
        store = _make_credential_store()
        adapter = OpenAILLMAdapter(store, max_retries=3)
        adapter._call_llm = AsyncMock(side_effect=RuntimeError("network error"))

        with pytest.raises(LLMUnavailableError, match="3 次重试均失败"):
            await adapter.suggest_decisions([_make_candidate("c1")])

        assert adapter._call_llm.call_count == 3

    async def test_retry_succeeds_on_second_attempt(self) -> None:
        store = _make_credential_store()
        adapter = OpenAILLMAdapter(store, max_retries=3)
        good_response = '{"decisions": [{"id": "c1", "decision": "keep", "reason": "ok"}]}'
        adapter._call_llm = AsyncMock(
            side_effect=[RuntimeError("timeout"), good_response]
        )

        decisions = await adapter.suggest_decisions([_make_candidate("c1")])
        assert len(decisions) == 1
        assert decisions[0].decision == Decision.KEEP
        assert adapter._call_llm.call_count == 2


@pytest.mark.unit
class TestFaultTolerantParsing:
    def test_valid_json(self) -> None:
        text = '{"decisions": [{"id": "c1", "decision": "delete", "reason": "r"}]}'
        result = OpenAILLMAdapter._parse_response(text, {"c1"})
        assert len(result) == 1
        assert result[0].decision == Decision.DELETE

    def test_invalid_json_all_uncertain(self) -> None:
        text = "这不是JSON"
        result = OpenAILLMAdapter._parse_response(text, {"c1", "c2"})
        assert len(result) == 2
        assert all(d.decision == Decision.UNCERTAIN for d in result)

    def test_json_in_markdown_code_block(self) -> None:
        text = '```json\n{"decisions": [{"id": "c1", "decision": "keep", "reason": "r"}]}\n```'
        result = OpenAILLMAdapter._parse_response(text, {"c1"})
        assert len(result) == 1
        assert result[0].decision == Decision.KEEP

    def test_json_embedded_in_text(self) -> None:
        text = '好的，以下是结果：{"decisions": [{"id": "c1", "decision": "delete", "reason": "r"}]} 谢谢'
        result = OpenAILLMAdapter._parse_response(text, {"c1"})
        assert len(result) == 1
        assert result[0].decision == Decision.DELETE

    def test_count_mismatch_missing_marked_uncertain(self) -> None:
        text = '{"decisions": [{"id": "c1", "decision": "delete", "reason": "r"}]}'
        result = OpenAILLMAdapter._parse_response(text, {"c1", "c2", "c3"})
        assert len(result) == 3
        ids = {d.id for d in result}
        assert ids == {"c1", "c2", "c3"}
        c2 = next(d for d in result if d.id == "c2")
        c3 = next(d for d in result if d.id == "c3")
        assert c2.decision == Decision.UNCERTAIN
        assert c3.decision == Decision.UNCERTAIN

    def test_unknown_id_ignored(self) -> None:
        text = (
            '{"decisions": ['
            '{"id": "c1", "decision": "delete", "reason": "r"},'
            '{"id": "nonexistent", "decision": "delete", "reason": "r"}'
            "]}"
        )
        result = OpenAILLMAdapter._parse_response(text, {"c1"})
        assert len(result) == 1
        assert result[0].id == "c1"

    def test_invalid_decision_value_marked_uncertain(self) -> None:
        text = '{"decisions": [{"id": "c1", "decision": "maybe", "reason": "r"}]}'
        result = OpenAILLMAdapter._parse_response(text, {"c1"})
        assert result[0].decision == Decision.UNCERTAIN

    def test_missing_decisions_key(self) -> None:
        text = '{"result": "ok"}'
        result = OpenAILLMAdapter._parse_response(text, {"c1"})
        assert len(result) == 1
        assert result[0].decision == Decision.UNCERTAIN

    def test_non_dict_entry_skipped(self) -> None:
        text = '{"decisions": ["invalid", {"id": "c1", "decision": "keep", "reason": "r"}]}'
        result = OpenAILLMAdapter._parse_response(text, {"c1"})
        assert len(result) == 1
        assert result[0].decision == Decision.KEEP


@pytest.mark.unit
class TestRuleBasedDecisionStrategy:
    def test_silence_above_threshold_deleted(self) -> None:
        strategy = RuleBasedDecisionStrategy(silence_threshold_us=500_000)
        candidates = [_make_candidate("c1", duration_us=1_000_000, type_=SuggestionType.SILENCE)]
        decisions = strategy.decide(candidates)
        assert decisions[0].decision == Decision.DELETE
        assert decisions[0].decision_source == DecisionSource.RULE

    def test_silence_below_threshold_kept(self) -> None:
        strategy = RuleBasedDecisionStrategy(silence_threshold_us=500_000)
        candidates = [_make_candidate("c1", duration_us=300_000, type_=SuggestionType.SILENCE)]
        decisions = strategy.decide(candidates)
        assert decisions[0].decision == Decision.KEEP

    def test_non_silence_always_kept(self) -> None:
        strategy = RuleBasedDecisionStrategy(silence_threshold_us=0)
        candidates = [_make_candidate("c1", duration_us=10_000_000, type_=SuggestionType.FILLER_WORD)]
        decisions = strategy.decide(candidates)
        assert decisions[0].decision == Decision.KEEP

    def test_empty_candidates(self) -> None:
        strategy = RuleBasedDecisionStrategy()
        assert strategy.decide([]) == []

    def test_timestamp_set(self) -> None:
        strategy = RuleBasedDecisionStrategy()
        decisions = strategy.decide([_make_candidate("c1")])
        assert decisions[0].timestamp != ""


@pytest.mark.unit
class TestLLMDecisionService:
    def _make_mock_provider(
        self, decisions: list[CandidateDecision] | None = None, available: bool = True
    ) -> MagicMock:
        provider = MagicMock(spec=LLMProviderProtocol)
        provider.is_available = MagicMock(return_value=available)
        provider.suggest_decisions = AsyncMock(
            return_value=decisions
            or [
                CandidateDecision(
                    id="c1",
                    decision=Decision.DELETE,
                    reason="llm",
                    decision_source=DecisionSource.LLM,
                    timestamp=datetime.now(UTC).isoformat(),
                )
            ]
        )
        return provider

    async def test_empty_candidates(self) -> None:
        provider = self._make_mock_provider()
        service = LLMDecisionService(provider)
        decisions, fallback = await service.get_decisions([])
        assert decisions == []
        assert fallback is False

    async def test_normal_flow(self) -> None:
        provider = self._make_mock_provider()
        service = LLMDecisionService(provider)
        decisions, fallback = await service.get_decisions([_make_candidate("c1")])
        assert len(decisions) == 1
        assert fallback is False
        assert decisions[0].decision_source == DecisionSource.LLM

    async def test_llm_unavailable_fallback(self) -> None:
        provider = self._make_mock_provider(available=False)
        service = LLMDecisionService(provider)
        candidates = [_make_candidate("c1", duration_us=1_000_000, type_=SuggestionType.SILENCE)]
        decisions, fallback = await service.get_decisions(candidates)
        assert fallback is True
        assert decisions[0].decision_source == DecisionSource.RULE
        assert decisions[0].decision == Decision.DELETE

    async def test_llm_error_fallback(self) -> None:
        provider = MagicMock(spec=LLMProviderProtocol)
        provider.is_available = MagicMock(return_value=True)
        provider.suggest_decisions = AsyncMock(side_effect=LLMUnavailableError("failed"))

        service = LLMDecisionService(provider)
        candidates = [_make_candidate("c1", duration_us=1_000_000, type_=SuggestionType.SILENCE)]
        decisions, fallback = await service.get_decisions(candidates)
        assert fallback is True
        assert decisions[0].decision_source == DecisionSource.RULE

    async def test_batching_200_candidates(self) -> None:
        provider = MagicMock(spec=LLMProviderProtocol)
        provider.is_available = MagicMock(return_value=True)

        async def mock_suggest(cands: list[CandidateContext]) -> list[CandidateDecision]:
            now = datetime.now(UTC).isoformat()
            return [
                CandidateDecision(
                    id=c.id,
                    decision=Decision.KEEP,
                    reason="ok",
                    decision_source=DecisionSource.LLM,
                    timestamp=now,
                )
                for c in cands
            ]

        provider.suggest_decisions = mock_suggest

        service = LLMDecisionService(provider, batch_size=50)
        candidates = [_make_candidate(f"c{i}") for i in range(200)]
        decisions, fallback = await service.get_decisions(candidates)

        assert len(decisions) == 200
        assert fallback is False
        ids = {d.id for d in decisions}
        assert ids == {f"c{i}" for i in range(200)}

    async def test_partial_batch_fallback(self) -> None:
        provider = MagicMock(spec=LLMProviderProtocol)
        provider.is_available = MagicMock(return_value=True)

        call_count = 0

        async def mock_suggest(cands: list[CandidateContext]) -> list[CandidateDecision]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise LLMUnavailableError("first batch fails")
            now = datetime.now(UTC).isoformat()
            return [
                CandidateDecision(
                    id=c.id,
                    decision=Decision.KEEP,
                    reason="ok",
                    decision_source=DecisionSource.LLM,
                    timestamp=now,
                )
                for c in cands
            ]

        provider.suggest_decisions = mock_suggest

        service = LLMDecisionService(provider, batch_size=2)
        candidates = [
            _make_candidate("c1", duration_us=1_000_000, type_=SuggestionType.SILENCE),
            _make_candidate("c2", duration_us=1_000_000, type_=SuggestionType.SILENCE),
            _make_candidate("c3", duration_us=1_000_000, type_=SuggestionType.SILENCE),
            _make_candidate("c4", duration_us=1_000_000, type_=SuggestionType.SILENCE),
        ]
        decisions, fallback = await service.get_decisions(candidates)

        assert len(decisions) == 4
        assert fallback is True
        c1_decision = next(d for d in decisions if d.id == "c1")
        assert c1_decision.decision_source == DecisionSource.RULE
        c3_decision = next(d for d in decisions if d.id == "c3")
        assert c3_decision.decision_source == DecisionSource.LLM

    def test_split_batches(self) -> None:
        provider = self._make_mock_provider()
        service = LLMDecisionService(provider, batch_size=50)
        candidates = [_make_candidate(f"c{i}") for i in range(120)]
        batches = service._split_batches(candidates)
        assert len(batches) == 3
        assert len(batches[0]) == 50
        assert len(batches[1]) == 50
        assert len(batches[2]) == 20
