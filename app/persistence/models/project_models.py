"""项目数据库 ORM 模型（project.db）。

约定：
- 时间列一律 `DateTime(timezone=True)`。SQLite 会把带 tz 的值存成字符串再读回 naive，
  因此所有写入用 `datetime.now(UTC)`，比较/排序只在同一列内进行。
- 外键显式声明 ondelete，避免删父行时抛 NO ACTION 异常或留下孤儿行。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class ProjectBase(DeclarativeBase):
    pass


class ProjectMeta(ProjectBase):
    __tablename__ = "project_meta"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(20), default="1.0")
    source_file_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    duration_us: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    time_base: Mapped[str] = mapped_column(String(50), default="1/90000")
    rotation: Mapped[int] = mapped_column(Integer, default=0)
    is_vfr: Mapped[bool] = mapped_column(default=False)
    # undo/redo 的"当前位置"指针；没有它，多次撤销只会反复回到同一层
    current_revision_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("edl_revisions.revision_id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class MediaAsset(ProjectBase):
    __tablename__ = "media_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(50), nullable=False)
    local_relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_us: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fps_num: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fps_den: Mapped[int | None] = mapped_column(Integer, nullable=True)
    codec_video: Mapped[str | None] = mapped_column(String(50), nullable=True)
    codec_audio: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class Transcript(ProjectBase):
    __tablename__ = "transcripts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    engine: Mapped[str] = mapped_column(String(50), nullable=False)
    language: Mapped[str] = mapped_column(String(20), default="zh")
    model_size: Mapped[str] = mapped_column(String(50), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class TranscriptOverride(ProjectBase):
    """人工改写的字幕文案，按 utterance 的时间区间定位，**不改动原始转录**。

    用 `{start_us}-{end_us}` 而不是 `utterance_id` 做键：重跑识别后 id 可能整套变掉，
    时间区间至少还能对上；对不上的覆盖会被报告成"未应用"，而不是静默吃掉用户的改动。
    """

    __tablename__ = "transcript_overrides"

    utterance_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class SilenceCandidateRecord(ProjectBase):
    __tablename__ = "silence_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    start_us: Mapped[int] = mapped_column(Integer, nullable=False)
    end_us: Mapped[int] = mapped_column(Integer, nullable=False)
    detection_method: Mapped[str] = mapped_column(String(50), nullable=False)
    prev_text: Mapped[str] = mapped_column(Text, default="")
    next_text: Mapped[str] = mapped_column(Text, default="")
    avg_dbfs: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("idx_silence_candidates_range", "start_us", "end_us"),)


class SuggestionRecord(ProjectBase):
    __tablename__ = "suggestions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    start_us: Mapped[int] = mapped_column(Integer, nullable=False)
    end_us: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    decision: Mapped[str] = mapped_column(String(50), default="uncertain")
    source: Mapped[str] = mapped_column(String(50), default="llm")
    reason: Mapped[str] = mapped_column(Text, default="")
    prev_text: Mapped[str] = mapped_column(Text, default="")
    next_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        Index("idx_suggestions_decision", "decision"),
        Index("idx_suggestions_source", "source"),
    )


class EDLRevision(ProjectBase):
    __tablename__ = "edl_revisions"

    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(36), nullable=False)
    parent_revision_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("edl_revisions.revision_id", ondelete="RESTRICT"),
        nullable=True,
    )
    edl_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    operation_type: Mapped[str] = mapped_column(String(50), nullable=False)
    operation_detail_json: Mapped[str] = mapped_column(Text, default="{}")
    # 同一项目内的线性序号，历史截断按它而不是按 created_at 比较
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("idx_edl_revisions_parent", "parent_revision_id"),
        Index("idx_edl_revisions_project_created", "project_id", "created_at"),
        Index("idx_edl_revisions_project_number", "project_id", "revision_number"),
    )


class RenderPlanRecord(ProjectBase):
    __tablename__ = "render_plans"

    plan_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_revision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("edl_revisions.revision_id", ondelete="CASCADE"),
        nullable=False,
    )
    preset_json: Mapped[str] = mapped_column(Text, nullable=False)
    ffmpeg_steps_json: Mapped[str] = mapped_column(Text, nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        # plan_hash 是计划的身份，重复生成不应产生第二行
        UniqueConstraint("plan_hash", name="uq_render_plans_hash"),
        Index("idx_render_plans_revision", "project_revision_id"),
    )


class RenderJobRecord(ProjectBase):
    __tablename__ = "render_jobs"

    job_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("render_plans.plan_id", ondelete="CASCADE"),
        nullable=False,
    )
    project_id: Mapped[str] = mapped_column(String(36), nullable=False, default="")
    stage: Mapped[str] = mapped_column(String(50), nullable=False, default="render")
    status: Mapped[str] = mapped_column(String(50), default="pending")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    message: Mapped[str] = mapped_column(Text, default="")
    cancel_requested: Mapped[int] = mapped_column(Integer, default=0)
    output_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        Index("idx_render_jobs_plan", "plan_id"),
        Index("idx_render_jobs_status", "status"),
    )


class UndoRedoHistory(ProjectBase):
    __tablename__ = "undo_redo_history"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    # 没有 project_id 时，redo 栈会跨项目取出别人的版本并改错项目
    project_id: Mapped[str] = mapped_column(String(36), nullable=False, default="")
    revision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("edl_revisions.revision_id", ondelete="CASCADE"),
        nullable=False,
    )
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        Index("idx_undo_redo_project", "project_id", "created_at"),
        Index("idx_undo_redo_revision", "revision_id"),
    )


class ExportRecord(ProjectBase):
    __tablename__ = "export_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("edl_revisions.revision_id", ondelete="CASCADE"),
        nullable=False,
    )
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    output_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("idx_export_created", "created_at"),)
