"""分析链路服务 —— 把 ASR / VAD / LLM / EDL 编排成一条真正能跑的流水线。

设计要点：
- 一次分析只抽一遍音轨（16kHz 单声道 WAV），ASR 与静音检测共用，避免两倍耗时与两份临时文件。
- 诚实降级：可选依赖（faster_whisper、silero）缺失时，跳过 ASR 并回落到音量检测，
  结果里明确写出 `asr_skipped_reason`；绝不伪造"识别成功但没有任何语句"。
- 所有阶段都可取消（`should_stop`），进度按阶段权重推进。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.models.asr_result import ASRResult
from app.models.edl import EDL, Suggestion
from app.models.enums import (
    ASREngine,
    Decision,
    DecisionSource,
    DetectionMethod,
    ModelSize,
    OperationType,
    SuggestionStatus,
    SuggestionType,
)
from app.models.media import MediaProbe
from app.models.silence_candidate import SilenceCandidate
from app.persistence.database import get_project_engine
from app.persistence.models.project_models import (
    SilenceCandidateRecord,
    Transcript,
    TranscriptOverride,
)
from app.providers.llm.protocol import CandidateContext
from app.providers.registry import Providers
from app.providers.vad.protocol import VADParams
from app.services.detect_service import SilenceDetectService
from app.services.llm_service import LLMDecisionService
from app.services.project_service import ProjectService
from app.utils.log_redaction import redact_value
from app.utils.logging import get_logger
from app.utils.path_validator import validate_media_path

logger = get_logger("services.pipeline")

#: 阶段权重（累计到 1.0）
_WEIGHTS = {"probe": 0.02, "audio": 0.10, "asr": 0.55, "detect": 0.20, "decide": 0.08, "save": 0.05}


class PipelineError(RuntimeError):
    pass


class ProgressSink(Protocol):
    """job_runner.JobContext 的结构化接口，便于单测注入。"""

    def progress(self, value: float, message: str = "") -> None: ...

    def should_stop(self) -> bool: ...


class NullProgress:
    def progress(self, value: float, message: str = "") -> None:
        return None

    def should_stop(self) -> bool:
        return False


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - 清理失败不应影响结果
        logger.warning("pipeline_temp_cleanup_failed", error=str(exc))


class PipelineService:
    def __init__(self, projects: ProjectService, providers: Providers) -> None:
        self._projects = projects
        self._providers = providers

    # ------------------------------------------------------------------ 探测

    async def probe_source(self, project_id: str) -> MediaProbe:
        source_path = await self._projects.source_path(project_id)
        probe = await self._providers.ffmpeg.probe(str(validate_media_path(source_path)))
        if not probe.has_video:
            raise PipelineError("源文件不含视频流，无法剪辑")
        if probe.duration_us <= 0:
            raise PipelineError("源文件时长为 0，疑似损坏")
        return probe

    async def extract_audio(self, source_path: str, track_index: int = 0) -> Path:
        path = validate_media_path(source_path)
        tmp_dir = get_settings().data_dir / "audio_tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        wav = tmp_dir / f"audio_{uuid4().hex[:8]}.wav"
        await self._providers.ffmpeg.extract_audio(str(path), track_index, str(wav))
        return wav

    # ------------------------------------------------------------------ 阶段

    async def transcribe(
        self,
        probe: MediaProbe,
        audio_path: Path,
        engine: ASREngine,
        model_size: ModelSize,
        language: str = "zh",
        progress: ProgressSink | None = None,
    ) -> ASRResult:
        from app.services.asr_service import ASRService

        provider = self._providers.asr(engine)
        if not provider.is_available():  # type: ignore[union-attr]
            raise PipelineError(f"ASR 引擎不可用: {engine.value}")

        service = ASRService(self._providers.ffmpeg, provider)
        result = await service.run_asr(
            probe,
            audio_track_index=0,
            language=language,
            model_size=model_size,
            pre_extracted_wav=audio_path,
        )
        if progress is not None:
            progress.progress(1.0, "语音识别完成")
        return result

    async def detect_silence(
        self,
        audio_path: Path,
        total_duration_us: int,
        params: VADParams,
        asr_result: ASRResult | None = None,
        method: DetectionMethod | None = None,
        progress: ProgressSink | None = None,
    ) -> list[SilenceCandidate]:
        vad = self._providers.vad(method) if method else self._providers.preferred_vad()
        service = SilenceDetectService(vad)
        sink = progress or NullProgress()
        detected = await service.detect(
            str(audio_path),
            params,
            asr_result=asr_result,
            progress_callback=lambda value, message: sink.progress(value, message),
            total_duration_us=total_duration_us,
        )
        # 没有候选不是错误：整段都保留也是一种合法结果，由调用方决定是否提示用户
        return detected

    async def decide(
        self, candidates: list[SilenceCandidate]
    ) -> tuple[list[tuple[SilenceCandidate, Decision, DecisionSource, str]], bool]:
        """返回 (每个候选的决策, 是否走了规则降级)。"""
        contexts = [
            CandidateContext(
                id=c.id,
                prev_text=c.prev_utterance_text,
                next_text=c.next_utterance_text,
                duration_us=c.duration_us,
                type=SuggestionType.SILENCE,
            )
            for c in candidates
        ]
        service = LLMDecisionService(self._providers.llm())
        decisions, used_fallback = await service.get_decisions(contexts)

        by_id = {d.id: d for d in decisions}
        rows: list[tuple[SilenceCandidate, Decision, DecisionSource, str]] = []
        for cand in candidates:
            hit = by_id.get(cand.id)
            if hit is None:
                # LLM 少答了一条必须显式保留为"未确定"，不能默认删除内容
                rows.append((cand, Decision.UNCERTAIN, DecisionSource.RULE, "模型未返回该区间决策"))
                continue
            rows.append((cand, hit.decision, hit.decision_source, hit.reason))
        return rows, used_fallback

    # ------------------------------------------------------------------ 建议

    @staticmethod
    def suggestions_from(
        rows: list[tuple[SilenceCandidate, Decision, DecisionSource, str]],
        duration_us: int,
    ) -> list[Suggestion]:
        """决策 → EDL 建议。区间互不重叠（EDL 模型的硬约束）。"""
        ordered = sorted(rows, key=lambda row: row[0].start_us)
        suggestions: list[Suggestion] = []
        previous_end = -1
        for cand, decision, source, reason in ordered:
            start = max(0, min(cand.start_us, duration_us))
            end = max(0, min(cand.end_us, duration_us))
            if end <= start or start < previous_end:
                logger.warning(
                    "candidate_dropped_overlaps",
                    candidate_id=cand.id,
                    start_us=start,
                    end_us=end,
                )
                continue
            status = (
                SuggestionStatus.ACCEPTED
                if decision == Decision.DELETE
                else SuggestionStatus.REJECTED
                if decision == Decision.KEEP
                else SuggestionStatus.PENDING
            )
            suggestions.append(
                Suggestion(
                    id=f"cut-{cand.id.split('-')[-1] or uuid4().hex[:8]}",
                    source_start_us=start,
                    source_end_us=end,
                    suggestion_type=SuggestionType.SILENCE,
                    decision=decision,
                    source=source,
                    reason=reason[:2000],
                    status=status,
                    prev_text=cand.prev_utterance_text[:2000],
                    next_text=cand.next_utterance_text[:2000],
                )
            )
            previous_end = end
        return suggestions

    @staticmethod
    def merge_into_edl(edl: EDL, suggestions: list[Suggestion]) -> EDL:
        """替换上一轮自动建议，保留人工建议。

        不能走 EDLService.add_suggestions：它会把所有建议状态重置为 PENDING，
        于是人工确认过的决策被抹掉，刚算出来的 ACCEPTED/REJECTED 也一并丢失。
        """
        manual = [
            s.model_copy(deep=True) for s in edl.suggestions if s.source == DecisionSource.MANUAL
        ]
        merged = sorted(manual, key=lambda s: s.source_start_us)
        used_ids = {s.id for s in merged}

        for cand in sorted(suggestions, key=lambda s: s.source_start_us):
            if any(
                cand.source_start_us < kept.source_end_us and kept.source_start_us < cand.source_end_us
                for kept in merged
            ):
                logger.warning("suggestion_dropped_overlaps_manual", suggestion_id=cand.id)
                continue
            item = cand.model_copy(deep=True)
            while item.id in used_ids:
                item.id = f"cut-{uuid4().hex[:8]}"
            used_ids.add(item.id)
            merged.append(item)

        merged.sort(key=lambda s: s.source_start_us)
        fresh = edl.model_copy(deep=True)
        fresh.suggestions = merged
        return fresh

    # ------------------------------------------------------------------ 全链路

    async def analyze(
        self,
        project_id: str,
        progress: ProgressSink | None = None,
        language: str = "zh",
        min_silence_ms: int = 500,
        dbfs_threshold: float = -40.0,
        run_asr: bool = True,
        audio_track_index: int | None = None,
    ) -> dict:
        sink = progress or NullProgress()
        base = 0.0

        def stage(name: str, ratio: float, message: str) -> None:
            sink.progress(min(1.0, base + _WEIGHTS[name] * ratio), message)

        source_path = await self._projects.source_path(project_id)

        stage("probe", 0.0, "探测媒体信息")
        probe = await self.probe_source(project_id)
        await self._projects.attach_source(
            project_id,
            source_path,
            probe.duration_us,
            probe.rotation,
            probe.is_vfr,
            probe.time_base,
        )
        edl, _ = await self._projects.ensure_initial_edl(
            project_id, source_path, probe.duration_us, probe.rotation
        )
        if sink.should_stop():
            raise PipelineError("任务已取消")
        stage("probe", 1.0, "媒体探测完成")
        base += _WEIGHTS["probe"]

        transcript: ASRResult | None = None
        asr_skipped_reason: str | None = None
        engine = await self.preferences_asr_engine()

        if not probe.has_audio:
            raise PipelineError("源文件没有音轨，无法进行静音检测与语音识别")
        audio_tracks = [t for t in probe.tracks if t.codec_type == "audio"]
        if audio_track_index is not None and audio_track_index >= len(audio_tracks):
            # 识别用轨与导出用轨必须同源，否则字幕对着一轨、画面播着另一轨
            raise PipelineError(
                f"源文件只有 {len(audio_tracks)} 条音轨，无法用第 {audio_track_index + 1} 条做识别"
            )

        try:
            # 两种检测器都只读波形文件，所以无论是否跑 ASR 都要先抽 WAV
            stage("audio", 0.0, "提取音轨")
            audio_path = await self.extract_audio(source_path, audio_track_index or 0)
            stage("audio", 1.0, "音轨提取完成")
            base += _WEIGHTS["audio"]

            if not run_asr:
                asr_skipped_reason = "本次分析未启用语音识别"
                base += _WEIGHTS["asr"]
            elif engine is None:
                asr_skipped_reason = '未安装 faster-whisper，已跳过语音识别（安装：pip install "faster-whisper"）'
                base += _WEIGHTS["asr"]
            else:
                try:
                    stage("asr", 0.0, "语音识别中")
                    transcript = await self.transcribe(
                        probe,
                        audio_path,
                        engine,
                        await self.preferences_model_size(),
                        language,
                        _ScopedProgress(sink, base, _WEIGHTS["asr"], "语音识别"),
                    )
                    base += _WEIGHTS["asr"]
                    stage("asr", 1.0, f"识别到 {len(transcript.utterances)} 条语句")
                    await self.save_transcript(project_id, transcript)
                except Exception as exc:  # noqa: BLE001 - ASR 失败不该带走整次分析
                    # 静音检测用的是同一份 WAV，识别挂了它仍然可用，所以降级而不是报错退出
                    transcript = None
                    base += _WEIGHTS["asr"]
                    reason = redact_value(str(exc))[:200] or type(exc).__name__
                    asr_skipped_reason = reason if isinstance(exc, PipelineError) else f"语音识别异常：{reason}"
                    logger.warning(
                        "asr_stage_skipped",
                        error_type=type(exc).__name__,
                        reason=asr_skipped_reason,
                    )
                    if sink.should_stop():
                        raise PipelineError("任务已取消") from exc

            if sink.should_stop():
                raise PipelineError("任务已取消")

            params = VADParams(
                min_silence_duration_us=max(100_000, min_silence_ms * 1000),
                dbfs_threshold=dbfs_threshold,
            )
            method = (
                DetectionMethod.VAD
                if self._vad_available(DetectionMethod.VAD)
                else DetectionMethod.VOLUME
            )
            stage("detect", 0.0, "静音检测中")
            candidates = await self.detect_silence(
                audio_path,
                probe.duration_us,
                params,
                asr_result=transcript,
                method=method,
                progress=_ScopedProgress(sink, base, _WEIGHTS["detect"], "静音检测"),
            )
        finally:
            _unlink_quietly(audio_path)

        base += _WEIGHTS["detect"]
        stage("detect", 1.0, f"检测到 {len(candidates)} 段候选")
        await self.save_candidates(project_id, candidates)
        if sink.should_stop():
            raise PipelineError("任务已取消")

        stage("decide", 0.0, "生成剪辑决策")
        rows, used_fallback = await self.decide(candidates)
        base += _WEIGHTS["decide"]
        suggestions = self.suggestions_from(rows, probe.duration_us)
        new_edl = self.merge_into_edl(edl, suggestions)
        errors = new_edl.validate_full()
        if errors:
            raise PipelineError(f"建议合并后 EDL 校验失败: {errors[:3]}")

        revision_id = await self._projects.save_edl(
            project_id,
            new_edl,
            OperationType.LLM_SUGGEST if not used_fallback else OperationType.DETECT,
            {
                "candidates": len(candidates),
                "suggestions": len(suggestions),
                "used_rule_fallback": used_fallback,
                "asr_skipped_reason": asr_skipped_reason or "",
            },
        )
        base += _WEIGHTS["save"]
        sink.progress(1.0, "分析完成")

        result = {
            "project_id": project_id,
            "revision_id": str(revision_id),
            "duration_us": probe.duration_us,
            "width": probe.width,
            "height": probe.height,
            "candidate_count": len(candidates),
            "suggestion_count": len(suggestions),
            "utterance_count": len(transcript.utterances) if transcript else 0,
            "used_rule_fallback": used_fallback,
            "asr_skipped_reason": asr_skipped_reason,
            "detection_method": (
                candidates[0].detection_method.value if candidates else method.value
            ),
            "edl": new_edl.to_json(),
        }
        logger.info(
            "pipeline_analyze_completed",
            project_id=project_id,
            suggestions=len(suggestions),
            fallback=used_fallback,
        )
        return result

    def _vad_available(self, method: DetectionMethod) -> bool:
        vad = self._providers.vad(method)
        return bool(getattr(vad, "is_available", lambda: False)())

    def _preferred_asr_engine(self) -> ASREngine | None:
        available = self._providers.available_asr_engines()
        return available[0] if available else None

    async def preferences_model_size(self) -> ModelSize:
        from app.persistence.preferences import get_preferences

        prefs = await get_preferences()
        try:
            return ModelSize(prefs.get("asr_model_size", "small"))
        except ValueError:
            return ModelSize.SMALL

    async def preferences_asr_engine(self) -> ASREngine | None:
        from app.persistence.preferences import get_preferences

        prefs = await get_preferences()
        try:
            engine = ASREngine(prefs.get("asr_engine", ASREngine.FASTER_WHISPER.value))
        except ValueError:
            return self._preferred_asr_engine()
        available = self._providers.available_asr_engines()
        return engine if engine in available else (available[0] if available else None)

    # ------------------------------------------------------------------ 持久化

    async def save_transcript(self, project_id: str, result: ASRResult) -> str:
        engine = await get_project_engine(project_id)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            next_revision = int(
                await session.scalar(
                    select(func.max(Transcript.revision)).where(Transcript.engine == result.engine.value)
                )
                or 0
            ) + 1
            record = Transcript(
                id=str(uuid4()),
                engine=result.engine.value,
                language=result.language,
                model_size=result.model_size.value,
                revision=next_revision,
                payload_json=json.dumps(result.to_json(), ensure_ascii=False),
            )
            session.add(record)
            await session.commit()
        logger.info("transcript_saved", project_id=project_id, revision=next_revision)
        return record.id

    async def load_transcript(self, project_id: str) -> ASRResult | None:
        engine = await get_project_engine(project_id)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            record = (
                await session.execute(
                    select(Transcript).order_by(Transcript.created_at.desc()).limit(1)
                )
            ).scalars().first()
        if record is None:
            return None
        return ASRResult.from_json(json.loads(record.payload_json))

    async def load_transcript_overrides(self, project_id: str) -> dict[str, str]:
        engine = await get_project_engine(project_id)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            rows = (
                (await session.execute(select(TranscriptOverride))).scalars().all()
            )
        return {row.utterance_key: row.text for row in rows}

    async def save_transcript_overrides(self, project_id: str, items: dict[str, str]) -> int:
        """写入人工文案；空文本表示撤销该条覆盖，回到机器识别的原文。"""
        engine = await get_project_engine(project_id)
        touched = 0
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            for key, raw in items.items():
                text = raw.strip()
                existing = await session.get(TranscriptOverride, key)
                if not text:
                    if existing is not None:
                        await session.delete(existing)
                        touched += 1
                    continue
                if existing is None:
                    session.add(TranscriptOverride(utterance_key=key, text=text))
                else:
                    existing.text = text
                    existing.updated_at = datetime.now(UTC)
                touched += 1
            await session.commit()
        logger.info("transcript_overrides_saved", project_id=project_id, count=touched)
        return touched

    async def save_candidates(self, project_id: str, candidates: list[SilenceCandidate]) -> None:
        engine = await get_project_engine(project_id)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            # 一轮检测整体替换上一轮：候选是派生数据，累积会让 UI 显示重复区间
            await session.execute(SilenceCandidateRecord.__table__.delete())
            for cand in candidates:
                session.add(
                    SilenceCandidateRecord(
                        id=cand.id,
                        start_us=cand.start_us,
                        end_us=cand.end_us,
                        detection_method=cand.detection_method.value,
                        prev_text=cand.prev_utterance_text,
                        next_text=cand.next_utterance_text,
                        avg_dbfs=cand.avg_dbfs,
                    )
                )
            await session.commit()

    async def load_candidates(self, project_id: str) -> list[SilenceCandidate]:
        engine = await get_project_engine(project_id)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            rows = (
                await session.execute(
                    select(SilenceCandidateRecord).order_by(SilenceCandidateRecord.start_us)
                )
            ).scalars().all()
        return [
            SilenceCandidate(
                id=row.id,
                start_us=row.start_us,
                end_us=row.end_us,
                detection_method=DetectionMethod(row.detection_method),
                prev_utterance_text=row.prev_text,
                next_utterance_text=row.next_text,
                avg_dbfs=row.avg_dbfs,
            )
            for row in rows
        ]


class _ScopedProgress:
    """把阶段内 [0,1] 的进度映射到全局进度区间。"""

    def __init__(self, sink: ProgressSink, base: float, weight: float, label: str) -> None:
        self._sink = sink
        self._base = base
        self._weight = weight
        self._label = label

    def progress(self, value: float, message: str = "") -> None:
        ratio = max(0.0, min(1.0, value))
        self._sink.progress(
            min(1.0, self._base + self._weight * ratio),
            f"{self._label}{'：' + message if message else ''}",
        )

    def should_stop(self) -> bool:
        return self._sink.should_stop()


_pipeline: PipelineService | None = None


def get_pipeline_service() -> PipelineService:
    global _pipeline
    if _pipeline is None:
        from app.providers.registry import get_providers
        from app.services.project_service import get_project_service

        _pipeline = PipelineService(get_project_service(), get_providers())
    return _pipeline


def reset_pipeline_service() -> None:
    global _pipeline
    _pipeline = None
