"""全局枚举类型定义。

所有枚举值与 spec.md 第 6 章数据约束和 design.md 2.3.2.2 完全一致。
"""

from __future__ import annotations

from enum import Enum


class SuggestionType(str, Enum):
    SILENCE = "silence"
    FILLER_WORD = "filler_word"
    REPETITION = "repetition"
    PAUSE = "pause"
    #: 用户在时间轴上手工加/合并的区间。不能冒充成 silence，否则列表里会把手工剪辑
    #: 显示成"静音建议"，也会让按类型统计的功能出错。
    MANUAL = "manual"


class Decision(str, Enum):
    DELETE = "delete"
    KEEP = "keep"
    UNCERTAIN = "uncertain"


class DecisionSource(str, Enum):
    LLM = "llm"
    RULE = "rule"
    MANUAL = "manual"


class OperationType(str, Enum):
    IMPORT = "import"
    ASR = "asr"
    DETECT = "detect"
    LLM_SUGGEST = "llm_suggest"
    MANUAL_EDIT = "manual_edit"
    UNDO = "undo"
    REDO = "redo"
    RENDER = "render"


class EditOperationType(str, Enum):
    """EDL 原子编辑操作的判别符（与修订记录的 OperationType 不同域）。"""

    ADD = "add"
    REMOVE = "remove"
    MODIFY = "modify"
    MERGE = "merge"
    SPLIT = "split"


class Resolution(str, Enum):
    P720 = "720p"
    P1080 = "1080p"
    SOURCE = "source"


class SubtitleMode(str, Enum):
    BURN_IN = "burn_in"
    EXTERNAL = "external"
    NONE = "none"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ASREngine(str, Enum):
    FASTER_WHISPER = "faster_whisper"
    WHISPERX = "whisperx"
    QWEN_ASR = "qwen_asr"


class ModelSize(str, Enum):
    TINY = "tiny"
    BASE = "base"
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


class DetectionMethod(str, Enum):
    VAD = "vad"
    VOLUME = "volume"
    COMBINED = "combined"


class SubtitleFormat(str, Enum):
    ASS = "ass"
    SRT = "srt"


class SynthesisStatus(str, Enum):
    PENDING = "pending"
    SYNTHESIZING = "synthesizing"
    COMPLETED = "completed"
    FAILED = "failed"


class AspectRatio(str, Enum):
    HORIZONTAL = "16:9"
    VERTICAL = "9:16"


class LayoutMode(str, Enum):
    SMART_CROP = "smart_crop"
    BLUR_BACKGROUND = "blur_background"


class SuggestionStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    MODIFIED = "modified"


class SuggestionStrength(str, Enum):
    STANDARD = "standard"
    AGGRESSIVE = "aggressive"
    CONSERVATIVE = "conservative"
