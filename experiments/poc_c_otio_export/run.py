"""POC-C: OTIO 导出 + 官方读取器往返验证。

验收标准: 导出的 .otio 能被官方 opentimelineio 读取器打开，且 clip 顺序与时长和 EDL 一致。
前一版只断言"自己写出的 JSON 里有 OTIO_SCHEMA"，那不是验证：同一份产物官方读取器当时直接报错。

用法: python -m experiments.poc_c_otio_export.run
"""

from __future__ import annotations

import json
from pathlib import Path

from app.models.edl import EDL, EDLSource, Suggestion
from app.models.enums import DecisionSource, SuggestionStatus, SuggestionType
from app.services.edl_service import EDLService
from app.services.otio_export_service import OTIOExportService

PROJECT_ROOT = Path(__file__).resolve().parent


def test_otio_export() -> dict:
    edl = EDL(
        source=EDLSource(uri=str(Path("C:/media/sample.mp4").resolve()), duration_us=10_000_000),
        suggestions=[
            Suggestion(
                id=f"cut-{index}",
                source_start_us=start,
                source_end_us=end,
                suggestion_type=SuggestionType.SILENCE,
                status=SuggestionStatus.ACCEPTED,
                source=DecisionSource.LLM,
            )
            for index, (start, end) in enumerate([(1_000_000, 2_000_000), (4_500_000, 6_000_000)])
        ],
    )

    output_path = PROJECT_ROOT / "test_output.otio"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result_path = OTIOExportService.export_otio(edl, str(output_path))

    with open(result_path, encoding="utf-8") as f:
        data = json.load(f)

    expected = [(iv.source_start_us, iv.duration_us) for iv in EDLService.compute_output_intervals(edl)]
    verdict: dict = {
        "schema": data.get("OTIO_SCHEMA"),
        "file_exists": output_path.exists(),
        "expected_clips": len(expected),
        "reader_available": False,
        "clips_read": None,
        "ranges_match": None,
    }

    try:
        import opentimelineio as otio
    except ImportError:
        return verdict

    verdict["reader_available"] = True
    timeline = otio.adapters.read_from_file(str(output_path))
    clips = list(timeline.tracks[0])
    verdict["clips_read"] = len(clips)
    verdict["ranges_match"] = [
        (int(c.source_range.start_time.value), int(c.source_range.duration.value)) for c in clips
    ] == expected
    return verdict


def generate_report(result: dict, output_path: Path) -> None:
    passed = bool(
        result["reader_available"]
        and result["file_exists"]
        and result["schema"] == "Timeline.1"
        and result["ranges_match"]
    )
    report = {
        "poc": "POC-C",
        "description": "OTIO 导出 + 官方读取器往返验证",
        "results": result,
        "acceptance_criteria": "官方 opentimelineio 读取器能打开产物，clip 顺序与时长与 EDL 完全一致",
        "passed": passed,
        "note": None if result["reader_available"] else "未安装 opentimelineio，无法判定：不是通过",
    }
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    result = test_otio_export()
    generate_report(result, PROJECT_ROOT / "report.json")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
