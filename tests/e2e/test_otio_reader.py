"""用官方 OpenTimelineIO 读取器验证导出产物。

转换器自己读自己不算验证：单元测试只能证明 JSON 形状，只有官方读取器接受、并且
读回来的时间线与我们写出去的一致，才谈得上"能在 Premiere/Resolve 里打开"。
没装 opentimelineio 时整组 skip —— 宁可没有结论，也不要假装通过。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models.edl import EDL, EDLSource, Suggestion
from app.models.enums import DecisionSource, SuggestionStatus, SuggestionType
from app.services.edl_service import EDLService
from app.services.otio_export_service import OTIOExportService

pytestmark = pytest.mark.e2e

otio = pytest.importorskip("opentimelineio", reason="需要官方读取器才能验证交换格式")

SOURCE = r"C:\media\timeline_clip.mp4"
SOURCE_URL = "file:///C:/media/timeline_clip.mp4"


def _edl(cuts: list[tuple[int, int]], duration_us: int = 8_000_000) -> EDL:
    return EDL(
        source=EDLSource(uri=SOURCE, duration_us=duration_us),
        suggestions=[
            Suggestion(
                id=f"cut-{index}",
                source_start_us=start,
                source_end_us=end,
                suggestion_type=SuggestionType.SILENCE,
                status=SuggestionStatus.ACCEPTED,
                source=DecisionSource.LLM,
            )
            for index, (start, end) in enumerate(cuts)
        ],
    )


def _clips(timeline) -> list:
    return list(timeline.tracks[0])


def test_reader_accepts_export_and_preserves_ranges(tmp_path: Path) -> None:
    edl = _edl([(2_520_000, 4_000_000), (5_000_000, 6_500_000)])
    target = tmp_path / "export.otio"
    OTIOExportService.export_otio(edl, str(target))

    timeline = otio.core.deserialize_json_from_file(str(target))
    intervals = EDLService.compute_output_intervals(edl)

    assert timeline.tracks[0].kind == otio.schema.Track.Kind.Video
    clips = _clips(timeline)
    assert len(clips) == len(intervals)
    for clip, interval in zip(clips, intervals, strict=True):
        start = clip.source_range.start_time
        duration = clip.source_range.duration
        # 微秒作时间基：读回来的整数必须与 EDL 完全相等，不允许有损换算
        assert start.rate == 1_000_000.0
        assert int(start.value) == interval.source_start_us
        assert int(duration.value) == interval.duration_us
        reference = clip.media_references()[clip.active_media_reference_key]
        assert reference.target_url == SOURCE_URL
        assert int(reference.available_range.duration.value) == edl.source.duration_us


def test_reader_preserves_clip_order(tmp_path: Path) -> None:
    """SPEC 的 POC-C 验收标准：round-trip 保留 clip order 与 duration。"""
    cuts = [(i * 400_000 + 50_000, i * 400_000 + 250_000) for i in range(30)]
    edl = _edl(cuts, duration_us=30_000_000)
    target = tmp_path / "ordered.otio"
    OTIOExportService.export_otio(edl, str(target))

    written = EDLService.compute_output_intervals(edl)
    timeline = otio.core.deserialize_json_from_file(str(target))
    read_back = [(int(c.source_range.start_time.value), int(c.source_range.duration.value))
                 for c in _clips(timeline)]
    assert read_back == [(i.source_start_us, i.end_us - i.start_us) for i in written]
    assert [c.name for c in _clips(timeline)] == [f"clip {n + 1}" for n in range(len(read_back))]


def test_adapter_reads_the_file_by_suffix(tmp_path: Path) -> None:
    """`.otio` 后缀走官方 otio_json 适配器，这是外部工具真正的入口。"""
    edl = _edl([(1_000_000, 2_000_000)])
    target = tmp_path / "via_adapter.otio"
    OTIOExportService.export_otio(edl, str(target))
    timeline = otio.adapters.read_from_file(str(target))
    assert len(_clips(timeline)) == 2


def test_exported_fields_match_official_serialization(tmp_path: Path) -> None:
    """读进来再按库自己的方式序列化，字段集合必须和我们写出的完全一致。

    这条比逐字段手抄断言更严：多写、漏写、拼错字段名都会在这里暴露。
    """
    edl = _edl([(2_520_000, 4_000_000), (5_000_000, 6_500_000)])
    target = tmp_path / "shape.otio"
    OTIOExportService.export_otio(edl, str(target))

    ours = json.loads(target.read_text(encoding="utf-8"))
    resaved = json.loads(
        otio.core.serialize_json_to_string(otio.core.deserialize_json_from_file(str(target)), indent=2)
    )

    def diff(left, right, path="$"):
        if isinstance(left, dict) and isinstance(right, dict):
            out = [f"{path}.{key} 只在官方输出里" for key in right if key not in left]
            out += [f"{path}.{key} 只在我们的输出里" for key in left if key not in right]
            for key in set(left) & set(right):
                out += diff(left[key], right[key], f"{path}.{key}")
            return out
        if isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                return [f"{path} 长度 {len(left)} != {len(right)}"]
            return [d for i, (x, y) in enumerate(zip(left, right, strict=True)) for d in diff(x, y, f"{path}[{i}]")]
        return [] if left == right else [f"{path}: {left!r} != {right!r}"]

    assert diff(ours, resaved) == []
