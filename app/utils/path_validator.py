"""媒体路径安全校验。

防护目标：目录遍历、网络流/UNC 注入、交替数据流（ADS）、8.3 短名绕过、
不支持格式导入、以及把源文件当输出覆写。

信任模型说明：应用运行在用户本机，用户可通过文件对话框选取任意磁盘位置的
媒体，因此这里不做根目录收敛（收敛由调用方按需传 root）；进程间/跨站的访问
控制由 API 鉴权负责。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mts", ".m2ts"}
SUPPORTED_VIDEO_CODECS = {"h264", "hevc", "av1", "h265"}
SUPPORTED_AUDIO_CODECS = {"aac", "mp3", "pcm_s16le", "pcm_s24le", "flac", "opus"}

# 任何带 scheme 的输入都不是本地文件
_SCHEME_PATTERN = re.compile(r"^[a-z][a-z0-9+.\-]{1,15}://", re.IGNORECASE)
# FFmpeg 可将其解释为协议/解封装器的协议前缀（无 :// 形式）
_BARE_PROTOCOLS = ("concat:", "crypto:", "srt:", "rtmp", "pipe:", "gd://", "tcp:", "udp:")
# Windows 保留名（不含扩展名部分）
_WINDOWS_RESERVED = re.compile(
    r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?$", re.IGNORECASE
)
_SHORT_NAME = re.compile(r"~\d+$")
_MAX_FILENAME_LEN = 200

# 输出侧允许扩展名
SUPPORTED_OUTPUT_EXTENSIONS = {
    ".mp4", ".mov", ".mkv", ".webm",
    ".srt", ".ass", ".otio", ".wav", ".txt",
}


class PathValidationError(ValueError):
    pass


def _reject_scheme(path_str: str) -> None:
    if _SCHEME_PATTERN.match(path_str):
        raise PathValidationError(f"不支持网络流或协议地址: {path_str.split('://')[0]}://")
    lowered = path_str.lower()
    for proto in _BARE_PROTOCOLS:
        if lowered.startswith(proto):
            raise PathValidationError(f"不支持网络流地址: {proto}")


def _reject_unc(path: Path) -> None:
    # \\server\share —— 在 Windows 上 is_absolute() 为真，会触发对外 SMB 认证
    parts = path.parts
    if parts and parts[0] in ("\\\\", "\\??"):
        raise PathValidationError("不支持 UNC 网络路径")
    if str(path).startswith("\\\\"):
        raise PathValidationError("不支持 UNC 网络路径")


def _reject_hostile_components(path: Path) -> None:
    """逐组件检查，避免 '..' 子串误杀（如 'my..vlog.mp4'）又漏掉编码变体。"""
    for part in path.parts:
        if part in ("..", "."):
            raise PathValidationError("路径包含目录遍历序列 '../'")
        if "\x00" in part:
            raise PathValidationError("路径包含空字节")
        if _SHORT_NAME.search(part):
            raise PathValidationError("路径包含 8.3 短名，可能绕过校验")
        # 以 '-' 开头的文件名会被 FFmpeg 当成选项（参数注入）
        if part.startswith("-"):
            raise PathValidationError("路径组件不可以下划线连字符 '-' 开头")


def validate_media_path(file_path: str | Path, root: str | Path | None = None) -> Path:
    """校验可读取的媒体文件路径，返回解析后的绝对路径。

    root 非空时额外要求解析结果位于该目录之内（用于上传目录等受控区域）。
    """
    if not file_path or not str(file_path).strip():
        raise PathValidationError("路径为空")

    path_str = os.fspath(file_path)
    if "\x00" in path_str:
        raise PathValidationError("路径包含空字节")

    _reject_scheme(path_str)

    # ':' 仅允许作为 Windows 盘符出现（索引 1），否则视为 ADS 或协议前缀
    if ":" in path_str and not re.match(r"^[A-Za-z]:[\\/]", path_str):
        raise PathValidationError("路径包含非法字符 ':'")

    if root is None and not Path(path_str).is_absolute():
        raise PathValidationError(f"路径必须为绝对路径: {file_path}")

    path = Path(path_str)
    _reject_unc(path)
    _reject_hostile_components(path)

    resolved = Path(os.path.realpath(path))
    _reject_hostile_components(resolved)
    _reject_unc(resolved)

    if root is not None:
        root_resolved = Path(os.path.realpath(root))
        if not resolved.is_relative_to(root_resolved):
            raise PathValidationError(f"路径超出允许范围: {root_resolved}")

    if not resolved.exists():
        raise PathValidationError(f"文件不存在: {resolved}")

    if not resolved.is_file():
        raise PathValidationError(f"路径不是文件: {resolved}")

    ext = resolved.suffix.lower()
    if ext not in SUPPORTED_VIDEO_EXTENSIONS:
        raise PathValidationError(
            f"不支持的文件格式: {ext}。支持的格式: {sorted(SUPPORTED_VIDEO_EXTENSIONS)}"
        )

    return resolved


def sanitize_filename(raw_name: str | None, *, default_stem: str = "upload") -> str:
    """把客户端提供的文件名净化为单一目录下的安全文件名（不创建文件）。"""
    name = (raw_name or "").replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r"[\x00-\x1f<>:\"|?*]", "", name)
    name = name.strip(" .")

    stem = Path(name).name
    if stem in ("", ".", "..") or "\x00" in stem:
        stem = f"{default_stem}.mp4"
    if stem.startswith("-"):
        # 避免 "-foo.mp4" 被下游当作 FFmpeg 选项
        stem = f"_{stem}"
    if _WINDOWS_RESERVED.match(stem):
        stem = f"_{stem}"
    if _SHORT_NAME.search(Path(stem).stem):
        stem = f"_{stem}"
    if len(stem) > _MAX_FILENAME_LEN:
        suffix = Path(stem).suffix.lower()
        if suffix not in SUPPORTED_VIDEO_EXTENSIONS:
            suffix = ".mp4"
        stem = f"{stem[: _MAX_FILENAME_LEN - len(suffix)]}{suffix}"

    ext = Path(stem).suffix.lower()
    if ext and ext not in SUPPORTED_VIDEO_EXTENSIONS:
        raise PathValidationError(
            f"不支持的文件格式: {ext or '无扩展名'}。支持的格式: {sorted(SUPPORTED_VIDEO_EXTENSIONS)}"
        )
    if not ext:
        stem = f"{stem}.mp4"

    return stem


def validate_output_path(
    file_path: str | Path,
    root: str | Path | None = None,
    source_path: str | Path | None = None,
) -> Path:
    """校验写入路径：可选地要求位于 root 内、扩展名合法、且不得覆写源文件。"""
    if not file_path or not str(file_path).strip():
        raise PathValidationError("输出路径为空")

    path_str = os.fspath(file_path)
    _reject_scheme(path_str)
    if ":" in path_str and not re.match(r"^[A-Za-z]:[\\/]", path_str):
        raise PathValidationError("路径包含非法字符 ':'")

    path = Path(path_str)
    if root is not None and not path.is_absolute():
        path = Path(root) / path
    if not path.is_absolute():
        raise PathValidationError(f"输出路径必须为绝对路径: {file_path}")

    _reject_unc(path)
    _reject_hostile_components(path)

    ext = path.suffix.lower()
    if ext not in SUPPORTED_OUTPUT_EXTENSIONS:
        raise PathValidationError(
            f"不支持的输出格式: {ext or '无扩展名'}。支持: {sorted(SUPPORTED_OUTPUT_EXTENSIONS)}"
        )

    # 目录部分需真实存在或由 resolve 规范化，父目录再单独做 root 检查
    resolved_dir = Path(os.path.realpath(path.parent))
    _reject_hostile_components(resolved_dir)
    resolved = resolved_dir / path.name
    _reject_hostile_components(resolved)

    if root is not None:
        root_resolved = Path(os.path.realpath(root))
        if not resolved.is_relative_to(root_resolved):
            raise PathValidationError(f"输出路径超出允许范围: {root_resolved}")

    if resolved.exists() and resolved.is_dir():
        raise PathValidationError(f"路径是目录: {resolved}")

    if source_path is not None:
        same = _same_file(resolved, Path(os.path.realpath(source_path)))
        if same:
            raise PathValidationError("导出路径不能与源文件路径相同")

    return resolved


def _same_file(a: Path, b: Path) -> bool:
    """路径等价判定：Windows 上大小写不敏感，并优先用 file-id 比较。"""
    if os.name == "nt":
        if os.path.normcase(str(a)) == os.path.normcase(str(b)):
            return True
    else:
        if a == b:
            return True

    if a.exists() and b.exists():
        try:
            sa, sb = a.stat(), b.stat()
            return sa.st_dev == sb.st_dev and sa.st_ino == sb.st_ino
        except OSError:
            return False
    return os.path.normcase(str(a)) == os.path.normcase(str(b)) if os.name == "nt" else False


def validate_subtitle_path(file_path: str | Path) -> Path:
    """校验字幕文件路径（会被写进 FFmpeg filtergraph，因此同样做遍历/协议防护）。"""
    if not file_path or not str(file_path).strip():
        raise PathValidationError("字幕路径为空")

    path_str = os.fspath(file_path)
    _reject_scheme(path_str)
    if ":" in path_str and not re.match(r"^[A-Za-z]:[\\/]", path_str):
        raise PathValidationError("路径包含非法字符 ':'")

    path = Path(path_str)
    if not path.is_absolute():
        raise PathValidationError(f"路径必须为绝对路径: {file_path}")

    _reject_unc(path)
    _reject_hostile_components(path)

    resolved = Path(os.path.realpath(path))
    _reject_hostile_components(resolved)

    if resolved.suffix.lower() not in (".ass", ".srt"):
        raise PathValidationError(f"不支持的字幕文件类型: {resolved.suffix}")
    if not resolved.is_file():
        raise PathValidationError(f"字幕文件不存在: {resolved}")

    return resolved


def check_codec_supported(
    codec_video: str | None = None, codec_audio: str | None = None
) -> list[str]:
    warnings: list[str] = []

    if codec_video and codec_video.lower() not in SUPPORTED_VIDEO_CODECS:
        warnings.append(
            f"当前不支持 {codec_video} 视频编码，请转换为 H.264/H.265/AV1 后导入"
        )

    if codec_audio and codec_audio.lower() not in SUPPORTED_AUDIO_CODECS:
        warnings.append(
            f"当前不支持 {codec_audio} 音频编码，请转换为 AAC/MP3/PCM 后导入"
        )

    return warnings


def is_within(path: str | Path, root: str | Path) -> bool:
    """公开的比较助手，供调用方做目录收敛判断（真实 resolve，含符号链接）。"""
    try:
        return Path(os.path.realpath(path)).is_relative_to(Path(os.path.realpath(root)))
    except OSError:  # pragma: no cover - 极端文件系统错误
        return False
