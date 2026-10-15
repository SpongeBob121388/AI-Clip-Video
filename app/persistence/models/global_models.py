"""全局数据库 ORM 模型（app.db）。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, Numeric, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _now() -> datetime:
    return datetime.now(UTC)


class GlobalBase(DeclarativeBase):
    pass


class ProjectRecord(GlobalBase):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    db_path: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )
    last_opened_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    thumbnail_path: Mapped[str | None] = mapped_column(Text, nullable=True)


class AppSetting(GlobalBase):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(255), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(100), default="general")


class CostRecord(GlobalBase):
    __tablename__ = "cost_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    # 成本用定点数：Float 累加会在长会话里产生肉眼可见的账目误差
    quantity: Mapped[float] = mapped_column(Numeric(18, 6), default=0.0)
    estimated_cost: Mapped[float] = mapped_column(Numeric(18, 6), default=0.0)
    actual_cost: Mapped[float] = mapped_column(Numeric(18, 6), default=0.0)
    currency: Mapped[str] = mapped_column(String(10), default="CNY")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class AvatarProfile(GlobalBase):
    __tablename__ = "avatar_profiles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    provider_id: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_profile_ref: Mapped[str] = mapped_column(Text, nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class RecentProject(GlobalBase):
    __tablename__ = "recent_projects"

    project_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    last_opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now
    )
    display_order: Mapped[int] = mapped_column(Integer, default=0)
