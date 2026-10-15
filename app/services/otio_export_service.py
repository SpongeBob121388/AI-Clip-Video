"""OTIO 导出服务 — EDL → OpenTimelineIO 交换文件。

产物结构以官方 `opentimelineio` 0.18.1 自己序列化的结果为准（见
`tests/e2e/test_otio_reader.py`：那份测试用真实读取器打开这里写出的文件并逐字段比对）。
本模块不依赖 opentimelineio，运行时只需要标准库，打包时也就不会引入 C++ 扩展。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.models.edl import EDL
from app.services.edl_service import EDLService
from app.utils.logging import get_logger

logger = get_logger("services.otio_export")

# EDL 全程用整数微秒；把微秒直接当 OTIO 的时间基，导出与回读都不会出现有损换算
OTIO_RATE = 1_000_000.0
MEDIA_REFERENCE_KEY = "DEFAULT_MEDIA"


class OTIOExportError(RuntimeError):
    pass


def _rational_time(us: int) -> dict[str, Any]:
    return {"OTIO_SCHEMA": "RationalTime.1", "rate": OTIO_RATE, "value": int(us)}


def _time_range(start_us: int, duration_us: int) -> dict[str, Any]:
    return {
        "OTIO_SCHEMA": "TimeRange.1",
        "start_time": _rational_time(start_us),
        "duration": _rational_time(duration_us),
    }


def to_media_url(uri: str) -> str:
    """EDL 里存的是本地路径，OTIO 的 target_url 必须是 URL（`file:///C:/...`）。

    直接写 `C:\\media\\x.mp4` 时 Premiere/Resolve 侧的解析结果不受控，
    官方 url_utils 也只认 URL 形式。
    """
    if not uri:
        raise OTIOExportError("EDL 缺少源文件 URI，无法生成可交换的 OTIO")
    scheme = uri.split(":", 1)[1] if ":" in uri else ""
    if scheme.startswith("//"):  # 已经是 file:// / http:// 之类的 URL
        return uri
    path = Path(uri).expanduser()
    if not path.is_absolute():
        raise OTIOExportError(f"EDL 源路径不是绝对路径，无法转成 URL: {uri}")
    return path.resolve().as_uri()


def _external_reference(url: str, duration_us: int) -> dict[str, Any]:
    return {
        "OTIO_SCHEMA": "ExternalReference.1",
        "metadata": {},
        "name": "",
        "available_range": _time_range(0, duration_us),
        "available_image_bounds": None,
        "target_url": url,
    }


def _clip(name: str, source_start_us: int, duration_us: int, reference: dict[str, Any]) -> dict[str, Any]:
    return {
        "OTIO_SCHEMA": "Clip.2",
        "metadata": {},
        "name": name,
        "source_range": _time_range(source_start_us, duration_us),
        "effects": [],
        "markers": [],
        "enabled": True,
        "color": None,
        "media_references": {MEDIA_REFERENCE_KEY: reference},
        "active_media_reference_key": MEDIA_REFERENCE_KEY,
    }


class OTIOExportService:
    @staticmethod
    def export_otio(edl: EDL, output_path: str = "export.otio") -> str:
        """把 EDL 的保留片段写成 OTIO 单轨时间线，返回落盘路径。"""
        timeline = OTIOExportService._convert_edl_to_otio(edl)
        OTIOExportService._write_otio_file(timeline, output_path)
        logger.info("otio_exported", output_path=output_path, clips=len(timeline["tracks"]["children"][0]["children"]))
        return output_path

    @staticmethod
    def _convert_edl_to_otio(edl: EDL) -> dict[str, Any]:
        intervals = EDLService.compute_output_intervals(edl)
        if not intervals:
            raise OTIOExportError("EDL 没有可导出的保留片段")

        reference = _external_reference(to_media_url(edl.source.uri), edl.source.duration_us)
        clips = [
            _clip(f"clip {index + 1}", iv.source_start_us, iv.duration_us, reference)
            for index, iv in enumerate(intervals)
        ]

        return {
            "OTIO_SCHEMA": "Timeline.1",
            "metadata": {
                "auto_clip_video": {
                    "source_duration_us": edl.source.duration_us,
                    "kept_duration_us": sum(iv.duration_us for iv in intervals),
                }
            },
            "name": "auto_clip_video_export",
            "global_start_time": None,
            "tracks": {
                "OTIO_SCHEMA": "Stack.1",
                "metadata": {},
                "name": "tracks",
                "source_range": None,
                "effects": [],
                "markers": [],
                "enabled": True,
                "color": None,
                "children": [
                    {
                        "OTIO_SCHEMA": "Track.1",
                        "metadata": {},
                        "name": "V1",
                        "source_range": None,
                        "effects": [],
                        "markers": [],
                        "enabled": True,
                        "color": None,
                        "children": clips,
                        "kind": "Video",
                    }
                ],
            },
        }

    @staticmethod
    def _write_otio_file(timeline: dict[str, Any], output_path: str) -> None:
        target = Path(output_path)
        if target.parent != Path(""):
            target.parent.mkdir(parents=True, exist_ok=True)
        # 半截文件比导出失败更糟：先写 .tmp 再原子替换
        temp = target.with_name(target.name + ".tmp")
        with temp.open("w", encoding="utf-8") as handle:
            json.dump(timeline, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
