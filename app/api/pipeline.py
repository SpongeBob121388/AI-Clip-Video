"""剪辑链路端点 —— 分析、EDL 编辑、撤销/重做、字幕、预览与导出。

这一层只做参数校验、鉴权与任务提交；业务编排留在 services 层，
以便同一套逻辑能被测试直接驱动。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.deps import require_auth
from app.config import get_settings
from app.models.asr_result import ASRResult, apply_text_overrides
from app.models.edl import (
    EDL,
    AddInterval,
    MergeIntervals,
    ModifyInterval,
    OutputInterval,
    RemoveInterval,
    SplitInterval,
)
from app.models.enums import (
    Decision,
    DecisionSource,
    DetectionMethod,
    OperationType,
    Resolution,
    SubtitleFormat,
    SubtitleMode,
    SuggestionStatus,
)
from app.models.render_plan import CRF_MAX, CRF_MIN, FPS_OPTIONS, RenderPreset
from app.models.subtitle_config import SubtitleConfig
from app.providers.avatar.protocol import AvatarProviderNotImplementedError
from app.providers.ffmpeg.runner import FFmpegError
from app.providers.registry import get_providers
from app.services.edl_service import EDLService, EDLValidationError
from app.services.job_runner import JobContext, JobWasCancelledError, get_job_runner
from app.services.otio_export_service import OTIOExportError, OTIOExportService
from app.services.pipeline_service import PipelineError, get_pipeline_service
from app.services.preview_cache import PreviewCache
from app.services.project_service import (
    ProjectNotFoundError,
    ProjectServiceError,
    get_project_service,
)
from app.services.render_service import (
    RenderCancelledError,
    RenderExecutor,
    RenderService,
    RenderServiceError,
    RenderVerificationError,
)
from app.services.subtitle_service import SubtitleService, SubtitleServiceError
from app.utils.log_redaction import redact_value
from app.utils.logging import get_logger
from app.utils.path_validator import PathValidationError, validate_output_path

logger = get_logger("api.pipeline")

router = APIRouter(tags=["pipeline"], dependencies=[Depends(require_auth)])

OperationUnion = Annotated[
    AddInterval | RemoveInterval | ModifyInterval | MergeIntervals | SplitInterval,
    Field(discriminator="operation_type"),
]


class EdlResponse(BaseModel):
    project_id: str
    revision_id: str
    edl: dict[str, Any]
    output_intervals: list[dict[str, int]]
    expected_duration_us: int
    can_undo: bool = False
    can_redo: bool = False


class JobSubmitted(BaseModel):
    job_id: str
    project_id: str
    stage: str
    status: str


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: str = Field(default="zh", min_length=2, max_length=16)
    min_silence_ms: int = Field(default=500, ge=100, le=10_000, description="最小静音时长")
    dbfs_threshold: float = Field(default=-40.0, ge=-120.0, le=0.0, description="音量阈值")
    use_asr: bool = Field(default=True, description="是否执行语音识别")
    audio_track_index: int | None = Field(
        default=None,
        ge=0,
        le=31,
        description="用第几条音轨做识别与检测（0 起）；导出选轨时应保持一致",
    )


class DecisionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggestion_ids: Annotated[list[str], Field(min_length=1, max_length=5000)]
    status: Literal["accepted", "rejected", "pending"] = "pending"
    expected_revision_id: UUID | None = Field(
        default=None, description="乐观并发：与当前指针不一致则拒绝"
    )


class OperationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: OperationUnion
    expected_revision_id: UUID | None = Field(
        default=None, description="乐观并发：与当前指针不一致则拒绝，避免覆盖他人编辑"
    )


class SubtitleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: SubtitleFormat = SubtitleFormat.ASS
    config: SubtitleConfig | None = None


class SubtitleResponse(BaseModel):
    content: str
    format: str
    used_srt_fallback: bool
    line_count: int
    message: str = ""


class RenderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preset: RenderPreset | None = None
    subtitle_config: SubtitleConfig | None = None
    output_path: str | None = Field(default=None, max_length=4096, description="导出目标路径")


class CapabilitiesResponse(BaseModel):
    ffmpeg_available: bool
    asr_engines: list[str]
    detection_methods: list[str]
    llm_available: bool
    avatar_available: bool
    avatar_message: str
    otio_available: bool
    subtitle_formats: list[str]
    resolutions: list[str]
    crf_range: list[int]
    fps_options: list[int]
    transcript_editable: bool


class TranscriptResponse(BaseModel):
    available: bool
    utterance_count: int = 0
    language: str = ""
    engine: str = ""
    utterances: list[dict[str, Any]] = Field(default_factory=list, max_length=20000)
    #: 用户改过文案、但当前转录里没有对应区间了（多半是重跑识别后切分变了）
    unapplied_overrides: list[dict[str, Any]] = Field(default_factory=list, max_length=2000)


class TranscriptOverrideItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    time_key: str = Field(
        ..., min_length=3, max_length=64, pattern=r"^\d+-\d+$", description="语句的 start_us-end_us"
    )
    text: str = Field(..., max_length=5000, description="改写后的文案；空串表示恢复自动文本")


class TranscriptOverridesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    overrides: Annotated[list[TranscriptOverrideItem], Field(max_length=2000)]


# --------------------------------------------------------------------- 能力


@router.get("/projects/{project_id}/otio")
async def export_otio(project_id: str) -> dict[str, str]:
    """导出 OTIO 交换文件（给 Premiere / Resolve / Kdenlive 打开的剪辑工程）。

    产物结构由 `tests/e2e/test_otio_reader.py` 用官方 opentimelineio 读取器验证；
    写不出合法交换格式时直接 400，而不是给外部工具一个打不开的文件。
    """
    pid = await _require_project(project_id)
    edl, _ = await _require_edl(pid)
    target = get_settings().render_work_dir / f"{pid}.otio"
    try:
        written = OTIOExportService.export_otio(edl, str(target))
    except OTIOExportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"output_path": written}


@router.get("/capabilities", response_model=CapabilitiesResponse)
async def capabilities() -> CapabilitiesResponse:
    """只报告"真的能跑"的能力；未实现的分支必须显式为不可用。"""
    providers = get_providers()
    avatar_available = False
    try:
        avatar_available = bool(providers.avatar("heygen").is_available())
    except AvatarProviderNotImplementedError as exc:  # pragma: no cover - 探测不应触发
        logger.warning("avatar_probe_unavailable", error=str(exc)[:200])

    # 这里以前硬编码 True：FFmpeg 缺失时前端照样放行渲染，用户看到的是一句渲染失败
    ffmpeg_available = True
    try:
        await providers.ffmpeg.check_version()
    except (FFmpegError, OSError) as exc:
        logger.warning("ffmpeg_probe_failed", error=str(exc)[:200])
        ffmpeg_available = False

    return CapabilitiesResponse(
        ffmpeg_available=ffmpeg_available,
        asr_engines=[engine.value for engine in providers.available_asr_engines()],
        detection_methods=[method.value for method in DetectionMethod],
        llm_available=bool(providers.llm().is_available()),
        avatar_available=avatar_available,
        avatar_message="" if avatar_available else "数字人合成尚未接入真实 API，该功能不可用",
        # 纯标准库产出、且产物已由官方读取器验证（tests/e2e/test_otio_reader.py），
        # 不像 FFmpeg 那样依赖外部可执行文件，因此无需运行时探测
        otio_available=True,
        subtitle_formats=[fmt.value for fmt in SubtitleFormat],
        resolutions=[res.value for res in Resolution],
        crf_range=[CRF_MIN, CRF_MAX],
        fps_options=list(FPS_OPTIONS),
        transcript_editable=True,
    )


# --------------------------------------------------------------------- 分析


@router.post("/projects/{project_id}/analyze", response_model=JobSubmitted)
async def analyze(project_id: str, request: AnalyzeRequest) -> JobSubmitted:
    pid = await _require_project(project_id)

    async def work(ctx: JobContext) -> dict:
        return await get_pipeline_service().analyze(
            pid,
            progress=ctx,
            language=request.language,
            min_silence_ms=request.min_silence_ms,
            dbfs_threshold=request.dbfs_threshold,
            run_asr=request.use_asr,
            audio_track_index=request.audio_track_index,
        )

    job = get_job_runner().start(pid, "analyze", work)
    return _submitted(job.job_id, pid, "analyze", job.status.value)


async def _transcript_response(project_id: str) -> TranscriptResponse:
    """当前转录 + 人工文案覆盖后的视图（不修改库里的原始转录）。"""
    service = get_pipeline_service()
    result = await service.load_transcript(project_id)
    if result is None:
        return TranscriptResponse(available=False)
    overrides = await service.load_transcript_overrides(project_id)
    effective, unapplied_keys = apply_text_overrides(result, overrides)
    return TranscriptResponse(
        available=True,
        utterance_count=len(effective.utterances),
        language=effective.language,
        engine=effective.engine.value,
        utterances=[
            {
                "utterance_id": utt.utterance_id,
                "time_key": utt.time_key,
                "start_us": utt.start_us,
                "end_us": utt.end_us,
                "text": utt.text,
                # 撤销覆盖时要知道回到哪句机器文本上，否则"恢复自动"是个盲操作
                "auto_text": result.utterances[index].text,
                "edited": utt.time_key in overrides,
            }
            for index, utt in enumerate(effective.utterances)
        ],
        unapplied_overrides=[{"time_key": key, "text": overrides[key]} for key in unapplied_keys],
    )


@router.get("/projects/{project_id}/transcript", response_model=TranscriptResponse)
async def get_transcript(project_id: str) -> TranscriptResponse:
    pid = await _require_project(project_id)
    return await _transcript_response(pid)


@router.put("/projects/{project_id}/transcript/overrides", response_model=TranscriptResponse)
async def put_transcript_overrides(
    project_id: str, request: TranscriptOverridesRequest
) -> TranscriptResponse:
    pid = await _require_project(project_id)
    service = get_pipeline_service()
    current = await service.load_transcript(pid)
    if current is None:
        raise HTTPException(status_code=409, detail="尚无语音识别结果，无法编辑字幕文案")

    known = {utt.time_key for utt in current.utterances}
    items: dict[str, str] = {}
    for override in request.overrides:
        if not override.text.strip():
            # 撤销覆盖必须允许：重跑识别后对不上的键也要能清掉，否则残留改不掉
            items[override.time_key] = ""
            continue
        if override.time_key not in known:
            raise HTTPException(
                status_code=409,
                detail=f"时间区间 {override.time_key} 不在当前转录里（可能刚重新识别过），请先刷新字幕列表",
            )
        items[override.time_key] = override.text
    await service.save_transcript_overrides(pid, items)
    return await _transcript_response(pid)


# --------------------------------------------------------------------- EDL


@router.get("/projects/{project_id}/edl", response_model=EdlResponse)
async def read_edl(project_id: str, revision_id: UUID | None = Query(default=None)) -> EdlResponse:
    pid = await _require_project(project_id)
    service = get_project_service()
    if revision_id is None:
        edl, rev = await _require_edl(pid)
    else:
        edl = await service.load_edl(pid, revision_id)
        rev = revision_id
    return await _edl_response(pid, str(rev), edl)


@router.post("/projects/{project_id}/edl/operations", response_model=EdlResponse)
async def apply_operation(project_id: str, request: OperationRequest) -> EdlResponse:
    """应用一个原子编辑操作并生成新修订。"""
    pid = await _require_project(project_id)
    service = get_project_service()
    edl, current = await _require_edl(pid)
    _require_current_revision(current, request.expected_revision_id)

    try:
        new_edl = EDLService.apply_operation(edl, request.operation)
    except EDLValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    rev = await service.save_edl(
        pid,
        new_edl,
        OperationType.MANUAL_EDIT,
        {"operation": request.operation.model_dump(mode="json")},
    )
    return await _edl_response(pid, str(rev), new_edl)


@router.post("/projects/{project_id}/suggestions/decisions", response_model=EdlResponse)
async def set_decisions(project_id: str, request: DecisionsRequest) -> EdlResponse:
    """批量确认/驳回：在一次复制里改完全部条目，只产生一个新修订。"""
    pid = await _require_project(project_id)
    service = get_project_service()
    edl, current = await _require_edl(pid)
    _require_current_revision(current, request.expected_revision_id)

    target_status = SuggestionStatus(request.status)
    selected = set(request.suggestion_ids)
    known = {s.id for s in edl.suggestions}
    missing = sorted(selected - known)
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"{len(missing)} 个建议不存在（首个: {missing[0]}）",
        )

    working = edl.model_copy(deep=True)
    for item in working.suggestions:
        if item.id not in selected:
            continue
        item.status = target_status
        if target_status is SuggestionStatus.ACCEPTED:
            item.decision = Decision.DELETE
        elif target_status is SuggestionStatus.REJECTED:
            item.decision = Decision.KEEP
        item.source = DecisionSource.MANUAL

    rev = await service.save_edl(
        pid,
        working,
        OperationType.MANUAL_EDIT,
        {"bulk_status": request.status, "count": len(selected)},
    )
    return await _edl_response(pid, str(rev), working)


@router.post("/projects/{project_id}/undo", response_model=EdlResponse)
async def undo(
    project_id: str, expected_revision_id: UUID | None = Query(default=None)
) -> EdlResponse:
    pid = await _require_project(project_id)
    manager = await get_project_service().undo_redo(pid)
    # 撤销/重做移动的是全局指针：不校验就会把别人刚做的编辑一起"撤销"掉
    before = await manager.get_current_revision(pid)
    _require_current_revision(before.revision_id if before else None, expected_revision_id)
    try:
        edl = await manager.undo(pid)
    except Exception as exc:
        raise _conflict(exc) from exc
    current = await manager.get_current_revision(pid)
    return await _edl_response(pid, str(current.revision_id) if current else "", edl)


@router.post("/projects/{project_id}/redo", response_model=EdlResponse)
async def redo(
    project_id: str, expected_revision_id: UUID | None = Query(default=None)
) -> EdlResponse:
    pid = await _require_project(project_id)
    manager = await get_project_service().undo_redo(pid)
    before = await manager.get_current_revision(pid)
    _require_current_revision(before.revision_id if before else None, expected_revision_id)
    try:
        edl = await manager.redo(pid)
    except Exception as exc:
        raise _conflict(exc) from exc
    current = await manager.get_current_revision(pid)
    return await _edl_response(pid, str(current.revision_id) if current else "", edl)


# --------------------------------------------------------------------- 字幕


async def _effective_transcript(pipeline: Any, project_id: str) -> ASRResult | None:
    """套用人工文案后的转录视图。

    字幕端点与渲染期都从这里取，两条路径才会一致：预览里看到的改字，导出必须一样。
    库里始终存原始识别结果，覆盖层不落进 transcript 载荷。
    """
    result = await pipeline.load_transcript(project_id)
    if result is None:
        return None
    overrides = await pipeline.load_transcript_overrides(project_id)
    effective, unapplied = apply_text_overrides(result, overrides)
    if unapplied:
        # 重跑识别后区间对不上：必须留痕，否则用户只会觉得"我改的字怎么没了"
        logger.warning(
            "transcript_overrides_unapplied",
            project_id=project_id,
            count=len(unapplied),
            keys=unapplied[:5],
        )
    return effective


@router.post("/projects/{project_id}/subtitles", response_model=SubtitleResponse)
async def subtitles(project_id: str, request: SubtitleRequest) -> SubtitleResponse:
    pid = await _require_project(project_id)
    pipeline = get_pipeline_service()
    edl, _ = await _require_edl(pid)
    asr_result = await _effective_transcript(pipeline, pid)
    if asr_result is None:
        raise HTTPException(
            status_code=409,
            detail="尚无语音识别结果，无法生成字幕；请先完成一次分析（或关闭字幕）",
        )

    intervals = EDLService.compute_output_intervals(edl)
    config = request.config or SubtitleConfig(format=request.format)
    probe = await pipeline.probe_source(pid)
    width, height = _canvas(edl, probe)

    try:
        content, used_fallback = SubtitleService.generate_subtitle(
            asr_result, intervals, config, width=width, height=height
        )
    except SubtitleServiceError as exc:
        raise HTTPException(status_code=500, detail=redact_value(str(exc))) from exc

    return SubtitleResponse(
        content=content,
        format=config.format.value,
        used_srt_fallback=used_fallback,
        line_count=_count_cues(content, config.format),
        message="字幕样式已降级为 SRT" if used_fallback else "",
    )


# --------------------------------------------------------------------- 渲染


@router.post("/projects/{project_id}/preview", response_model=JobSubmitted)
async def preview(project_id: str, request: RenderRequest) -> JobSubmitted:
    pid = await _require_project(project_id)
    preset = _preview_preset(request.preset, request.subtitle_config)
    await _check_audio_track(pid, preset)

    async def work(ctx: JobContext) -> dict:
        return await _render(pid, preset, ctx, is_preview=True)

    job = get_job_runner().start(pid, "preview", work)
    return _submitted(job.job_id, pid, "preview", job.status.value)


@router.post("/projects/{project_id}/export", response_model=JobSubmitted)
async def export(project_id: str, request: RenderRequest) -> JobSubmitted:
    pid = await _require_project(project_id)
    preset = _export_preset(request.preset, request.subtitle_config)
    await _check_audio_track(pid, preset)

    try:
        source = await get_project_service().source_path(pid)
        if request.output_path:
            output = validate_output_path(request.output_path, source_path=source)
        else:
            # 浏览器/无原生对话框的环境拿不到本地路径：落到应用导出目录，
            # 桌面外壳则应通过原生"另存为"给出用户选定的绝对路径
            export_dir = get_settings().export_dir
            export_dir.mkdir(parents=True, exist_ok=True)
            suggested = RenderService.generate_default_filename(Path(source).name)
            output = validate_output_path(
                str(export_dir / suggested), source_path=source
            )
    except PathValidationError as exc:
        raise HTTPException(status_code=400, detail=redact_value(str(exc))) from exc
    RenderService.check_output_path(str(output), source)

    expected_intervals = EDLService.compute_output_intervals((await _require_edl(pid))[0])
    estimate = RenderService.estimate_output_size(
        RenderService.expected_output_duration_us(expected_intervals),
        preset.resolution if preset.resolution in (Resolution.P720, Resolution.P1080)
        else Resolution.P1080,
    )
    free = shutil.disk_usage(_existing_ancestor(output.parent)).free
    try:
        RenderService.check_disk_space(str(output.parent), estimate, free)
    except RenderServiceError as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc

    async def work(ctx: JobContext) -> dict:
        return await _render(pid, preset, ctx, is_preview=False, output_path=str(output))

    job = get_job_runner().start(pid, "export", work)
    return _submitted(job.job_id, pid, "export", job.status.value)


async def _check_audio_track(project_id: str, preset: RenderPreset) -> None:
    """音轨序号来自用户挑选，越界必须在开工前拦住。

    放到渲染中途才炸只会白等几分钟编码，而且 FFmpeg 那句
    "Stream map '0:a:5' matches no streams" 对用户毫无意义。
    """
    if preset.audio_track_index is None:
        return
    probe = await get_pipeline_service().probe_source(project_id)
    count = sum(1 for track in probe.tracks if track.codec_type == "audio")
    if preset.audio_track_index >= count:
        raise HTTPException(
            status_code=409,
            detail=f"源文件只有 {count} 条音轨，无法使用第 {preset.audio_track_index + 1} 条",
        )


async def _render(
    project_id: str,
    preset: RenderPreset,
    ctx: JobContext,
    *,
    is_preview: bool,
    output_path: str | None = None,
) -> dict:
    pipeline = get_pipeline_service()
    service = get_project_service()
    edl, revision_id = await service.load_current_edl(project_id)
    source = await service.source_path(project_id)
    probe = await pipeline.probe_source(project_id)
    intervals = EDLService.compute_output_intervals(edl)

    executor = RenderExecutor(get_providers().ffmpeg, get_settings().render_work_dir)
    plan_id = uuid4()

    subtitle_path = ""
    external_subtitle: Path | None = None
    if preset.subtitle_mode is not SubtitleMode.NONE and not is_preview:
        content = await _subtitle_content(pipeline, project_id, edl, intervals, probe, preset)
        if preset.subtitle_mode is SubtitleMode.BURN_IN:
            subtitle_path = str(await executor.write_subtitle_file(plan_id, content))
        else:
            external_subtitle = _write_external_subtitle(Path(str(output_path)), content, preset)
    elif preset.subtitle_mode is SubtitleMode.BURN_IN and is_preview:
        content = await _subtitle_content(pipeline, project_id, edl, intervals, probe, preset)
        subtitle_path = str(await executor.write_subtitle_file(plan_id, content))

    plan = RenderService.generate_render_plan(
        edl,
        preset,
        revision_id,
        source_file_path=source,
        subtitle_file_path=subtitle_path,
        output_file_path=(Path(output_path).name if output_path else "preview.mp4"),
        has_audio=probe.has_audio,
        plan_id=plan_id,
    )

    cache = PreviewCache() if is_preview else None
    if cache is not None:
        hit = cache.get(plan.plan_hash)
        if hit is not None:
            ctx.progress(1.0, "命中预览缓存")
            return {
                "output_path": str(hit),
                "cached": True,
                "plan_hash": plan.plan_hash,
                "duration_us": RenderService.expected_output_duration_us(intervals),
            }

    # 预览直接写进缓存的暂存名：渲染工作目录会在 execute 收尾时被清掉
    final_output = (
        str(cache.staging_path(plan.plan_hash))
        if cache is not None
        else str(output_path)
    )
    ctx.progress(0.0, "开始渲染")
    try:
        outcome = await executor.execute(
            plan,
            source_path=source,
            output_path=final_output,
            subtitle_path=subtitle_path or None,
            on_progress=lambda value, message: ctx.progress(value, message),
            should_stop=ctx.should_stop,
        )
    except RenderCancelledError as exc:
        if cache is not None:
            cache.discard(plan.plan_hash)
        raise JobWasCancelledError(redact_value(str(exc))) from exc
    except (RenderServiceError, RenderVerificationError) as exc:
        if cache is not None:
            cache.discard(plan.plan_hash)
        raise PipelineError(redact_value(str(exc))) from exc

    if cache is not None:
        stored = cache.promote(plan.plan_hash)
    else:
        stored = outcome.output_path
        await _record_export(project_id, revision_id, plan.plan_hash, str(stored))

    ctx.progress(1.0, "渲染完成")
    payload = {
        "output_path": str(stored),
        "cached": False,
        "plan_hash": plan.plan_hash,
        "duration_us": outcome.duration_us,
        "size_bytes": outcome.size_bytes,
        "width": outcome.width,
        "height": outcome.height,
    }
    if external_subtitle is not None:
        payload["subtitle_path"] = str(external_subtitle)
    return payload


def _write_external_subtitle(
    output: Path, content: str, preset: RenderPreset
) -> Path:
    target = output.with_suffix(_subtitle_suffix(preset))
    validate_output_path(str(target))
    target.write_text(content, encoding="utf-8")
    return target


async def _subtitle_content(
    pipeline: Any,
    project_id: str,
    edl: EDL,
    intervals: list[OutputInterval],
    probe: Any,
    preset: RenderPreset,
) -> str:
    if preset.subtitle_config is None:
        raise PipelineError("字幕模式需要 subtitle_config，请在设置中提供字幕配置")
    asr_result = await _effective_transcript(pipeline, project_id)
    if asr_result is None:
        raise PipelineError("尚无语音识别结果，无法生成字幕（请重新分析或改用无字幕导出）")
    width, height = _canvas(edl, probe)
    content, fell_back = SubtitleService.generate_subtitle(
        asr_result, intervals, preset.subtitle_config, width=width, height=height
    )
    if not content:
        raise PipelineError("字幕内容为空（识别结果未落在任何保留区间内）")
    if fell_back:
        logger.warning("subtitle_fell_back_to_srt", project_id=project_id)
    return content


async def _record_export(project_id: str, revision_id: UUID, plan_hash: str, output_path: str) -> None:
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.persistence.database import get_project_engine
    from app.persistence.models.project_models import ExportRecord

    engine = await get_project_engine(project_id)
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        existing = (
            await session.execute(
                select(ExportRecord).where(ExportRecord.plan_hash == plan_hash).limit(1)
            )
        ).scalars().first()
        if existing is None:
            session.add(
                ExportRecord(
                    id=str(uuid4()),
                    revision_id=str(revision_id),
                    plan_hash=plan_hash,
                    output_path=output_path,
                )
            )
        else:
            existing.output_path = output_path
            existing.revision_id = str(revision_id)
        await session.commit()


def _canvas(edl: EDL, probe: Any) -> tuple[int, int]:
    """字幕 PlayRes：源分辨率优先，否则按输出预设的画布。"""
    if edl.output.resolution is Resolution.SOURCE and probe.width and probe.height:
        return int(probe.width), int(probe.height)
    preset = RenderPreset(
        resolution=edl.output.resolution,
        aspect_ratio=edl.output.aspect_ratio,
        layout_mode=edl.output.layout_mode,
        subtitle_mode=SubtitleMode.BURN_IN,
    )
    width, height = RenderService.target_canvas(preset)
    return int(width), int(height)


def _preview_preset(preset: RenderPreset | None, config: SubtitleConfig | None) -> RenderPreset:
    base = (preset or RenderPreset()).model_copy(deep=True)
    base.resolution = Resolution.P720
    base.preview_quality = True
    if base.subtitle_mode is SubtitleMode.EXTERNAL:
        # 预览没有可附带的外挂文件，烧录与否由字幕开关决定
        base.subtitle_mode = SubtitleMode.NONE
    base.subtitle_config = _subtitle_config_for(base, config)
    return base


def _export_preset(preset: RenderPreset | None, config: SubtitleConfig | None) -> RenderPreset:
    base = (preset or RenderPreset()).model_copy(deep=True)
    base.preview_quality = False
    base.subtitle_config = _subtitle_config_for(base, config)
    return base


def _subtitle_config_for(
    preset: RenderPreset, config: SubtitleConfig | None
) -> SubtitleConfig | None:
    if preset.subtitle_mode is SubtitleMode.NONE:
        return None
    chosen = config or preset.subtitle_config
    return chosen.model_copy(deep=True) if chosen is not None else SubtitleConfig()


def _subtitle_suffix(preset: RenderPreset) -> str:
    fmt = preset.subtitle_config.format if preset.subtitle_config else SubtitleFormat.ASS
    return ".srt" if fmt is SubtitleFormat.SRT else ".ass"


def _count_cues(content: str, fmt: SubtitleFormat) -> int:
    if not content:
        return 0
    return content.count("Dialogue:") if fmt is SubtitleFormat.ASS else content.count(" --> ")


async def _edl_response(project_id: str, revision_id: str, edl: EDL) -> EdlResponse:
    """组装响应，并附带"还能不能撤销/重做"，省掉前端靠本地计数器猜。"""
    intervals = EDLService.compute_output_intervals(edl)
    can_undo = can_redo = False
    if revision_id:
        manager = await get_project_service().undo_redo(project_id)
        can_undo = await manager.can_undo(project_id)
        can_redo = await manager.can_redo(project_id)
    return EdlResponse(
        project_id=project_id,
        revision_id=revision_id,
        edl=edl.to_json(),
        can_undo=can_undo,
        can_redo=can_redo,
        output_intervals=[
            {
                "start_us": iv.start_us,
                "end_us": iv.end_us,
                "source_start_us": iv.source_start_us,
            }
            for iv in intervals
        ],
        expected_duration_us=RenderService.expected_output_duration_us(intervals),
    )


async def _require_project(project_id: str) -> str:
    try:
        pid = str(UUID(project_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="project_id 必须为 UUID") from exc
    try:
        await get_project_service().get_project(pid)
    except (ProjectNotFoundError, ProjectServiceError) as exc:
        raise HTTPException(status_code=404, detail="项目不存在") from exc
    return pid


async def _require_edl(project_id: str):
    """读当前 EDL；新项目还没分析过时返回 409，而不是冒泡成 500。"""
    try:
        return await get_project_service().load_current_edl(project_id)
    except ProjectServiceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _require_current_revision(current: UUID | None, expected: UUID | None) -> None:
    if expected is not None and expected != current:
        raise HTTPException(status_code=409, detail="剪辑版本已变化，请刷新后重试")


def _conflict(exc: Exception) -> HTTPException:
    text = redact_value(str(exc)) or "操作不可用"
    return HTTPException(status_code=409, detail=text[:300])


def _existing_ancestor(path: Path) -> Path:
    """导出目录往往还没创建；disk_usage 只能作用在真实存在的路径上。"""
    current = path
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def _submitted(job_id: UUID, project_id: str, stage: str, status: str) -> JobSubmitted:
    return JobSubmitted(job_id=str(job_id), project_id=project_id, stage=stage, status=status)
