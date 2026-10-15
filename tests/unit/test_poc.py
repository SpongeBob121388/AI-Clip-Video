"""POC 实验框架测试。

验证 POC 代码可运行且不被产品运行时 import。
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class TestPOCStructure:
    def test_poc_a_exists(self) -> None:
        assert (PROJECT_ROOT / "experiments" / "poc_a_silence_detection" / "run.py").exists()

    def test_poc_b_exists(self) -> None:
        assert (PROJECT_ROOT / "experiments" / "poc_b_ffmpeg_subtitle" / "run.py").exists()

    def test_poc_c_exists(self) -> None:
        assert (PROJECT_ROOT / "experiments" / "poc_c_otio_export" / "run.py").exists()

    def test_poc_d_exists(self) -> None:
        assert (PROJECT_ROOT / "experiments" / "poc_d_kdenlive" / "run.py").exists()

    def test_poc_e_exists(self) -> None:
        assert (PROJECT_ROOT / "experiments" / "poc_e_reproducible_build" / "run.py").exists()

    def test_adr_exists(self) -> None:
        assert (PROJECT_ROOT / "docs" / "adr.md").exists()


class TestPOCAF1Computation:
    def test_compute_f1_perfect_match(self) -> None:
        from experiments.poc_a_silence_detection.run import compute_f1

        pred = [(1_000_000, 2_000_000)]
        gt = [(1_000_000, 2_000_000)]
        p, r, f1 = compute_f1(pred, gt)
        assert p == 1.0
        assert r == 1.0
        assert f1 == 1.0

    def test_compute_f1_no_match(self) -> None:
        from experiments.poc_a_silence_detection.run import compute_f1

        pred = [(1_000_000, 2_000_000)]
        gt = [(5_000_000, 6_000_000)]
        p, r, f1 = compute_f1(pred, gt)
        assert p == 0.0
        assert r == 0.0
        assert f1 == 0.0

    def test_compute_f1_empty(self) -> None:
        from experiments.poc_a_silence_detection.run import compute_f1

        p, r, f1 = compute_f1([], [])
        assert f1 == 1.0

    def test_compute_f1_partial(self) -> None:
        from experiments.poc_a_silence_detection.run import compute_f1

        pred = [(1_000_000, 2_000_000), (3_000_000, 4_000_000)]
        gt = [(1_000_000, 2_000_000)]
        p, r, f1 = compute_f1(pred, gt)
        assert p == 0.5
        assert r == 1.0
        assert f1 == pytest.approx(2 / 3)


class TestPOCBChinesePunctuation:
    def test_chinese_punctuation_ok(self) -> None:
        from experiments.poc_b_ffmpeg_subtitle.run import test_chinese_punctuation

        result = test_chinese_punctuation()
        assert result is True


class TestPOCCOTIOExport:
    def test_otio_export_round_trips_through_the_official_reader(self) -> None:
        """POC 的结论必须由真实读取器给出：没装 opentimelineio 就是"未验证"，不能算通过。"""
        pytest.importorskip("opentimelineio", reason="POC-C 的验收依据是官方读取器")
        from experiments.poc_c_otio_export.run import test_otio_export

        result = test_otio_export()
        assert result["schema"] == "Timeline.1"
        assert result["file_exists"] is True
        assert result["reader_available"] is True
        assert result["clips_read"] == result["expected_clips"]
        assert result["ranges_match"] is True


class TestPOCERehash:
    def test_compute_file_hash(self, tmp_path) -> None:
        from experiments.poc_e_reproducible_build.run import compute_file_hash

        test_file = tmp_path / "test.bin"
        test_file.write_bytes(b"hello world")
        h = compute_file_hash(test_file)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)
