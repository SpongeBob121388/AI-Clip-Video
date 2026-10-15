"""RenderPlan 生成与 Hash 单元测试。"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.models.edl import EDL, EDLSource, Suggestion
from app.models.enums import (
    AspectRatio,
    Decision,
    DecisionSource,
    LayoutMode,
    Resolution,
    SubtitleMode,
    SuggestionStatus,
    SuggestionType,
)
from app.models.render_plan import CRF_MAX, CRF_MIN, RenderPlan, RenderPreset
from app.services.render_service import RenderService, RenderServiceError


def _make_edl(
    duration_us: int = 10_000_000,
    suggestions: list[Suggestion] | None = None,
    rotation: int = 0,
) -> EDL:
    return EDL(
        source=EDLSource(uri="test.mp4", duration_us=duration_us, rotation=rotation),
        suggestions=suggestions or [],
    )


def _make_accepted_suggestion(
    start_us: int = 2_000_000,
    end_us: int = 3_000_000,
) -> Suggestion:
    return Suggestion(
        id=f"cut-{uuid4().hex[:8]}",
        source_start_us=start_us,
        source_end_us=end_us,
        suggestion_type=SuggestionType.SILENCE,
        decision=Decision.DELETE,
        source=DecisionSource.MANUAL,
        status=SuggestionStatus.ACCEPTED,
    )


@pytest.mark.unit
class TestRenderPlanGeneration:
    def test_basic_plan_generation(self) -> None:
        edl = _make_edl()
        preset = RenderPreset()
        rev_id = uuid4()
        plan = RenderService.generate_render_plan(
            edl, preset, rev_id, source_file_path="src.mp4", output_file_path="out.mp4"
        )
        assert plan.project_revision_id == rev_id
        assert len(plan.ffmpeg_steps) == 1
        assert plan.plan_hash != ""

    def test_plan_hash_deterministic(self) -> None:
        edl = _make_edl()
        preset = RenderPreset()
        rev_id = uuid4()
        plan1 = RenderService.generate_render_plan(
            edl, preset, rev_id, "src.mp4", "sub.ass", "out.mp4"
        )
        plan2 = RenderService.generate_render_plan(
            edl, preset, rev_id, "src.mp4", "sub.ass", "out.mp4"
        )
        assert plan1.plan_hash == plan2.plan_hash

    def test_different_preset_different_hash(self) -> None:
        edl = _make_edl()
        rev_id = uuid4()
        preset1 = RenderPreset(resolution=Resolution.P720)
        preset2 = RenderPreset(resolution=Resolution.P1080)
        plan1 = RenderService.generate_render_plan(
            edl, preset1, rev_id, "src.mp4", "", "out.mp4"
        )
        plan2 = RenderService.generate_render_plan(
            edl, preset2, rev_id, "src.mp4", "", "out.mp4"
        )
        assert plan1.plan_hash != plan2.plan_hash

    def test_different_edl_different_hash(self) -> None:
        edl1 = _make_edl()
        edl2 = _make_edl(suggestions=[_make_accepted_suggestion()])
        preset = RenderPreset()
        rev_id = uuid4()
        plan1 = RenderService.generate_render_plan(
            edl1, preset, rev_id, "src.mp4", "", "out.mp4"
        )
        plan2 = RenderService.generate_render_plan(
            edl2, preset, rev_id, "src.mp4", "", "out.mp4"
        )
        assert plan1.plan_hash != plan2.plan_hash

    def test_steps_have_correct_indices(self) -> None:
        edl = _make_edl(suggestions=[_make_accepted_suggestion(1_000_000, 2_000_000)])
        preset = RenderPreset()
        plan = RenderService.generate_render_plan(
            edl, preset, uuid4(), source_file_path="src.mp4", output_file_path="out.mp4"
        )
        for i, step in enumerate(plan.ffmpeg_steps):
            assert step.step_index == i

    def test_source_intervals_populated(self) -> None:
        edl = _make_edl(suggestions=[_make_accepted_suggestion(2_000_000, 4_000_000)])
        preset = RenderPreset()
        plan = RenderService.generate_render_plan(
            edl, preset, uuid4(), source_file_path="src.mp4", output_file_path="out.mp4"
        )
        assert len(plan.source_intervals) >= 1
        for iv in plan.source_intervals:
            assert "start_us" in iv
            assert "end_us" in iv


@pytest.mark.unit
class TestFFmpegStepAssembly:
    """直接断言 argv 与 filtergraph —— 不再只测内部字符串拼装助手。"""

    @staticmethod
    def _graph(
        preset: RenderPreset,
        intervals=None,
        subtitle: str = "",
        has_audio: bool = True,
        rotation: int = 0,
    ) -> str:
        from app.models.edl import OutputInterval

        ivs = intervals or [OutputInterval(start_us=0, end_us=5_000_000, source_start_us=0)]
        return RenderService._build_filtergraph(ivs, preset, subtitle, has_audio, rotation)

    def test_single_step_and_required_flags(self) -> None:
        edl = _make_edl()
        preset = RenderPreset()
        plan = RenderService.generate_render_plan(
            edl, preset, uuid4(), source_file_path="src.mp4", output_file_path="out.mp4"
        )
        assert len(plan.ffmpeg_steps) == 1
        args = plan.ffmpeg_steps[0].args
        assert args[:5] == ["-noautorotate", "-display_rotation:v:0", "0", "-i", "src.mp4"]
        assert "-filter_complex" in args
        assert "-map" in args and "[outv]" in args
        assert args[-1] == "out.mp4"
        assert "-c:v" in args

    def test_horizontal_scale_in_graph(self) -> None:
        preset = RenderPreset(
            resolution=Resolution.P1080, aspect_ratio=AspectRatio.HORIZONTAL
        )
        assert "scale=1920:1080" in self._graph(preset)

    def test_vertical_blur_background_graph(self) -> None:
        graph = self._graph(RenderPreset())
        assert "split=2[fg_src][bg_src]" in graph
        assert "boxblur=20:5" in graph
        assert "overlay=" in graph
        # 布局链已完成缩放，不应再叠一次 scale
        assert graph.count("scale=") == 2  # 背景 increase + 前景 decrease

    def test_preview_quality_halves_canvas(self) -> None:
        preset = RenderPreset(resolution=Resolution.P1080, preview_quality=True)
        graph = self._graph(preset)
        assert "scale=540:960" in graph

    def test_smart_crop_fills_canvas_by_center_crop(self) -> None:
        preset = RenderPreset(layout_mode=LayoutMode.SMART_CROP)
        graph = self._graph(preset)
        assert "boxblur" not in graph
        # 只缩放一次，且必须是"放大到填满 + 居中裁剪"，否则输出尺寸会随源素材漂移
        assert graph.count("scale=") == 1
        assert "force_original_aspect_ratio=increase" in graph
        assert "crop=1080:1920" in graph

    def test_target_canvas_is_always_even(self) -> None:
        """yuv420p 要求宽高为偶数，奇数画布会让 libx264 在打开编码器时直接失败。"""
        for aspect in AspectRatio:
            for resolution in (Resolution.P720, Resolution.P1080):
                for preview in (True, False):
                    width, height = RenderService.target_canvas(
                        RenderPreset(
                            aspect_ratio=aspect, resolution=resolution, preview_quality=preview
                        )
                    )
                    assert width % 2 == 0 and height % 2 == 0, (aspect, resolution, preview)

    def test_smart_crop_scale_forces_even_dimensions(self) -> None:
        """智能裁切的缩放结果直接决定裁剪输入，必须取偶。"""
        graph = self._graph(RenderPreset(layout_mode=LayoutMode.SMART_CROP))
        assert "force_divisible_by=2" in graph

    @pytest.mark.parametrize(
        ("aspect", "canvas"),
        [(AspectRatio.VERTICAL, "1080:1920"), (AspectRatio.HORIZONTAL, "1920:1080")],
    )
    @pytest.mark.parametrize("layout", [LayoutMode.SMART_CROP, LayoutMode.BLUR_BACKGROUND])
    def test_fixed_resolution_always_fills_canvas(
        self, aspect: AspectRatio, canvas: str, layout: LayoutMode
    ) -> None:
        """选了 720p/1080p 就必须正好是那个画布，横屏也不例外。"""
        graph = self._graph(
            RenderPreset(resolution=Resolution.P1080, aspect_ratio=aspect, layout_mode=layout)
        )
        assert f"crop={canvas}" in graph
        assert ("boxblur" in graph) is (layout is LayoutMode.BLUR_BACKGROUND)

    @pytest.mark.parametrize("layout", [LayoutMode.SMART_CROP, LayoutMode.BLUR_BACKGROUND])
    def test_source_resolution_keeps_source_geometry(self, layout: LayoutMode) -> None:
        """「源分辨率」不裁剪、不加模糊背景，只把边长取偶（yuv420p 的硬要求）。"""
        graph = self._graph(
            RenderPreset(resolution=Resolution.SOURCE, layout_mode=layout)
        )
        assert "crop=" not in graph
        assert "boxblur" not in graph
        assert "scale=trunc(iw/2)*2:trunc(ih/2)*2" in graph
        assert "[outv]" in graph

    def test_subtitle_filter_added_and_escaped(self) -> None:
        preset = RenderPreset(subtitle_mode=SubtitleMode.BURN_IN)
        graph = self._graph(preset, subtitle="C:/subs/a.ass")
        assert "subtitles=filename='C\\:/subs/a.ass'" in graph

    def test_subtitle_quote_cannot_escape_filter(self) -> None:
        """片名里的单引号必须被转义，否则可以注入任意滤镜文本。"""
        preset = RenderPreset(subtitle_mode=SubtitleMode.BURN_IN)
        graph = self._graph(preset, subtitle="/tmp/ev';-vf=bad.ass")
        assert r"\'" in graph

    def test_no_subtitle_filter_when_none(self) -> None:
        """模式判定发生在计划装配处：subtitle_mode=none 时即便给了字幕路径也不烧录。"""
        preset = RenderPreset(subtitle_mode=SubtitleMode.NONE)
        plan = RenderService.generate_render_plan(
            _make_edl(),
            preset,
            uuid4(),
            source_file_path="src.mp4",
            subtitle_file_path="/tmp/x.ass",
            output_file_path="out.mp4",
        )
        assert "subtitles" not in " ".join(plan.ffmpeg_steps[0].args)

    def test_subtitles_reach_graph_when_burn_in(self) -> None:
        preset = RenderPreset(subtitle_mode=SubtitleMode.BURN_IN)
        plan = RenderService.generate_render_plan(
            _make_edl(),
            preset,
            uuid4(),
            source_file_path="src.mp4",
            subtitle_file_path="/tmp/x.ass",
            output_file_path="out.mp4",
        )
        assert "subtitles=filename=" in " ".join(plan.ffmpeg_steps[0].args)
        assert "/tmp/x.ass" in plan.ffmpeg_steps[0].input_files

    def test_audio_less_graph_skips_atrim(self) -> None:
        preset = RenderPreset(subtitle_mode=SubtitleMode.NONE)
        graph = self._graph(preset, has_audio=False)
        assert "atrim" not in graph
        assert "concat=n=1:v=1:a=0" in graph

    def test_multi_interval_concat_labels(self) -> None:
        from app.models.edl import OutputInterval

        ivs = [
            OutputInterval(start_us=0, end_us=2_000_000, source_start_us=0),
            OutputInterval(start_us=2_000_000, end_us=5_000_000, source_start_us=3_000_000),
        ]
        graph = self._graph(RenderPreset(), intervals=ivs)
        assert "concat=n=2:v=1:a=1" in graph
        assert "[v0][a0][v1][a1]concat" in graph

    def test_hash_stable_across_output_directories(self) -> None:
        """plan_hash 不能含易变的临时绝对路径，否则预览缓存永不命中。"""
        edl = _make_edl()
        preset = RenderPreset()
        rev = uuid4()
        plan_a = RenderService.generate_render_plan(
            edl, preset, rev, "src.mp4", "", "/tmp/workA/out.mp4"
        )
        plan_b = RenderService.generate_render_plan(
            edl, preset, rev, "src.mp4", "", "/other/place/out.mp4"
        )
        assert plan_a.plan_hash == plan_b.plan_hash


@pytest.mark.unit
class TestOrientationHandling:
    """源素材旋转：像素方向由我们显式决定，不依赖 FFmpeg 的自动旋转。

    FFmpeg 只在输入流被单个滤镜消费时才自动旋转；多段 trim 会让它退化成
    "像素不转、只把 displaymatrix 抄到输出"，竖屏素材因此在播放器里横倒。
    """

    @staticmethod
    def _ivs(count: int) -> list:
        from app.models.edl import OutputInterval

        return [
            OutputInterval(
                start_us=i * 2_000_000,
                end_us=(i + 1) * 2_000_000,
                source_start_us=i * 3_000_000,
            )
            for i in range(count)
        ]

    @pytest.mark.parametrize(
        ("rotation", "expected"),
        [(0, None), (90, "transpose=2"), (180, "hflip,vflip"), (270, "transpose=1")],
    )
    def test_rotation_maps_to_expected_filter(self, rotation: int, expected) -> None:
        graph = RenderService._build_filtergraph(
            self._ivs(1), RenderPreset(), "", True, rotation
        )
        if expected is None:
            assert "transpose" not in graph
            assert "hflip" not in graph
            assert graph.startswith("[0:v]trim=")
        else:
            assert expected in graph

    def test_negative_rotation_normalized_before_mapping(self) -> None:
        """-90 与 270 必须等价（探测层已归一，这里再兜一层）。"""
        assert RenderService._rotation_filter(-90) == "transpose=1"
        assert RenderService._rotation_filter(270) == "transpose=1"

    def test_unexpected_angle_adds_no_filter(self) -> None:
        assert RenderService._rotation_filter(45) == ""

    def test_rotation_applied_once_for_multiple_intervals(self) -> None:
        graph = RenderService._build_filtergraph(
            self._ivs(3), RenderPreset(), "", True, 270
        )
        assert graph.count("transpose=1") == 1
        assert "split=3[rv0][rv1][rv2]" in graph
        assert "[rv0]trim=" in graph and "[rv2]trim=" in graph
        # 未旋转时各分支直接复用 [0:v]，不应出现 rv 标签
        plain = RenderService._build_filtergraph(self._ivs(3), RenderPreset(), "", True, 0)
        assert "[rv" not in plain

    def test_single_interval_does_not_split(self) -> None:
        graph = RenderService._build_filtergraph(
            self._ivs(1), RenderPreset(), "", True, 270
        )
        # 一段时直接串接，无需 split（blur 布局自己的 split 不算）
        assert "transpose=1,split" not in graph
        assert graph.startswith("[0:v]transpose=1[rv0];[rv0]trim=")

    def test_audio_chain_unaffected_by_rotation(self) -> None:
        graph = RenderService._build_filtergraph(
            self._ivs(2), RenderPreset(), "", True, 270
        )
        # 音轨标签带显式序号（[0:a:0] 与 [0:a] 等价），选轨只需换这个下标
        assert "[0:a:0]atrim=" in graph

    def test_rotation_from_edl_reaches_argv_and_graph(self) -> None:
        plan = RenderService.generate_render_plan(
            _make_edl(rotation=270),
            RenderPreset(),
            uuid4(),
            source_file_path="src.mp4",
            output_file_path="out.mp4",
        )
        args = plan.ffmpeg_steps[0].args
        graph = args[args.index("-filter_complex") + 1]
        assert "transpose=1" in graph
        assert args[:3] == ["-noautorotate", "-display_rotation:v:0", "0"]

    def test_rotation_changes_plan_hash(self) -> None:
        """旋转角不同却共用 plan_hash 会让预览缓存吐出错误方向的成片。"""
        rev = uuid4()
        plain = RenderService.generate_render_plan(
            _make_edl(rotation=0), RenderPreset(), rev, "src.mp4", "", "out.mp4"
        )
        rotated = RenderService.generate_render_plan(
            _make_edl(rotation=270), RenderPreset(), rev, "src.mp4", "", "out.mp4"
        )
        assert plain.plan_hash != rotated.plan_hash

    def test_empty_source_rejected(self) -> None:
        with pytest.raises(RenderServiceError, match="源文件路径为空"):
            RenderService.generate_render_plan(_make_edl(), RenderPreset(), uuid4())

    def test_step_args_are_list_not_string(self) -> None:
        edl = _make_edl()
        preset = RenderPreset()
        plan = RenderService.generate_render_plan(
            edl, preset, uuid4(), source_file_path="src.mp4", output_file_path="out.mp4"
        )
        for step in plan.ffmpeg_steps:
            assert isinstance(step.args, list)
            for arg in step.args:
                assert isinstance(arg, str)


@pytest.mark.unit
class TestPreviewAndExportPlans:
    def test_preview_plan_uses_720p(self) -> None:
        edl = _make_edl()
        plan = RenderService.generate_preview_plan(
            edl, uuid4(), source_file_path="src.mp4", output_file_path="preview.mp4"
        )
        assert plan.preset.resolution == Resolution.P720
        assert plan.preset.preview_quality is True

    def test_export_plan_uses_preset_resolution(self) -> None:
        edl = _make_edl()
        preset = RenderPreset(resolution=Resolution.P1080)
        plan = RenderService.generate_export_plan(
            edl, uuid4(), preset, source_file_path="src.mp4", output_file_path="export.mp4"
        )
        assert plan.preset.resolution == Resolution.P1080
        assert plan.preset.preview_quality is False


@pytest.mark.unit
class TestDiskSpaceCheck:
    def test_sufficient_space(self) -> None:
        RenderService.check_disk_space("/output", 1_000_000, 2_000_000)

    def test_insufficient_space_raises(self) -> None:
        with pytest.raises(RenderServiceError, match="磁盘空间不足"):
            RenderService.check_disk_space("/output", 2_000_000, 1_000_000)


@pytest.mark.unit
class TestOutputPathCheck:
    def test_same_path_rejected(self) -> None:
        with pytest.raises(RenderServiceError, match="不能与源文件"):
            RenderService.check_output_path("video.mp4", "video.mp4")

    def test_different_path_allowed(self) -> None:
        RenderService.check_output_path("output.mp4", "source.mp4")


@pytest.mark.unit
class TestEstimateOutputSize:
    def test_size_estimation(self) -> None:
        size = RenderService.estimate_output_size(60_000_000, Resolution.P1080)
        assert size > 0

    def test_longer_video_larger_size(self) -> None:
        size1 = RenderService.estimate_output_size(60_000_000, Resolution.P1080)
        size2 = RenderService.estimate_output_size(120_000_000, Resolution.P1080)
        assert size2 > size1


@pytest.mark.unit
class TestWorkdirCleanup:
    def _executor(self, tmp_path):
        from app.services.render_service import RenderExecutor

        return RenderExecutor(work_root=tmp_path / "work")

    def test_cleanup_removes_nested_directories(self, tmp_path) -> None:
        executor = self._executor(tmp_path)
        plan_id = uuid4()
        nested = executor._plan_dir(plan_id) / "sub"
        nested.mkdir(parents=True)
        (nested / "subtitles.ass").write_text("a", encoding="utf-8")

        executor._cleanup(plan_id)
        assert not executor._plan_dir(plan_id).exists()

    def test_cleanup_is_idempotent(self, tmp_path) -> None:
        """成功/失败/取消路径都会清理，第二次不能报 warning。"""
        executor = self._executor(tmp_path)
        plan_id = uuid4()
        executor._plan_dir(plan_id).mkdir(parents=True)

        executor._cleanup(plan_id)
        executor._cleanup(plan_id)
        assert not executor._plan_dir(plan_id).exists()

    def test_keep_debug_files_preserves_workdir(self, tmp_path) -> None:
        from app.services.render_service import RenderExecutor

        executor = RenderExecutor(work_root=tmp_path / "work", keep_debug_files=True)
        plan_id = uuid4()
        executor._plan_dir(plan_id).mkdir(parents=True)

        executor._cleanup(plan_id)
        assert executor._plan_dir(plan_id).exists()


@pytest.mark.unit
class TestEncoderParameters:
    """fps / CRF / 音轨序号是导出面板上的旋钮，必须真的落到 argv 和 plan_hash。"""

    @staticmethod
    def _plan(preset: RenderPreset) -> RenderPlan:
        edl = _make_edl(suggestions=[_make_accepted_suggestion()])
        return RenderService.generate_render_plan(
            edl,
            preset,
            uuid4(),
            source_file_path="src.mp4",
            output_file_path="out.mp4",
            has_audio=True,
        )

    @classmethod
    def _args(cls, preset: RenderPreset) -> list[str]:
        return cls._plan(preset).ffmpeg_steps[0].args

    @classmethod
    def _graph(cls, preset: RenderPreset) -> str:
        args = cls._args(preset)
        return args[args.index("-filter_complex") + 1]

    @staticmethod
    def _flag(args: list[str], flag: str) -> str:
        return args[args.index(flag) + 1]

    def test_default_crf_keeps_previous_final_quality(self) -> None:
        # 默认值必须等于改动前写死的 18，否则老项目重新导出会悄悄变糊
        assert self._flag(self._args(RenderPreset()), "-crf") == "18"

    def test_preset_crf_reaches_argv(self) -> None:
        assert self._flag(self._args(RenderPreset(crf=28)), "-crf") == "28"

    def test_preview_quality_ignores_preset_crf(self) -> None:
        args = self._args(RenderPreset(crf=28, preview_quality=True))
        assert self._flag(args, "-crf") == "26"

    def test_crf_change_invalidates_plan_hash(self) -> None:
        assert self._plan(RenderPreset(crf=20)).plan_hash != self._plan(
            RenderPreset(crf=28)
        ).plan_hash

    def test_no_fps_filter_when_fps_is_none(self) -> None:
        assert "fps=" not in self._graph(RenderPreset())
        assert "-r" not in self._args(RenderPreset())

    def test_fps_filter_sits_at_the_end_of_the_chain(self) -> None:
        # 放在画幅/缩放之后才是「按目标帧率重采样」，放前面 blur 布局会按错帧率取帧
        assert "fps=30,format=yuv420p[outv]" in self._graph(RenderPreset(fps=30))

    def test_fps_change_invalidates_plan_hash(self) -> None:
        assert self._plan(RenderPreset(fps=30)).plan_hash != self._plan(
            RenderPreset()
        ).plan_hash

    def test_audio_track_index_reaches_the_filter_label(self) -> None:
        assert "[0:a:2]atrim=" in self._graph(RenderPreset(audio_track_index=2))
        assert "[0:a:0]atrim=" in self._graph(RenderPreset())

    def test_audio_track_change_invalidates_plan_hash(self) -> None:
        assert self._plan(
            RenderPreset(audio_track_index=1)
        ).plan_hash != self._plan(RenderPreset(audio_track_index=2)).plan_hash

    def test_crf_out_of_range_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RenderPreset(crf=CRF_MIN - 1)
        with pytest.raises(ValidationError):
            RenderPreset(crf=CRF_MAX + 1)

    def test_fps_out_of_range_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            RenderPreset(fps=0)
        with pytest.raises(ValidationError):
            RenderPreset(fps=240)
