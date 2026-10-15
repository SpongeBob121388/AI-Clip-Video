"""字幕配置模型 — 白名单样式字段，防止任意 FFmpeg filter 注入。

颜色值格式：&H00BBGGRR（ASS 标准，BGR 顺序，&H 前缀）。
alignment：1-9（ASS numpad 布局，1=左下，5=居中，9=右上）。
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.enums import SubtitleFormat

_COLOR_PATTERN = re.compile(r"^&H[0-9A-Fa-f]{8}$")
_ALLOWED_FONTS = {
    "Microsoft YaHei",
    "SimHei",
    "SimSun",
    "Noto Sans SC",
    "PingFang SC",
    "Source Han Sans SC",
    "Arial",
    "sans-serif",
}


class SubtitleStyle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    font_name: str = Field(
        default="Microsoft YaHei", min_length=1, max_length=100, description="字体名称"
    )
    font_size: int = Field(default=48, description="字号", ge=8, le=200)
    primary_color: str = Field(default="&H00FFFFFF", description="主颜色 &H00BBGGRR")
    outline_color: str = Field(default="&H00000000", description="描边颜色")
    bold: bool = Field(default=False, description="粗体")
    italic: bool = Field(default=False, description="斜体")
    outline: float = Field(default=2.0, description="描边宽度", ge=0.0, le=10.0)
    shadow: float = Field(default=1.0, description="阴影宽度", ge=0.0, le=10.0)
    alignment: int = Field(default=2, description="对齐方式 1-9", ge=1, le=9)
    margin_l: int = Field(default=20, description="左边距", ge=0, le=500)
    margin_r: int = Field(default=20, description="右边距", ge=0, le=500)
    margin_v: int = Field(default=40, description="垂直边距", ge=0, le=500)

    @field_validator("primary_color", "outline_color")
    @classmethod
    def validate_color(cls, v: str) -> str:
        if not _COLOR_PATTERN.match(v):
            raise ValueError(f"颜色值必须为 &H00BBGGRR 格式，得到: {v}")
        return v

    @field_validator("font_name")
    @classmethod
    def validate_font(cls, v: str) -> str:
        if v not in _ALLOWED_FONTS:
            raise ValueError(
                f"字体不在白名单中: {v}。允许: {sorted(_ALLOWED_FONTS)}"
            )
        return v


class SubtitleLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_us: int = Field(..., description="输出时间轴起始时间（微秒）", ge=0)
    end_us: int = Field(..., description="输出时间轴结束时间（微秒）", ge=0)
    text: str = Field(..., max_length=500, description="字幕文本")

    @model_validator(mode="after")
    def validate_time_range(self) -> SubtitleLine:
        if self.end_us <= self.start_us:
            raise ValueError("end_us 必须大于 start_us")
        return self

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us


class SubtitleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: SubtitleFormat = Field(default=SubtitleFormat.ASS, description="字幕格式")
    style: SubtitleStyle = Field(default_factory=SubtitleStyle, description="字幕样式")
    safe_margin_horizontal: float = Field(
        default=0.1, description="水平安全边距比例", ge=0.0, le=0.5
    )
    safe_margin_vertical: float = Field(
        default=0.1, description="垂直安全边距比例", ge=0.0, le=0.5
    )
    max_chars_per_line: int = Field(
        default=20, description="每行最大字符数（中文）", ge=5, le=100
    )
