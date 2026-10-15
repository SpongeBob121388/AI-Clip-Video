"""OTIO 导出服务的结构与时基测试（不依赖官方读取器，读取器验证见 tests/e2e/test_otio_reader.py）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models.edl import EDL, EDLSource, Suggestion
from app.models.enums import DecisionSource, SuggestionStatus, SuggestionType
from app.services.edl_service import EDLService
from app.services.otio_export_service import OTIOExportError, OTIOExportService, to_media_url

pytestmark = pytest.mark.unit


def _make_edl(cuts: list[tuple[int, int]] | None = None, duration_us: int = 10_000_000) -> EDL:
    return EDL(
        source=EDLSource(uri=r"C:\media\test.mp4", duration_us=duration_us),
        suggestions=[
            Suggestion(
                id=f"cut-{index}",
                source_start_us=start,
                source_end_us=end,
                suggestion_type=SuggestionType.SILENCE,
                status=SuggestionStatus.ACCEPTED,
                source=DecisionSource.LLM,
            )
            for index, (start, end) in enumerate(cuts or [])
        ],
    )


def _track(data: dict) -> dict:
    return data["tracks"]["children"][0]


class TestUrlConversion:
    def test_windows_path_becomes_file_url(self) -> None:
        assert to_media_url(r"C:\media\test.mp4") == "file:///C:/media/test.mp4"

    def test_existing_url_passes_through(self) -> None:
        assert to_media_url("file:///C:/media/test.mp4") == "file:///C:/media/test.mp4"
        assert to_media_url("http://host/x.mp4") == "http://host/x.mp4"

    def test_relative_path_rejected(self) -> None:
        """相对路径写进 target_url 会让外部编辑器按各自的工程目录猜，必须当场拒绝。"""
        with pytest.raises(OTIOExportError, match="绝对路径"):
            to_media_url("media/test.mp4")

    def test_empty_uri_rejected(self) -> None:
        with pytest.raises(OTIOExportError, match="源文件"):
            to_media_url("")


class TestExport:
    def test_export_creates_file(self, tmp_path: Path) -> None:
        output = tmp_path / "nested" / "export.otio"
        result = OTIOExportService.export_otio(_make_edl(), str(output))
        assert output.exists()
        assert result == str(output)

    def test_write_leaves_no_temp_file(self, tmp_path: Path) -> None:
        output = tmp_path / "export.otio"
        OTIOExportService.export_otio(_make_edl(), str(output))
        assert [p.name for p in tmp_path.iterdir()] == ["export.otio"]

    def test_timeline_shape(self, tmp_path: Path) -> None:
        output = tmp_path / "export.otio"
        OTIOExportService.export_otio(_make_edl(), str(output))
        data = json.loads(output.read_text(encoding="utf-8"))
        assert data["OTIO_SCHEMA"] == "Timeline.1"
        assert data["tracks"]["OTIO_SCHEMA"] == "Stack.1"
        track = _track(data)
        assert track["OTIO_SCHEMA"] == "Track.1"
        assert track["kind"] == "Video"
        assert track["children"], "至少要有一个 clip"
        for clip in track["children"]:
            assert clip["OTIO_SCHEMA"] == "Clip.2"
            assert clip["active_media_reference_key"] == "DEFAULT_MEDIA"
            reference = clip["media_references"]["DEFAULT_MEDIA"]
            assert reference["OTIO_SCHEMA"] == "ExternalReference.1"
            assert reference["target_url"] == "file:///C:/media/test.mp4"

    def test_time_base_is_microseconds_and_exact(self, tmp_path: Path) -> None:
        edl = _make_edl([(2_520_000, 4_000_000)])
        output = tmp_path / "export.otio"
        OTIOExportService.export_otio(edl, str(output))
        data = json.loads(output.read_text(encoding="utf-8"))
        clips = _track(data)["children"]
        assert [(c["source_range"]["start_time"]["value"], c["source_range"]["duration"]["value"])
                for c in clips] == [
                    (iv.source_start_us, iv.duration_us)
                    for iv in EDLService.compute_output_intervals(edl)
                ]
        assert all(c["source_range"]["start_time"]["rate"] == 1_000_000.0 for c in clips)

    def test_no_cuts_yields_single_full_length_clip(self, tmp_path: Path) -> None:
        output = tmp_path / "export.otio"
        OTIOExportService.export_otio(_make_edl(duration_us=6_000_000), str(output))
        data = json.loads(output.read_text(encoding="utf-8"))
        clips = _track(data)["children"]
        assert len(clips) == 1
        assert clips[0]["source_range"]["duration"]["value"] == 6_000_000

    def test_available_range_reports_source_length(self, tmp_path: Path) -> None:
        output = tmp_path / "export.otio"
        OTIOExportService.export_otio(_make_edl([(1_000_000, 2_000_000)], duration_us=9_000_000), str(output))
        data = json.loads(output.read_text(encoding="utf-8"))
        for clip in _track(data)["children"]:
            ref = clip["media_references"]["DEFAULT_MEDIA"]
            assert ref["available_range"]["duration"]["value"] == 9_000_000
            assert ref["available_range"]["start_time"]["value"] == 0
